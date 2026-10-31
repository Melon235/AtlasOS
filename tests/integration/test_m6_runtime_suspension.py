"""M6 Runtime Suspension + durable approval focused tests."""

# The M6 harness always constructs the durable stores; optional attributes are configured here.
# pyright: reportOptionalMemberAccess=false

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from anycode.types import LLMMessage
from tests.helpers.m6_harness import M6World

from atlasos.approvals.models import (
    ReviewerIdentity,
    derive_approval_id,
)
from atlasos.approvals.store import SQLiteApprovalStore
from atlasos.suspension.batch import BatchCall
from atlasos.suspension.models import (
    ExecutionSuspension,
    UnsupportedSuspensionSchema,
    derive_suspension_id,
)
from atlasos.suspension.store import SQLiteSuspensionStore


def _workspace_file(world: M6World) -> Path:
    workspace = world.root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    target = workspace / "note.txt"
    target.write_text("hello", encoding="utf-8")
    return target


async def _ready(world: M6World, runtime=None, *, policy=None):
    runtime = runtime or world.runtime(policy=policy)
    await world.disclose(runtime, "send external email notification")
    await world.disclose(runtime, "search internal documentation")
    await world.disclose(runtime, "fresh counter read")
    return runtime


def _email_call(world: M6World, body: str = "x") -> list[BatchCall]:
    return [world.dispatch_call(0, "call", "email.send", {"to": "ops@example.test", "body": body})]


def _abc(world: M6World, target: Path) -> list[BatchCall]:
    return [
        world.call(0, "call-a", "file_read", {"path": str(target)}),
        world.dispatch_call(
            1,
            "call-b",
            "email.send",
            {"to": "ops@example.test", "body": "secret-body"},
        ),
        world.dispatch_call(2, "call-c", "docs.search", {"query": "checkout"}),
    ]


async def test_m6_01_whole_batch_preflight_suspends_before_m4(tmp_path: Path) -> None:
    world = M6World(tmp_path / "world")
    runtime = await _ready(world)
    target = _workspace_file(world)
    suspension = await world.suspend(runtime, _abc(world, target))

    assert suspension.state == "ACTIVE"
    assert len(suspension.pending_invocations) == 3
    assert [item.ordinal for item in suspension.pending_invocations] == [0, 1, 2]
    assert [item.call_id for item in suspension.pending_invocations] == [
        "call-a",
        "call-b",
        "call-c",
    ]
    assert suspension.pending_invocations[1].preflight_outcome == "REQUIRE_HUMAN"
    assert suspension.pending_invocations[0].preflight_outcome == "ALLOW"
    assert suspension.pending_invocations[2].preflight_outcome == "ALLOW"
    assert suspension.suspension_id == derive_suspension_id(
        run_id="run-m6",
        response_identity=suspension.response_identity,
        assistant_response_digest=suspension.assistant_response_digest,
    )
    assert runtime.action_ledger.list() == ()
    assert world.provider.effects == 0
    assert world.calls == {}
    assert runtime.state.is_dispatchable("email.send")
    assert runtime.suspension_store.run_state("run-m6") == "PAUSED"
    assert runtime.suspension_store.task_state("run-m6", "task-m6") == "PAUSED"
    assert runtime.m6_metrics is not None
    metrics = runtime.m6_metrics
    assert metrics.tool_calls_entered_m4_before_required_approval == 0
    assert metrics.tool_calls_preflighted == 3


