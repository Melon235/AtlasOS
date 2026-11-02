"""Deterministic M6 fake runtime/tool instrumentation shared by tests and formal eval."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from anycode.identity.context import ExecutionContext
from anycode.tools.idempotency import InMemoryToolIdempotencyStore
from anycode.types import (
    AgentInfo,
    LLMMessage,
    TextBlock,
    ToolDefinition,
    ToolResult,
    ToolSecurityPolicy,
    ToolUseBlock,
    ToolUseContext,
)
from pydantic import BaseModel

from atlasos.actions import (
    DEFAULT_MAPPINGS,
    ActionCatalog,
    ActionMapping,
    ActionPolicy,
    ActionRule,
)
from atlasos.approvals.models import ReviewerIdentity
from atlasos.approvals.resume import ResumeOutcome, resume_execution_suspension
from atlasos.approvals.store import SQLiteApprovalStore
from atlasos.ledger import SQLiteActionLedgerStore
from atlasos.suspension.batch import BatchCall
from atlasos.suspension.models import ExecutionSuspension
from atlasos.suspension.store import SQLiteSuspensionStore
from atlasos.tools import AtlasToolRuntime


class EmailInput(BaseModel):
    to: str
    subject: str = "subject"
    body: str = "body"
    idempotency_key: str | None = None


class ChargeInput(BaseModel):
    recipient: str
    amount: int
    idempotency_key: str | None = None


class DocsInput(BaseModel):
    query: str


class MutateInput(BaseModel):
    value: str


class CrashInput(BaseModel):
    value: str = "crash"
    idempotency_key: str | None = None


class ReadInput(BaseModel):
    query: str


class FakeProvider:
    """Keyed idempotent effect sink; duplicate keys never create a second effect."""

    def __init__(self) -> None:
        self.effects = 0
        self.attempts = 0
        self.duplicate_effects = 0
        self.cache: dict[str, tuple[str, ToolResult]] = {}

    def mutate(self, key: str, payload: dict[str, Any]) -> ToolResult:
        self.attempts += 1
        fingerprint = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        existing = self.cache.get(key)
        if existing is not None:
            if existing[0] != fingerprint:
                return ToolResult(data="provider idempotency conflict", is_error=True)
            if self.effects > 0:
                self.duplicate_effects += 0
            return existing[1]
        self.effects += 1
        result = ToolResult(data=f"effect:{key}:ok")
        self.cache[key] = (fingerprint, result)
        return result


class CrashAfterEffectProvider:
    def __init__(self) -> None:
        self.effects = 0

    def mutate(self, payload: dict[str, Any]) -> None:
        del payload
        self.effects += 1
        raise RuntimeError("transport failed after external effect")


@dataclass
class M6World:
    root: Path
    provider: FakeProvider = field(default_factory=FakeProvider)
    non_idempotent: CrashAfterEffectProvider = field(default_factory=CrashAfterEffectProvider)
    calls: dict[str, int] = field(default_factory=dict)
    idempotency: InMemoryToolIdempotencyStore = field(default_factory=InMemoryToolIdempotencyStore)
    suspension_store: SQLiteSuspensionStore = field(init=False)
    approval_store: SQLiteApprovalStore = field(init=False)
    ledger: SQLiteActionLedgerStore = field(init=False)
    state_path: Path = field(init=False)
    conversation_path: Path = field(init=False)
    approval_store_path: Path = field(init=False)
    clock: Any | None = None

    def __post_init__(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.state_path = self.root / "tool_state.json"
        self.conversation_path = self.root / "conversation.json"
        self.approval_store_path = self.root / "approvals.db"
        self.suspension_store = SQLiteSuspensionStore(self.root / "runtime.db", clock=self.clock)
        self.approval_store = SQLiteApprovalStore(self.approval_store_path)
        self.ledger = SQLiteActionLedgerStore(self.root / "ledger.db")

    # -- tool definitions ---------------------------------------------------

    def tools(self) -> tuple[ToolDefinition, ...]:
        async def email(validated: EmailInput, context: ToolUseContext) -> ToolResult:
            self.calls["email.send"] = self.calls.get("email.send", 0) + 1
            return self.provider.mutate(
                f"email:{context.idempotency_key}",
                {"to": validated.to, "body": validated.body},
            )

        async def charge(validated: ChargeInput, context: ToolUseContext) -> ToolResult:
            self.calls["payment.charge"] = self.calls.get("payment.charge", 0) + 1
            return self.provider.mutate(
                f"charge:{context.idempotency_key}",
                {"recipient": validated.recipient, "amount": validated.amount},
            )

        async def docs(validated: DocsInput, context: ToolUseContext) -> ToolResult:
            del context
            self.calls["docs.search"] = self.calls.get("docs.search", 0) + 1
            return ToolResult(data=f"docs:{validated.query}")

        async def read(validated: ReadInput, context: ToolUseContext) -> ToolResult:
            del context
            self.calls["counter.read"] = self.calls.get("counter.read", 0) + 1
            return ToolResult(data=f"counter:{self.calls['counter.read']}:{validated.query}")

        async def unsafe(validated: MutateInput, context: ToolUseContext) -> ToolResult:
            del context
            self.calls["unsafe.mutate"] = self.calls.get("unsafe.mutate", 0) + 1
            self.non_idempotent.mutate({"value": validated.value})
            return ToolResult(data="unreachable")

        async def crash(validated: CrashInput, context: ToolUseContext) -> ToolResult:
            self.calls["crash.mutate"] = self.calls.get("crash.mutate", 0) + 1
            self.non_idempotent.mutate({"value": validated.value})
            return ToolResult(data="unreachable")

        async def crash_before(validated: CrashInput, context: ToolUseContext) -> ToolResult:
            del validated, context
            self.calls["crash.before"] = self.calls.get("crash.before", 0) + 1
            from atlasos.durability.faults import InjectedCrash

            raise InjectedCrash("crash after M4 claim before tool execution")

        return (
            ToolDefinition(
                name="email.send",
                description="send an external email",
                input_model=EmailInput,
                execute=email,
                side_effecting=True,
            ),
            ToolDefinition(
                name="payment.charge",
                description="charge a payment recipient",
                input_model=ChargeInput,
                execute=charge,
                side_effecting=True,
            ),
            ToolDefinition(
                name="docs.search",
                description="search internal documentation",
                input_model=DocsInput,
                execute=docs,
                side_effecting=False,
            ),
            ToolDefinition(
                name="counter.read",
                description="read a fresh counter",
                input_model=ReadInput,
                execute=read,
                side_effecting=False,
            ),
            ToolDefinition(
                name="unsafe.mutate",
                description="unsafe external mutation",
                input_model=MutateInput,
                execute=unsafe,
                side_effecting=True,
            ),
            ToolDefinition(
                name="crash.mutate",
                description="crash after an external effect",
                input_model=CrashInput,
                execute=crash,
                side_effecting=True,
            ),
            ToolDefinition(
                name="crash.before",
                description="crash before an external effect",
                input_model=CrashInput,
                execute=crash_before,
                side_effecting=True,
            ),
        )

    def catalog(self) -> ActionCatalog:
        return ActionCatalog(
            DEFAULT_MAPPINGS
            + (
                ActionMapping("docs.search", "docs.search", "docs", frozenset({"research"})),
                ActionMapping(
                    "payment.charge",
                    "payment.charge",
                    "tool",
                    frozenset({"coding", "research"}),
                ),
                ActionMapping("counter.read", "counter.read", "counter", frozenset({"research"})),
                ActionMapping("unsafe.mutate", "unsafe.mutate", "tool", frozenset({"research"})),
                ActionMapping("crash.mutate", "crash.mutate", "tool", frozenset({"research"})),
                ActionMapping("crash.before", "crash.before", "tool", frozenset({"research"})),
            )
        )

    def policy(
        self,
        *,
        human: tuple[str, ...] = ("communication.send",),
        deny: tuple[str, ...] = ("unsafe.mutate",),
        allow_extra: tuple[str, ...] = (),
    ) -> ActionPolicy:
        allow = (
            "control.search",
            "control.dispatch",
            "filesystem.read",
            "filesystem.write",
            "docs.search",
            "counter.read",
            "payment.charge",
            *allow_extra,
        )
        rules = (
            tuple(
                ActionRule(id=f"allow-{action}", decision="ALLOW", action=action)
                for action in allow
            )
            + tuple(
                ActionRule(id=f"human-{action}", decision="REQUIRE_HUMAN", action=action)
                for action in human
            )
            + tuple(
                ActionRule(id=f"deny-{action}", decision="DENY", action=action) for action in deny
            )
        )
        return ActionPolicy(name="m6-test", default="REQUIRE_HUMAN", rules=rules)

    def runtime(
        self,
        *,
        policy: ActionPolicy | None = None,
        tools: tuple[ToolDefinition, ...] | None = None,
        approval_store: SQLiteApprovalStore | None = None,
        suspension_store: SQLiteSuspensionStore | None = None,
        idempotency: Any | None = None,
        runtime_instance_id: str = "runtime-m6",
        sandbox_tools: Any | None = None,
        fault_hook: Callable[[str], None] | None = None,
        trajectory_recorder: Any | None = None,
    ) -> AtlasToolRuntime:
        runtime = AtlasToolRuntime(
            "run-m6",
            state_path=self.state_path,
            conversation_path=self.conversation_path,
            external_tools=tools if tools is not None else self.tools(),
            action_policy=policy if policy is not None else self.policy(),
            action_catalog=self.catalog(),
            action_ledger=self.ledger,
            idempotency_store=idempotency or self.idempotency,
            suspension_store=suspension_store or self.suspension_store,
            approval_store=approval_store or self.approval_store,
            runtime_instance_id=runtime_instance_id,
            sandbox_tools=sandbox_tools,
            trajectory_recorder=trajectory_recorder,
        )
        if fault_hook is not None and runtime.suspension_coordinator is not None:
            runtime.suspension_coordinator._fault_hook = fault_hook  # noqa: SLF001
        return runtime

    # -- context / calls ----------------------------------------------------

    def context(
        self,
        *,
        run_id: str = "run-m6",
        task_id: str = "task-m6",
        worker_id: str = "worker-m6",
        profile_id: str = "research_worker",
        principal: str = "user:test",
        tenant: str = "tenant-a",
    ) -> ToolUseContext:
        return ToolUseContext(
            agent=AgentInfo(name=f"{profile_id}:{worker_id}", role="worker", model="fake-model"),
            execution_context=ExecutionContext(
                principal=principal,
                tenant_scope=tenant,
                classification="internal",
                attributes={
                    "run_id": run_id,
                    "task_id": task_id,
                    "worker_id": worker_id,
                    "profile_id": profile_id,
                    "worker_profile": profile_id,
                    "operating_scope": "research",
                },
            ),
            security_policy=ToolSecurityPolicy(workspace_root=str(self.root / "workspace")),
        )

    @staticmethod
    def call(
        ordinal: int,
        call_id: str,
        tool_name: str,
        raw_input: dict[str, Any],
        *,
        scope: str = "scope",
    ) -> BatchCall:
        return BatchCall(
            ordinal=ordinal,
            call_id=call_id,
            tool_name=tool_name,
            raw_input=raw_input,
            execution_key=f"{scope}:{ordinal}",
        )

    def dispatch_call(
        self,
        ordinal: int,
        call_id: str,
        target: str,
        arguments: dict[str, Any],
        *,
        scope: str = "scope",
    ) -> BatchCall:
        return self.call(
            ordinal,
            call_id,
            "dispatch_action",
            {"tool_id": target, "arguments": arguments},
            scope=scope,
        )

    @staticmethod
    def assistant(calls: list[BatchCall]) -> LLMMessage:
        content: list[Any] = [
            ToolUseBlock(id=call.call_id, name=call.tool_name, input=call.raw_input)
            for call in calls
        ]
        if not content:
            content = [TextBlock(text="")]
        return LLMMessage(role="assistant", content=content)

    async def disclose(self, runtime: AtlasToolRuntime, query: str) -> None:
        await runtime.search(query)

    async def suspend(
        self,
        runtime: AtlasToolRuntime,
        calls: list[BatchCall],
        *,
        context: ToolUseContext | None = None,
        scope: str = "scope",
        sandbox_binding_digest: str | None = None,
        sandbox_required: bool = False,
        model_response_id: str | None = None,
    ) -> ExecutionSuspension:
        assert runtime.suspension_coordinator is not None
        actual_context = context or self.context()
        plan = await runtime.preflight_batch(
            calls,
            actual_context,
            model_response_id=model_response_id,
            execution_scope=scope,
        )
        if not plan.requires_human:
            raise AssertionError("expected at least one REQUIRE_HUMAN call")
        execution_context = actual_context.execution_context
        task_id = (
            str(execution_context.attributes.get("task_id"))
            if execution_context is not None
            else None
        )
        if sandbox_required or runtime.sandbox_required(task_id):
            sandbox_required = True
            if sandbox_binding_digest is None:
                sandbox_binding_digest = runtime.sandbox_binding_digest(task_id)
        return await runtime.suspension_coordinator.suspend(
            plan,
            actual_context,
            assistant_message=self.assistant(calls),
            execution_scope=scope,
            sandbox_binding_digest=sandbox_binding_digest,
            sandbox_required=sandbox_required,
            model_response_id=model_response_id,
        )

    def approve(
        self,
        runtime: AtlasToolRuntime,
        suspension: ExecutionSuspension,
        *,
        actor: str = "alice",
    ) -> None:
        assert runtime.approval_coordinator is not None
        assert runtime.approval_store is not None
        for challenge in runtime.approval_store.list_for_suspension(suspension.suspension_id):
            if challenge.state == "PENDING":
                resolution = runtime.approval_coordinator.resolve(
                    challenge.approval_id,
                    expected_version=challenge.version,
                    decision="APPROVED",
                    reviewer=ReviewerIdentity(actor=actor, source="host"),
                    reason="approved in test",
                )
                assert resolution.status == "resolved"

    def reject(
        self,
        runtime: AtlasToolRuntime,
        suspension: ExecutionSuspension,
        *,
        actor: str = "alice",
    ) -> None:
        assert runtime.approval_coordinator is not None
        assert runtime.approval_store is not None
        for challenge in runtime.approval_store.list_for_suspension(suspension.suspension_id):
            if challenge.state == "PENDING":
                resolution = runtime.approval_coordinator.resolve(
                    challenge.approval_id,
                    expected_version=challenge.version,
                    decision="REJECTED",
                    reviewer=ReviewerIdentity(actor=actor, source="host"),
                    reason="rejected in test",
                )
                assert resolution.status == "resolved"

    async def resume(
        self,
        runtime: AtlasToolRuntime,
        suspension: ExecutionSuspension,
        *,
        context: ToolUseContext | None = None,
        owner: str = "resumer",
        fault_hook: Callable[[str], None] | None = None,
        commit_continuation: bool = True,
    ) -> ResumeOutcome:
        assert runtime.suspension_coordinator is not None
        return await resume_execution_suspension(
            suspension.suspension_id,
            runtime=runtime,
            coordinator=runtime.suspension_coordinator,
            context=context or self.context(),
            owner=owner,
            fault_hook=fault_hook,
            commit_continuation=commit_continuation,
        )


def call_map(*specs: tuple[str, str, dict[str, Any]]) -> list[BatchCall]:
    """Build ordinal calls from (call_id, tool_name, input)."""

    return [
        BatchCall(
            ordinal=index,
            call_id=call_id,
            tool_name=tool_name,
            raw_input=raw_input,
            execution_key=f"scope:{index}",
        )
        for index, (call_id, tool_name, raw_input) in enumerate(specs)
    ]
