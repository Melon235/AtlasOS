"""M4 durable action integration, crash, identity, provider, and recovery tests."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from anycode.hitl.approval import ApprovalManager
from anycode.identity.context import ExecutionContext
from anycode.tools.executor import ToolExecutor
from anycode.tools.idempotency import InMemoryToolIdempotencyStore
from anycode.tools.registry import ToolRegistry
from anycode.types import (
    AgentInfo,
    ApprovalConfig,
    ApprovalRequest,
    ApprovalResponse,
    ToolDefinition,
    ToolResult,
    ToolUseContext,
)
from pydantic import BaseModel

from atlasos.actions import ActionCatalog, ActionPolicy, ActionRule, ActionSafetyGate
from atlasos.durability.faults import InjectedCrash
from atlasos.ledger import (
    ActionIdentityProvider,
    ActionLedgerRecord,
    DurableActionBridge,
    IdempotencyInspection,
    ReconcileResult,
    RecoveryClassifier,
    SQLiteActionLedgerStore,
)
from atlasos.ledger.recovery import ActionLedgerRecovery
from atlasos.tools import AtlasToolRuntime


class EmailInput(BaseModel):
    to: str
    body: str
    idempotency_key: str | None = None


class ReadInput(BaseModel):
    query: str


class FakeIdempotentProvider:
    def __init__(self) -> None:
        self.effects = 0
        self.cache: dict[str, tuple[str, ToolResult]] = {}

    def send(self, key: str, payload: dict[str, Any]) -> ToolResult:
        digest = hashlib.sha256(
            (payload["to"] + "\0" + payload["body"]).encode("utf-8")
        ).hexdigest()
        existing = self.cache.get(key)
        if existing is not None:
            if existing[0] != digest:
                return ToolResult(data="provider idempotency conflict", is_error=True)
            return existing[1]
        self.effects += 1
        result = ToolResult(data=f"sent:{payload['body']}")
        self.cache[key] = (digest, result)
        return result


class FakeNonIdempotentProvider:
    def __init__(self) -> None:
        self.effects = 0

    def send(self, payload: dict[str, Any]) -> None:
        del payload
        self.effects += 1
        raise RuntimeError("transport failed after external effect")


class CountingStore(InMemoryToolIdempotencyStore):
    def __init__(self) -> None:
        super().__init__()
        self.claims = 0
        self.completes = 0
        self.fail_complete = False
        self.crash_complete = False

    async def claim(self, tool_name: str, key: str, input_fingerprint: str):
        self.claims += 1
        return await super().claim(tool_name, key, input_fingerprint)

    async def complete(self, tool_name: str, key: str, result: ToolResult) -> None:
        self.completes += 1
        if self.crash_complete:
            raise InjectedCrash("crash before complete")
        if self.fail_complete:
            raise RuntimeError("complete store failure")
        await super().complete(tool_name, key, result)


class CompletedInspector:
    async def inspect(self, tool_name: str, idempotency_key: str) -> IdempotencyInspection:
        del tool_name, idempotency_key
        return IdempotencyInspection(status="completed", result_digest="done")


class UnresolvedInspector:
    async def inspect(self, tool_name: str, idempotency_key: str) -> IdempotencyInspection:
        del tool_name, idempotency_key
        return IdempotencyInspection(status="unresolved")


class AbsentInspector:
    async def inspect(self, tool_name: str, idempotency_key: str) -> IdempotencyInspection:
        del tool_name, idempotency_key
        return IdempotencyInspection(status="absent")


class FakeReconciler:
    def __init__(self, status: str) -> None:
        self.status = status

    async def reconcile(self, record: ActionLedgerRecord) -> ReconcileResult:
        del record
        return ReconcileResult(status=self.status)  # type: ignore[arg-type]


class RejectGate:
    async def request_approval(self, request: ApprovalRequest) -> ApprovalResponse:
        return ApprovalResponse(
            request_id=request.id,
            approved=False,
            responded_at=datetime.now(UTC),
        )


class ApproveGate:
    async def request_approval(self, request: ApprovalRequest) -> ApprovalResponse:
        return ApprovalResponse(
            request_id=request.id,
            approved=True,
            responded_at=datetime.now(UTC),
        )


class Fixture:
    def __init__(self, tmp_path: Path, *, provider: FakeIdempotentProvider | None = None):
        self.provider = provider or FakeIdempotentProvider()
        self.non_idempotent = FakeNonIdempotentProvider()
        self.calls: dict[str, int] = {}
        self.seen_keys: list[str | None] = []
        self.ledger = SQLiteActionLedgerStore(tmp_path / "ledger.db")
        self.store = CountingStore()
        self.registry = ToolRegistry()

        async def email(validated: EmailInput, context: ToolUseContext) -> ToolResult:
            self.calls["email"] = self.calls.get("email", 0) + 1
            self.seen_keys.append(context.idempotency_key)
            return self.provider.send(context.idempotency_key or "", validated.model_dump())

        async def crash_after_email(validated: EmailInput, context: ToolUseContext) -> ToolResult:
            self.calls["crash_email"] = self.calls.get("crash_email", 0) + 1
            self.non_idempotent.effects += 1
            raise InjectedCrash("crash after external effect")

        async def crash_before_effect(validated: EmailInput, context: ToolUseContext) -> ToolResult:
            del validated, context
            self.calls["crash_before"] = self.calls.get("crash_before", 0) + 1
            raise InjectedCrash("crash after claim before tool")

        async def provider_crash(validated: EmailInput, context: ToolUseContext) -> ToolResult:
            self.calls["provider_crash"] = self.calls.get("provider_crash", 0) + 1
            self.provider.send(context.idempotency_key or "", validated.model_dump())
            raise InjectedCrash("crash after provider effect")

        async def non_idempotent_failure(
            validated: EmailInput, context: ToolUseContext
        ) -> ToolResult:
            del context
            self.calls["nonidem"] = self.calls.get("nonidem", 0) + 1
            self.non_idempotent.send(validated.model_dump())
            return ToolResult(data="unreachable")

        async def read(validated: ReadInput, context: ToolUseContext) -> ToolResult:
            del context
            self.calls["read"] = self.calls.get("read", 0) + 1
            return ToolResult(data=f"read:{validated.query}")

        self.registry.register(
            ToolDefinition(
                name="email.send",
                description="send",
                input_model=EmailInput,
                execute=email,
                side_effecting=True,
            )
        )
        self.registry.register(
            ToolDefinition(
                name="email.crash_after",
                description="crash",
                input_model=EmailInput,
                execute=crash_after_email,
                side_effecting=True,
            )
        )
        self.registry.register(
            ToolDefinition(
                name="email.crash_before",
                description="crash",
                input_model=EmailInput,
                execute=crash_before_effect,
                side_effecting=True,
            )
        )

        self.registry.register(
            ToolDefinition(
                name="email.provider_crash",
                description="provider crash",
                input_model=EmailInput,
                execute=provider_crash,
                side_effecting=True,
            )
        )
        self.registry.register(
            ToolDefinition(
                name="email.nonidem",
                description="non-idempotent failure",
                input_model=EmailInput,
                execute=non_idempotent_failure,
                side_effecting=True,
            )
        )
        self.registry.register(
            ToolDefinition(
                name="weather.read",
                description="read",
                input_model=ReadInput,
                execute=read,
                side_effecting=False,
            )
        )
        self.executor = ToolExecutor(self.registry, idempotency_store=self.store)
        self.context = ToolUseContext(
            agent=AgentInfo(name="research_worker:w1", role="worker", model="fake"),
            execution_context=ExecutionContext(
                principal="user:test",
                attributes={
                    "run_id": "run-1",
                    "task_id": "task-1",
                    "worker_id": "w1",
                    "atlas_worker": {"profile_id": "research_worker"},
                },
            ),
        )

    def bridge(self, **hooks) -> DurableActionBridge:
        return DurableActionBridge(
            self.executor,
            self.registry,
            self.ledger,
            runtime_instance_id="runtime-1",
            catalog=ActionCatalog(),
            **hooks,
        )


async def _send(bridge: DurableActionBridge, fixture: Fixture, key: str, body: str = "x"):
    return await bridge.execute(
        "email.send",
        {"to": "ops@example.test", "body": body},
        fixture.context,
        idempotency_key=key,
    )


async def test_m4_11_to_16_replay_conflict_concurrent_and_in_progress(
    tmp_path: Path,
) -> None:
    fixture = Fixture(tmp_path)
    bridge = fixture.bridge()
    first = await _send(bridge, fixture, "key-1")
    assert first.is_error is not True
    assert fixture.calls["email"] == 1
    replay = await _send(bridge, fixture, "key-1")
    assert replay.is_error is not True
    assert fixture.calls["email"] == 1
    records = fixture.ledger.list()
    assert records[0].action_id == records[1].action_id
    assert records[1].completion_kind == "REPLAYED"
    assert records[0].attempt_id != records[1].attempt_id

    conflict = await _send(bridge, fixture, "key-1", body="different")
    assert conflict.is_error is True
    assert "conflict" in conflict.data
    assert fixture.calls["email"] == 1
    assert fixture.ledger.list()[-1].completion_kind == "CONFLICT"

    concurrent = Fixture(tmp_path / "concurrent")
    concurrent_bridge = concurrent.bridge()
    results = await asyncio.gather(
        _send(concurrent_bridge, concurrent, "concurrent"),
        _send(concurrent_bridge, concurrent, "concurrent"),
    )
    assert any(result.is_error is not True for result in results)
    assert concurrent.calls["email"] == 1
    assert any(record.completion_kind == "REPLAYED" for record in concurrent.ledger.list())

    blocked = Fixture(tmp_path / "blocked")
    blocked_bridge = blocked.bridge()
    fingerprint = hashlib.sha256(
        json.dumps(
            {"to": "ops@example.test", "body": "x", "idempotency_key": None},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    claim = await blocked.store.claim("email.send", "blocked-key", fingerprint)
    assert claim.outcome == "execute"
    in_progress = await _send(blocked_bridge, blocked, "blocked-key")
    assert in_progress.is_error is True
    assert blocked.calls.get("email", 0) == 0
    assert blocked.ledger.list()[-1].state == "UNCERTAIN"


async def test_m4_17_read_tool_not_cached(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path)
    bridge = fixture.bridge()
    first = await bridge.execute("weather.read", {"query": "x"}, fixture.context)
    second = await bridge.execute("weather.read", {"query": "x"}, fixture.context)
    assert first.data == second.data
    assert fixture.calls["read"] == 2
    assert fixture.ledger.list() == ()


async def test_m4_18_to_24_crash_windows(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path)

    # C0: before ledger.
    def crash_before_ledger() -> None:
        raise InjectedCrash("before ledger")

    with pytest.raises(InjectedCrash):
        await _send(fixture.bridge(before_record=crash_before_ledger), fixture, "c0")
    assert fixture.ledger.list() == ()
    assert fixture.store.claims == 0

    # C1: after ledger before claim.
    def crash_after_ledger() -> None:
        raise InjectedCrash("after ledger")

    with pytest.raises(InjectedCrash):
        await _send(fixture.bridge(after_record=crash_after_ledger), fixture, "c1")
    assert fixture.ledger.list()[-1].state == "IN_PROGRESS"
    assert fixture.store.claims == 0

    # C2: after claim before tool.
    fixture2 = Fixture(tmp_path / "c2")
    with pytest.raises(InjectedCrash):
        await fixture2.bridge().execute(
            "email.crash_before",
            {"to": "ops@example.test", "body": "x"},
            fixture2.context,
            idempotency_key="c2",
        )
    assert fixture2.store.claims == 1
    assert fixture2.ledger.list()[-1].state == "UNCERTAIN"

    # C3: after external effect before return.
    fixture3 = Fixture(tmp_path / "c3")
    with pytest.raises(InjectedCrash):
        await fixture3.bridge().execute(
            "email.crash_after",
            {"to": "ops@example.test", "body": "x"},
            fixture3.context,
            idempotency_key="c3",
        )
    assert fixture3.non_idempotent.effects == 1
    assert fixture3.ledger.list()[-1].state == "UNCERTAIN"
    retry = await fixture3.bridge().execute(
        "email.crash_after",
        {"to": "ops@example.test", "body": "x"},
        fixture3.context,
        idempotency_key="c3",
    )
    assert retry.is_error is True
    assert fixture3.non_idempotent.effects == 1

    # C4: Tool returned but AnyCode complete was not durably recorded.
    fixture_c4 = Fixture(tmp_path / "c4")
    fixture_c4.store.crash_complete = True
    with pytest.raises(InjectedCrash):
        await _send(fixture_c4.bridge(), fixture_c4, "c4")
    assert fixture_c4.store.completes == 1
    assert fixture_c4.ledger.list()[-1].state == "UNCERTAIN"

    # C5: AnyCode complete succeeds, then ledger finalization crashes.
    def crash_after_execute() -> None:
        raise InjectedCrash("after execute")

    with pytest.raises(InjectedCrash):
        await _send(fixture.bridge(after_execute=crash_after_execute), fixture, "c4")
    c4_record = fixture.ledger.latest_for_key_hash(ActionIdentityProvider.key_hash("c4"))
    assert c4_record is not None
    assert c4_record.state == "IN_PROGRESS"
    assert fixture.store.completes == 1

    # C6: complete store failure.
    fixture4 = Fixture(tmp_path / "c6")
    fixture4.store.fail_complete = True
    result = await _send(fixture4.bridge(), fixture4, "c6")
    assert result.is_error is True
    assert fixture4.ledger.list()[-1].state == "UNCERTAIN"


async def test_m4_25_to_30_identity_and_model_key(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path)
    bridge = fixture.bridge()
    rejected_model_key = await bridge.execute(
        "email.send",
        {"to": "ops@example.test", "body": "x", "idempotency_key": "model-key"},
        fixture.context,
        idempotency_key="trusted-key",
    )
    assert rejected_model_key.is_error is True
    assert fixture.seen_keys == []
    assert fixture.ledger.list() == ()

    await bridge.execute(
        "email.send",
        {"to": "ops@example.test", "body": "x"},
        fixture.context,
        idempotency_key="trusted-key",
    )
    assert fixture.seen_keys[-1] == "trusted-key"
    first_record = fixture.ledger.list()[-1]
    assert first_record.idempotency_key_hash != "trusted-key"
    assert first_record.action_id
    assert first_record.attempt_id != first_record.action_id

    retry = await _send(bridge, fixture, "trusted-key")
    assert retry.is_error is not True
    retry_record = fixture.ledger.list()[-1]
    assert retry_record.action_id == first_record.action_id
    assert retry_record.attempt_id != first_record.attempt_id
    assert retry_record.completion_kind == "REPLAYED"

    new = await _send(bridge, fixture, "trusted-key-2")
    assert new.is_error is not True
    assert fixture.ledger.list()[-1].action_id != first_record.action_id

    modified = await _send(bridge, fixture, "trusted-key", body="modified")
    assert modified.is_error is True
    assert fixture.ledger.list()[-1].completion_kind == "CONFLICT"

    # A new bridge/executor with the same key resumes the same action identity.
    new_store = CountingStore()
    new_executor = ToolExecutor(fixture.registry, idempotency_store=new_store)
    resumed = DurableActionBridge(
        new_executor,
        fixture.registry,
        fixture.ledger,
        runtime_instance_id="runtime-2",
        catalog=ActionCatalog(),
    )
    effects_before = fixture.provider.effects
    await _send(resumed, fixture, "trusted-key")
    assert fixture.ledger.list()[-1].action_id == first_record.action_id
    assert fixture.provider.effects == effects_before  # provider key prevented a duplicate effect


async def test_m4_31_to_33_provider_idempotency(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path)
    provider = fixture.provider
    first = provider.send("provider-key", {"to": "a", "body": "x"})
    replay = provider.send("provider-key", {"to": "a", "body": "x"})
    assert first.data == replay.data
    assert provider.effects == 1
    conflict = provider.send("provider-key", {"to": "a", "body": "y"})
    assert conflict.is_error is True
    assert provider.effects == 1

    # A provider-backed crash, then a fresh local retry: provider key prevents a
    # duplicate external effect even though the local claim is new.
    crashed = Fixture(tmp_path / "provider-crash")
    with pytest.raises(InjectedCrash):
        await crashed.bridge().execute(
            "email.provider_crash",
            {"to": "ops@example.test", "body": "x"},
            crashed.context,
            idempotency_key="provider-crash-key",
        )
    assert crashed.provider.effects == 1
    fresh = DurableActionBridge(
        ToolExecutor(crashed.registry, idempotency_store=CountingStore()),
        crashed.registry,
        crashed.ledger,
        runtime_instance_id="runtime-fresh",
        catalog=ActionCatalog(),
    )
    with pytest.raises(InjectedCrash):
        await fresh.execute(
            "email.provider_crash",
            {"to": "ops@example.test", "body": "x"},
            crashed.context,
            idempotency_key="provider-crash-key",
        )
    assert crashed.provider.effects == 1


async def test_m4_34_to_39_recovery_and_non_idempotent(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path)

    # Completed AnyCode result but Ledger still IN_PROGRESS: recover safely.
    def crash_after_execute() -> None:
        raise InjectedCrash("after complete")

    with pytest.raises(InjectedCrash):
        await _send(fixture.bridge(after_execute=crash_after_execute), fixture, "recover")
    record = fixture.ledger.list()[-1]
    assert record.state == "IN_PROGRESS"
    recovery = ActionLedgerRecovery(fixture.ledger)
    classification = await recovery.recover(
        record,
        idempotency_key="recover",
        inspector=CompletedInspector(),
    )
    assert classification.state == "COMPLETED"
    assert fixture.ledger.get(record.action_id, record.attempt_id).state == "COMPLETED"  # type: ignore[union-attr]

    # Unresolved claim: UNCERTAIN, never deleted, no automatic retry.
    fixture2 = Fixture(tmp_path / "uncertain")
    with pytest.raises(InjectedCrash):
        await fixture2.bridge().execute(
            "email.crash_after",
            {"to": "ops@example.test", "body": "x"},
            fixture2.context,
            idempotency_key="uncertain",
        )
    uncertain = fixture2.ledger.list()[-1]
    classification = await ActionLedgerRecovery(fixture2.ledger).recover(
        uncertain,
        idempotency_key="uncertain",
        inspector=UnresolvedInspector(),
    )
    assert classification.state == "UNCERTAIN"
    assert fixture2.ledger.get(uncertain.action_id, uncertain.attempt_id) is not None
    retry = await fixture2.bridge().execute(
        "email.crash_after",
        {"to": "ops@example.test", "body": "x"},
        fixture2.context,
        idempotency_key="uncertain",
    )
    assert retry.is_error is True
    assert fixture2.non_idempotent.effects == 1

    # Reconciler success/still-unknown.
    reconciled = await RecoveryClassifier().classify(
        uncertain,
        idempotency_key="uncertain",
        reconciler=FakeReconciler("CONFIRMED_SUCCESS"),
    )
    assert reconciled.state == "COMPLETED"
    unknown = await RecoveryClassifier().classify(
        uncertain,
        idempotency_key="uncertain",
        reconciler=FakeReconciler("STILL_UNKNOWN"),
    )
    assert unknown.state == "UNCERTAIN"

    # Non-idempotent local-only failure: UNCERTAIN, no blind retry.
    fixture3 = Fixture(tmp_path / "nonidem")
    result = await fixture3.bridge().execute(
        "email.nonidem",
        {"to": "ops@example.test", "body": "x"},
        fixture3.context,
        idempotency_key="nonidem",
    )
    assert result.is_error is True
    assert fixture3.ledger.list()[-1].state == "UNCERTAIN"
    retry = await fixture3.bridge().execute(
        "email.nonidem",
        {"to": "ops@example.test", "body": "x"},
        fixture3.context,
        idempotency_key="nonidem",
    )
    assert retry.is_error is True
    assert fixture3.calls["nonidem"] == 1


async def test_m3_deny_and_hitl_reject_do_not_claim(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path)
    bridge = fixture.bridge()
    deny_policy = ActionPolicy(name="deny", default="DENY", rules=())
    denied_gate = ActionSafetyGate(
        bridge,  # type: ignore[arg-type]
        fixture.registry,
        policy=deny_policy,
        catalog=ActionCatalog(),
    )
    denied = await denied_gate.execute(
        "email.send",
        {"to": "ops@example.test", "body": "x"},
        fixture.context,
        idempotency_key="deny-key",
    )
    assert denied.is_error is True
    assert fixture.store.claims == 0
    assert fixture.ledger.list() == ()

    human_policy = ActionPolicy(
        name="human",
        default="REQUIRE_HUMAN",
        rules=(ActionRule(id="human", decision="REQUIRE_HUMAN", action="communication.send"),),
    )
    rejected_gate = ActionSafetyGate(
        bridge,  # type: ignore[arg-type]
        fixture.registry,
        policy=human_policy,
        catalog=ActionCatalog(),
        approval_manager=ApprovalManager(
            ApprovalConfig(enabled=True, timeout_seconds=5.0, default_on_timeout="reject"),
            RejectGate(),
        ),
    )
    rejected = await rejected_gate.execute(
        "email.send",
        {"to": "ops@example.test", "body": "x"},
        fixture.context,
        idempotency_key="reject-key",
    )
    assert rejected.is_error is True
    assert fixture.store.claims == 0
    assert fixture.ledger.list() == ()


async def test_m4_worker_metadata_reaches_ledger(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path)
    await _send(fixture.bridge(), fixture, "worker-key")
    record = fixture.ledger.list()[-1]
    assert record.worker_id == "w1"
    assert record.task_id == "task-1"
    assert record.run_id == "run-1"
    assert record.agent == "research_worker:w1"


async def test_m4_runtime_places_ledger_below_m3(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path)
    email_tool = fixture.registry.get("email.send")
    assert email_tool is not None
    runtime = AtlasToolRuntime(
        "m4-runtime",
        external_tools=(email_tool,),
        action_policy=ActionPolicy(
            name="human",
            default="REQUIRE_HUMAN",
            rules=(
                ActionRule(
                    id="human-comms",
                    decision="REQUIRE_HUMAN",
                    action="communication.send",
                ),
            ),
        ),
        action_catalog=ActionCatalog(),
        action_approval_manager=ApprovalManager(
            ApprovalConfig(enabled=True, timeout_seconds=5.0, default_on_timeout="reject"),
            ApproveGate(),
        ),
        action_ledger=fixture.ledger,
        idempotency_store=fixture.store,
        runtime_instance_id="runtime-m4",
    )
    assert runtime.durable_bridge is not None
    assert runtime.action_safety_gate is not None
    result = await runtime.executor.execute(
        "email.send",
        {"to": "ops@example.test", "body": "runtime"},
        fixture.context,
        idempotency_key="runtime-key",
    )
    assert result.is_error is not True
    assert fixture.calls["email"] == 1
    records = fixture.ledger.list()
    assert records[-1].state == "COMPLETED"
    assert records[-1].runtime_instance_id == "runtime-m4"
    replay = await runtime.executor.execute(
        "email.send",
        {"to": "ops@example.test", "body": "runtime"},
        fixture.context,
        idempotency_key="runtime-key",
    )
    assert replay.is_error is not True
    assert fixture.calls["email"] == 1
    assert fixture.ledger.list()[-1].completion_kind == "REPLAYED"