async def test_m6_b01_b02_allow_deny_human_batch_waits(tmp_path: Path) -> None:
    world = M6World(tmp_path / "world")
    runtime = await _ready(world)
    target = _workspace_file(world)
    calls = [
        world.call(0, "a", "file_read", {"path": str(target)}),
        world.dispatch_call(1, "b", "unsafe.mutate", {"value": "x"}),
        world.dispatch_call(2, "c", "email.send", {"to": "ops@example.test", "body": "x"}),
    ]
    suspension = await world.suspend(runtime, calls)
    assert [item.preflight_outcome for item in suspension.pending_invocations] == [
        "ALLOW",
        "DENY",
        "REQUIRE_HUMAN",
    ]
    assert runtime.action_ledger.list() == ()
    assert world.provider.effects == 0
    assert runtime.conversation_store.load("task-m6") is None

    # Human not yet terminal: resume is a no-op settlement.
    waiting = await world.resume(runtime, suspension)
    assert waiting.status == "waiting"
    assert runtime.action_ledger.list() == ()
    assert runtime.conversation_store.load("task-m6") is None

    world.approve(runtime, suspension)
    settled = await world.resume(runtime, suspension)
    assert settled.status == "settled"
    assert world.provider.effects == 1
    assert world.calls["email.send"] == 1
    assert world.calls.get("unsafe.mutate", 0) == 0
    conversation = runtime.conversation_store.load("task-m6")
    assert conversation is not None and len(conversation) == 2
    results = conversation[1]["content"]
    assert [block["tool_use_id"] for block in results] == ["a", "b", "c"]
    assert [block["is_error"] for block in results] == [False, True, False]
    ledger = runtime.action_ledger.list()
    assert len(ledger) == 1
    assert ledger[0].tool_name == "email.send"


async def test_m6_b03_b04_multiple_humans_wait_for_all(tmp_path: Path) -> None:
    world = M6World(tmp_path / "world")
    runtime = await _ready(world)
    calls = [
        world.dispatch_call(0, "one", "email.send", {"to": "a@example.test", "body": "one"}),
        world.dispatch_call(1, "two", "email.send", {"to": "b@example.test", "body": "two"}),
    ]
    suspension = await world.suspend(runtime, calls)
    pending = runtime.approval_store.list_for_suspension(suspension.suspension_id)
    assert len(pending) == 2
    assert pending[0].approval_id != pending[1].approval_id

    first = pending[0]
    resolution = runtime.approval_coordinator.resolve(  # type: ignore[union-attr]
        first.approval_id,
        expected_version=first.version,
        decision="APPROVED",
        reviewer=ReviewerIdentity(actor="alice"),
    )
    assert resolution.status == "resolved"
    outcome = await world.resume(runtime, suspension)
    assert outcome.status == "waiting"
    assert world.provider.effects == 0
    assert runtime.conversation_store.load("task-m6") is None

    second = runtime.approval_store.list_for_suspension(suspension.suspension_id)[1]
    assert second.state == "PENDING"
    runtime.approval_coordinator.resolve(  # type: ignore[union-attr]
        second.approval_id,
        expected_version=second.version,
        decision="REJECTED",
        reviewer=ReviewerIdentity(actor="bob"),
    )
    settled = await world.resume(runtime, suspension)
    assert settled.status == "settled"
    assert world.provider.effects == 1  # only the approved call executes
    results = runtime.conversation_store.load("task-m6")[1]["content"]  # type: ignore[index]
    assert [block["tool_use_id"] for block in results] == ["one", "two"]
    assert results[0]["is_error"] is False
    assert results[1]["is_error"] is True
    assert "Rejected by human reviewer" in results[1]["content"]


async def test_m6_03_m04_approve_and_reject_outcomes(tmp_path: Path) -> None:
    world = M6World(tmp_path / "world")
    runtime = await _ready(world)
    target = _workspace_file(world)

    approve_calls = _abc(world, target)
    suspension = await world.suspend(runtime, approve_calls)
    world.approve(runtime, suspension)
    outcome = await world.resume(runtime, suspension)
    assert outcome.status == "settled"
    assert world.provider.effects == 1
    assert runtime.suspension_store.get(suspension.suspension_id).state == "CLEARED"  # type: ignore[union-attr]
    assert runtime.m6_metrics is not None
    assert runtime.m6_metrics.tool_results_committed == 3
    assert runtime.m6_metrics.suspensions_cleared == 1
    assert runtime.m6_metrics.duplicate_provider_side_effects == 0

    reject_world = M6World(tmp_path / "reject")
    reject_runtime = await _ready(reject_world)
    reject_calls = [
        reject_world.dispatch_call(
            0, "reject", "email.send", {"to": "ops@example.test", "body": "x"}
        )
    ]
    reject_suspension = await reject_world.suspend(reject_runtime, reject_calls)
    reject_world.reject(reject_runtime, reject_suspension)
    rejected = await reject_world.resume(reject_runtime, reject_suspension)
    assert rejected.status == "settled"
    assert reject_world.provider.effects == 0
    assert reject_runtime.action_ledger.list() == ()
    result = reject_runtime.conversation_store.load("task-m6")[1]["content"][0]  # type: ignore[index]
    assert result["is_error"] is True
    assert result["content"] == "Rejected by human reviewer."
    assert reject_runtime.suspension_store.get(reject_suspension.suspension_id).state == "CLEARED"  # type: ignore[union-attr]


async def test_m6_exact_input_and_deterministic_ids(tmp_path: Path) -> None:
    world = M6World(tmp_path / "world")
    runtime = await _ready(world)
    calls = _abc(world, _workspace_file(world))
    suspension = await world.suspend(runtime, calls)
    invocation = suspension.pending_invocations[1]
    assert invocation.canonical_input == {
        "to": "ops@example.test",
        "subject": "subject",
        "body": "secret-body",
        "idempotency_key": None,
    }
    assert invocation.approval_id == derive_approval_id(
        suspension_id=suspension.suspension_id,
        call_id=invocation.call_id,
        action_digest=invocation.action_digest,
        decision_basis_digest=invocation.decision_basis_digest,
    )
    # Reconstructing the exact same paused response yields the same identity.
    assert (
        derive_suspension_id(
            run_id="run-m6",
            response_identity=suspension.response_identity,
            assistant_response_digest=suspension.assistant_response_digest,
        )
        == suspension.suspension_id
    )
    second = await world.suspend(runtime, _abc(world, _workspace_file(world)))
    assert second.suspension_id == suspension.suspension_id
    assert runtime.m6_metrics is not None
    assert runtime.m6_metrics.runtime_suspension_reloads == 1
    assert runtime.approval_store.list_for_suspension(suspension.suspension_id)[0].version == 0


async def test_m6_restart_loads_suspension_without_execution(tmp_path: Path) -> None:
    world = M6World(tmp_path / "world")
    runtime = await _ready(world)
    suspension = await world.suspend(runtime, _abc(world, _workspace_file(world)))

    fresh = world.runtime(runtime_instance_id="runtime-restart")
    loaded = fresh.suspension_store.get(suspension.suspension_id)
    assert loaded is not None
    assert loaded.suspension_id == suspension.suspension_id
    assert loaded.held_message().role == "assistant"
    assert fresh.action_ledger.list() == ()
    assert world.provider.effects == 0
    assert fresh.approval_store.list_pending(suspension_id=suspension.suspension_id)
    # No LLM step is involved in load or approval.
    world.approve(fresh, loaded)
    outcome = await world.resume(fresh, loaded)
    assert outcome.status == "settled"
    assert world.provider.effects == 1


async def test_m6_06_changed_basis_invalidates_old_receipt(tmp_path: Path) -> None:
    world = M6World(tmp_path / "world")
    runtime = await _ready(world)
    calls = [world.dispatch_call(0, "call", "email.send", {"to": "ops@example.test", "body": "x"})]
    suspension = await world.suspend(runtime, calls)
    world.approve(runtime, suspension)

    # Same action, changed security basis: the policy digest changes while the action still
    # requires human approval. The old approval must not satisfy the new basis.
    from atlasos.actions import ActionPolicy, ActionRule

    changed_policy = ActionPolicy(
        name="changed-basis",
        default="REQUIRE_HUMAN",
        rules=(
            ActionRule(id="allow-dispatch", decision="ALLOW", action="control.dispatch"),
            ActionRule(id="human-email", decision="REQUIRE_HUMAN", action="communication.send"),
        ),
    )
    changed = world.runtime(
        policy=changed_policy,
        approval_store=runtime.approval_store,
        suspension_store=runtime.suspension_store,
        runtime_instance_id="runtime-changed",
    )
    outcome = await world.resume(changed, suspension)
    assert outcome.status == "stale"
    assert world.provider.effects == 0
    assert runtime.action_ledger.list() == ()
    challenge = runtime.approval_store.list_for_suspension(suspension.suspension_id)[0]
    assert challenge.state == "APPROVED"
    assert changed.m6_metrics is not None
    assert changed.m6_metrics.stale_decision_basis == 1


async def test_m6_05_current_deny_overrides_approval(tmp_path: Path) -> None:
    world = M6World(tmp_path / "world")
    runtime = await _ready(world)
    calls = [world.dispatch_call(0, "call", "email.send", {"to": "ops@example.test", "body": "x"})]
    suspension = await world.suspend(runtime, calls)
    world.approve(runtime, suspension)
    from atlasos.actions import ActionPolicy, ActionRule

    deny_policy = ActionPolicy(
        name="deny-now",
        default="REQUIRE_HUMAN",
        rules=(
            ActionRule(id="allow-dispatch", decision="ALLOW", action="control.dispatch"),
            ActionRule(id="deny-email", decision="DENY", action="communication.send"),
        ),
    )
    denied_runtime = world.runtime(
        policy=deny_policy,
        approval_store=runtime.approval_store,
        suspension_store=runtime.suspension_store,
        runtime_instance_id="runtime-deny",
    )
    outcome = await world.resume(denied_runtime, suspension)
    assert outcome.status == "settled"
    assert world.provider.effects == 0
    assert denied_runtime.action_ledger.list() == ()
    detail = denied_runtime.conversation_store.load("task-m6")[1]["content"][0]  # type: ignore[index]
    assert detail["is_error"] is True
    assert "action_denied" in detail["content"]
    assert denied_runtime.m6_metrics is not None
    assert denied_runtime.m6_metrics.m3_new_denies_after_approval == 1


async def test_m6_07_changed_input_or_binding_fails_closed(tmp_path: Path) -> None:
    world = M6World(tmp_path / "world")
    runtime = await _ready(world)
    suspension = await world.suspend(runtime, _abc(world, _workspace_file(world)))
    original = suspension.pending_invocations[1]
    tampered = suspension.model_copy(
        update={
            "pending_invocations": (
                suspension.pending_invocations[0],
                original.model_copy(
                    update={"canonical_input": {**original.canonical_input, "body": "changed"}}
                ),
                suspension.pending_invocations[2],
            )
        }
    )
    # Put the tampered suspension back into the durable store for this negative check.
    with runtime.suspension_store._connection() as connection:  # noqa: SLF001
        connection.execute(
            "UPDATE execution_suspensions SET payload=? WHERE suspension_id=?",
            (tampered.model_dump_json(), tampered.suspension_id),
        )
    outcome = await world.resume(runtime, tampered)
    assert outcome.status == "stale"
    assert world.provider.effects == 0
    assert runtime.action_ledger.list() == ()


async def test_m6_08_changed_tool_binding_fails_closed(tmp_path: Path) -> None:
    world = M6World(tmp_path / "world")
    runtime = await _ready(world)
    suspension = await world.suspend(runtime, _abc(world, _workspace_file(world)))
    world.approve(runtime, suspension)

    original = world.tools()
    changed_tools = tuple(
        tool.model_copy(update={"description": "send an external email (v2 source)"})
        if tool.name == "email.send"
        else tool
        for tool in original
    )
    changed = world.runtime(
        tools=changed_tools,
        approval_store=runtime.approval_store,
        suspension_store=runtime.suspension_store,
        runtime_instance_id="runtime-binding",
    )
    outcome = await world.resume(changed, suspension)
    assert outcome.status == "stale"
    assert world.provider.effects == 0
    assert changed.m6_metrics is not None
    assert changed.m6_metrics.stale_tool_bindings == 1


async def test_m6_09_m1_hidden_tool_cannot_resume(tmp_path: Path) -> None:
    world = M6World(tmp_path / "world")
    runtime = await _ready(world)
    suspension = await world.suspend(runtime, _abc(world, _workspace_file(world)))
    world.approve(runtime, suspension)
    fresh_root = tmp_path / "fresh"
    fresh = M6World(fresh_root)
    fresh_runtime = fresh.runtime(
        approval_store=runtime.approval_store,
        suspension_store=runtime.suspension_store,
        idempotency=world.idempotency,
        runtime_instance_id="runtime-hidden",
    )
    # No disclosure state was copied, so the target is hidden in this run.
    outcome = await fresh.resume(fresh_runtime, suspension)
    assert outcome.status == "stale"
    assert world.provider.effects == 0
    assert fresh_runtime.m6_metrics is not None
    assert fresh_runtime.m6_metrics.m1_resume_violations == 1


async def test_m6_10_m2_identity_mismatch_fails_closed(tmp_path: Path) -> None:
    world = M6World(tmp_path / "world")
    runtime = await _ready(world)
    suspension = await world.suspend(runtime, _abc(world, _workspace_file(world)))
    world.approve(runtime, suspension)
    outcome = await world.resume(
        runtime,
        suspension,
        context=world.context(worker_id="different-worker"),
    )
    assert outcome.status == "stale"
    assert world.provider.effects == 0
    assert runtime.m6_metrics is not None
    assert runtime.m6_metrics.m2_identity_violations >= 1


async def test_m6_18_two_resumes_one_continuation_commit(tmp_path: Path) -> None:
    world = M6World(tmp_path / "world")
    runtime = await _ready(world)
    suspension = await world.suspend(runtime, _abc(world, _workspace_file(world)))
    world.approve(runtime, suspension)

    async def run(owner: str):
        return await world.resume(runtime, suspension, owner=owner)

    first, second = await asyncio.gather(run("a"), run("b"))
    statuses = {first.status, second.status}
    assert "settled" in statuses
    assert statuses <= {"settled", "busy", "waiting"}
    assert world.provider.effects == 1
    conversation = runtime.conversation_store.load("task-m6")
    assert conversation is not None and len(conversation) == 2
    assert runtime.m6_metrics is not None
    assert runtime.m6_metrics.tool_results_committed == 3
    assert runtime.m6_metrics.duplicate_provider_side_effects == 0


async def test_m6_20_approval_store_has_no_raw_secret(tmp_path: Path) -> None:
    world = M6World(tmp_path / "world")
    runtime = await _ready(world)
    secret = "TOP-SECRET-BODY-42"
    suspension = await world.suspend(
        runtime,
        [
            world.dispatch_call(
                0,
                "call",
                "email.send",
                {"to": "ops@example.test", "body": secret, "subject": "secret subject"},
            )
        ],
    )
    challenge = runtime.approval_store.list_for_suspension(suspension.suspension_id)[0]
    serialized = json.dumps(challenge.trusted_review_projection, sort_keys=True)
    assert secret not in serialized
    assert "secret subject" not in serialized
    assert "body_digest" in serialized
    # Exact input remains available only in protected runtime state.
    assert suspension.pending_invocations[0].canonical_input["body"] == secret
    raw_db = (world.root / "approvals.db").read_bytes()
    assert secret.encode() not in raw_db


async def test_m6_27_terminal_run_ignores_late_approval(tmp_path: Path) -> None:
    world = M6World(tmp_path / "world")
    runtime = await _ready(world)
    suspension = await world.suspend(runtime, _abc(world, _workspace_file(world)))
    runtime.suspension_coordinator.mark_terminal_run(  # type: ignore[union-attr]
        "run-m6", "CANCELLED", "cancelled while paused"
    )
    world.approve(runtime, suspension)
    outcome = await world.resume(runtime, suspension)
    assert outcome.status == "terminal_run"
    assert world.provider.effects == 0
    assert runtime.action_ledger.list() == ()
    assert runtime.m6_metrics is not None
    assert runtime.m6_metrics.late_approval_resumes == 0
    assert runtime.m6_metrics.late_approval_attempts == 1


async def test_m6_32_unsupported_suspension_schema_fails_closed(tmp_path: Path) -> None:
    world = M6World(tmp_path / "world")
    runtime = await _ready(world)
    suspension = await world.suspend(runtime, _abc(world, _workspace_file(world)))
    payload = suspension.model_dump(mode="json")
    payload["schema_version"] = 999
    with runtime.suspension_store._connection() as connection:  # noqa: SLF001
        connection.execute(
            "UPDATE execution_suspensions SET payload=? WHERE suspension_id=?",
            (json.dumps(payload), suspension.suspension_id),
        )
    with pytest.raises(UnsupportedSuspensionSchema):
        runtime.suspension_store.get(suspension.suspension_id)


async def test_m6_normal_allow_path_is_unchanged(tmp_path: Path) -> None:
    world = M6World(tmp_path / "world")
    runtime = await _ready(world)
    target = _workspace_file(world)
    calls = [
        world.call(0, "read", "file_read", {"path": str(target)}),
        world.dispatch_call(1, "docs", "docs.search", {"query": "checkout"}),
    ]
    plan = await runtime.preflight_batch(calls, world.context())
    assert plan.requires_human is False
    # The normal M4/AnyCode path remains the execution path for the no-human case.
    from anycode.types import BatchToolCall

    results = await runtime.executor.execute_batch(
        [
            BatchToolCall(id="read", name="file_read", input={"path": str(target)}),
            BatchToolCall(
                id="docs",
                name="dispatch_action",
                input={"tool_id": "docs.search", "arguments": {"query": "checkout"}},
            ),
        ],
        world.context(),
    )
    assert results["read"].is_error is not True
    assert results["docs"].is_error is not True
    assert world.calls["docs.search"] == 1
    assert world.provider.effects == 0


async def test_m6_14_approval_creation_is_idempotent(tmp_path: Path) -> None:
    world = M6World(tmp_path / "world")
    runtime = await _ready(world)
    suspension = await world.suspend(runtime, _email_call(world))
    assert runtime.suspension_coordinator is not None
    runtime.suspension_coordinator.reconcile(suspension)
    first = runtime.approval_store.list_for_suspension(suspension.suspension_id)
    runtime.suspension_coordinator.reconcile(suspension)
    second = runtime.approval_store.list_for_suspension(suspension.suspension_id)
    assert len(first) == len(second) == 1
    assert first[0].approval_id == second[0].approval_id
    assert runtime.m6_metrics is not None
    assert runtime.m6_metrics.approval_challenges_created == 1
    assert runtime.m6_metrics.approval_create_dedupes >= 1


async def test_m6_19_approval_does_not_create_m4_until_resume(tmp_path: Path) -> None:
    world = M6World(tmp_path / "world")
    runtime = await _ready(world)
    suspension = await world.suspend(runtime, _email_call(world))
    world.approve(runtime, suspension)
    assert world.provider.effects == 0
    assert runtime.action_ledger.list() == ()
    assert runtime.conversation_store.load("task-m6") is None
    outcome = await world.resume(runtime, suspension)
    assert outcome.status == "settled"
    assert world.provider.effects == 1
    assert len(runtime.action_ledger.list()) == 1


async def test_m6_28_model_cannot_approve_itself(tmp_path: Path) -> None:
    world = M6World(tmp_path / "world")
    runtime = await _ready(world)
    assert not runtime.registry.has("approval.approve")
    assert not runtime.registry.has("approval.reject")
    attempted = await runtime.executor.execute(
        "approval.approve", {"approval_id": "x"}, world.context()
    )
    assert attempted.is_error is True
    assert "not registered" in attempted.data
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ReviewerIdentity(actor="model:planner")
    suspension = await world.suspend(runtime, _email_call(world))
    assert (
        runtime.approval_store.list_pending(suspension_id=suspension.suspension_id)[0].state
        == "PENDING"
    )
    assert runtime.m6_metrics is not None
    assert runtime.m6_metrics.model_self_approval_attempts == 0


async def test_m6_22_m4_conflict_is_preserved_and_not_retried(tmp_path: Path) -> None:
    world = M6World(tmp_path / "world")
    runtime = await _ready(world)
    suspension = await world.suspend(runtime, _email_call(world, "original"))
    world.approve(runtime, suspension)
    # Simulate a prior execution that claimed the same idempotency key with different input.
    assert runtime.durable_bridge is not None
    prior = await runtime.durable_bridge.execute(
        "email.send",
        {"to": "ops@example.test", "body": "different"},
        world.context(),
        idempotency_key="scope:0",
    )
    assert prior.is_error is not True
    effects_before = world.provider.effects
    assert effects_before == 1
    outcome = await world.resume(runtime, suspension)
    assert outcome.status == "settled"
    assert world.provider.effects == effects_before
    assert runtime.m6_metrics is not None
    assert runtime.m6_metrics.m4_conflicts >= 1
    detail = runtime.conversation_store.load("task-m6")[1]["content"][0]  # type: ignore[index]
    assert detail["is_error"] is True


async def test_m6_23_runtime_resume_precedence_over_checkpoint(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from anycode.types import TextBlock

    from atlasos.config import AtlasConfig, ToolsConfig
    from atlasos.runtime import AtlasRuntime, SuspensionResumeRequired
    from atlasos.suspension.models import (
        assistant_message_digest,
        derive_suspension_id,
    )
    from atlasos.workflow import AtlasWorkflow

    held = LLMMessage(role="assistant", content=[TextBlock(text="held")])
    digest = assistant_message_digest(held.content)
    now = datetime.now(UTC)
    suspension = ExecutionSuspension(
        suspension_id=derive_suspension_id(
            run_id="run-precedence",
            response_identity="run:run-precedence:task:t:turn:1",
            assistant_response_digest=digest,
        ),
        run_id="run-precedence",
        task_id="task",
        assistant_response_digest=digest,
        held_assistant_message=held.model_dump(mode="json"),
        pending_invocations=(),
        trusted_identity_digest="0" * 64,
        response_identity="run:run-precedence:task:t:turn:1",
        created_at=now,
        updated_at=now,
    )
    store = SQLiteSuspensionStore(tmp_path / "precedence.db")
    store.save_if_absent(suspension)
    runtime = AtlasRuntime(
        AtlasConfig(tools=ToolsConfig(mode="progressive")),
        suspension_store=store,
        approval_store=SQLiteApprovalStore(tmp_path / "precedence-approvals.db"),
    )
    assert runtime.active_suspensions("run-precedence")[0].suspension_id == suspension.suspension_id
    with pytest.raises(SuspensionResumeRequired):
        await runtime.resume(
            "run-precedence",
            AtlasWorkflow(name="w", agents=(), tasks=()),
        )


async def test_m6_29_approval_has_no_timeout_auto_approve(tmp_path: Path) -> None:
    world = M6World(tmp_path / "world")
    runtime = await _ready(world)
    suspension = await world.suspend(
        runtime,
        [world.dispatch_call(0, "call", "email.send", {"to": "ops@example.test", "body": "x"})],
    )
    # There is no expiry engine; a pending challenge remains PENDING.
    challenge = runtime.approval_store.list_pending(suspension_id=suspension.suspension_id)[0]
    assert challenge.state == "PENDING"
    outcome = await world.resume(runtime, suspension)
    assert outcome.status == "waiting"
    assert world.provider.effects == 0
    assert runtime.m6_metrics is not None
    assert runtime.m6_metrics.timeout_auto_approvals == 0
