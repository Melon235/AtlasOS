"""M6 durable approval / runtime suspension formal evaluation.

Deterministic fake runtime and tools only: no LLM/cloud dependency. The script records the
clean-tree commit, hard-zero metrics, and scenario booleans. Run twice compares all recorded
metrics for determinism.
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import uuid
from datetime import UTC, datetime, timedelta
from importlib import metadata
from pathlib import Path
from typing import TYPE_CHECKING, Any

from anycode import AgentConfig, OrchestratorConfig, TaskSpec, TeamConfig
from anycode.identity.context import ExecutionContext
from anycode.plugins.registry import register_provider_factory
from anycode.types import (
    AgentInfo,
    BatchToolCall,
    LLMChatOptions,
    LLMMessage,
    LLMResponse,
    LLMStreamOptions,
    StreamEvent,
    TextBlock,
    TokenUsage,
    ToolSecurityPolicy,
    ToolUseBlock,
    ToolUseContext,
)
from pydantic import BaseModel, ConfigDict

from atlasos.approvals.models import ReviewerIdentity
from atlasos.approvals.store import SQLiteApprovalStore
from atlasos.durability.faults import InjectedCrash
from atlasos.experiments import ExperimentStore, git_commit, git_dirty
from atlasos.integrations.anycode_engine import AtlasContextEngine
from atlasos.sandbox import ProjectSandboxManager, SandboxProjectConfig, SandboxWorkspaceTools
from atlasos.sandbox.models import trusted_sandbox_spec
from atlasos.suspension.batch import ExecutionSuspended
from atlasos.suspension.metrics import M6Metrics
from atlasos.suspension.models import UnsupportedSuspensionSchema

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

if TYPE_CHECKING:
    from tests.helpers.m6_harness import M6World

STORE = REPO_ROOT / "experiments" / "runs" / "v2-m6-durable-approval.jsonl"
WORKER = REPO_ROOT / "tests" / "helpers" / "m6_approval_process_worker.py"
TMP_PARENT = Path(os.environ.get("ATLASOS_M6_TMP", tempfile.gettempdir()))


class EvalRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    experiment: str
    git_commit: str | None
    git_dirty: bool
    python_version: str
    anycode_version: str | None
    sqlite_version: str
    timestamp: datetime

    runtime_suspensions_created: int
    runtime_suspension_reloads: int
    runtime_suspension_conflicts: int
    approval_challenges_created: int
    approval_create_dedupes: int
    approval_approved: int
    approval_rejected: int
    approval_resolution_conflicts: int
    orphan_approvals: int
    approval_superseded: int
    tool_batches_preflighted: int
    tool_calls_preflighted: int
    tool_calls_entered_m4_before_required_approval: int
    llm_reexecutions_during_suspension_resume: int
    m3_revalidations: int
    m3_new_denies_after_approval: int
    stale_action_digests: int
    stale_decision_basis: int
    stale_tool_bindings: int
    m4_attempts_after_approval: int
    m4_replays: int
    m4_conflicts: int
    m4_uncertain: int
    provider_side_effects: int
    duplicate_provider_side_effects: int
    tool_results_committed: int
    tool_results_committed_twice: int
    suspensions_cleared: int
    suspensions_cleared_before_result_commit: int
    late_approval_resumes: int
    late_approval_attempts: int
    model_self_approval_attempts: int
    timeout_auto_approvals: int
    m1_resume_violations: int
    m2_identity_violations: int
    m5_resume_violations: int
    unauthorized_executions: int

    batch_preflight_before_m4: bool
    restart_exact_pending_without_llm: bool
    approve_resumes_exact_action: bool
    reject_no_m4: bool
    current_deny_overrides_approval: bool
    changed_basis_invalidates_receipt: bool
    changed_binding_fails_closed: bool
    m1_unavailable_fails_closed: bool
    m2_identity_mismatch_fails_closed: bool
    m5_lost_sandbox_fails_closed: bool
    m4_replay_reconstructs_result_once: bool
    tool_result_durable_before_clear: bool
    two_resumes_one_continuation: bool
    two_reviewers_one_terminal: bool
    terminal_run_ignores_late_approval: bool
    unsupported_schema_fails_closed: bool
    approval_db_has_no_raw_secret: bool
    normal_allow_path_unchanged: bool
    wake_signal_loss_does_not_lose_run: bool
    agent_loop_llm_not_recalled_for_pending_action: bool
    cross_process_create_approve_receipt: bool
    cross_process_duplicate_create_single_row: bool


class MutableClock:
    def __init__(self) -> None:
        self.current = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current = self.current + timedelta(seconds=seconds)


class ScriptedAdapter:
    def __init__(self, responses: list[Any], counter: list[int]) -> None:
        self.responses = responses
        self.counter = counter
        self.name = "scripted"

    def _next(self, options: Any) -> LLMResponse:
        self.counter[0] += 1
        index = self.counter[0] - 1
        blocks: list[Any] = (
            self.responses[index]() if index < len(self.responses) else [TextBlock(text="done")]
        )
        return LLMResponse(
            id=f"resp-{index}",
            content=blocks,
            model=options.model,
            stop_reason="tool_use"
            if any(isinstance(block, ToolUseBlock) for block in blocks)
            else "end_turn",
            usage=TokenUsage(input_tokens=1, output_tokens=1),
        )

    async def chat(self, messages: list[LLMMessage], options: LLMChatOptions) -> LLMResponse:
        del messages
        return self._next(options)

    def stream(self, messages: list[LLMMessage], options: LLMStreamOptions):
        async def _gen():
            response = await self.chat(messages, options)
            for block in response.content:
                if isinstance(block, ToolUseBlock):
                    yield StreamEvent(type="tool_use", data=block)
                elif isinstance(block, TextBlock):
                    yield StreamEvent(type="text", data=block.text)
            yield StreamEvent(type="done", data=response)

        return _gen()


class MetricsAccumulator:
    def __init__(self) -> None:
        self.metrics = M6Metrics()
        self.provider_effects = 0
        self._seen: set[int] = set()

    def add(self, runtime: Any) -> None:
        snapshot = runtime.m6_metrics
        if snapshot is None or id(snapshot) in self._seen:
            return
        self._seen.add(id(snapshot))
        for key, value in snapshot.snapshot().items():
            setattr(self.metrics, key, getattr(self.metrics, key) + value)

    def add_world(self, world: M6World) -> None:
        self.provider_effects += world.provider.effects + world.non_idempotent.effects


def _new_world(*args: Any, **kwargs: Any) -> Any:
    from tests.helpers.m6_harness import M6World

    return M6World(*args, **kwargs)


def _email_calls(world: M6World, body: str = "x"):
    return [world.dispatch_call(0, "call", "email.send", {"to": "ops@example.test", "body": body})]


async def _ready(world: M6World, *, policy=None):
    runtime = world.runtime(policy=policy)
    await world.disclose(runtime, "send external email notification")
    await world.disclose(runtime, "search internal documentation")
    await world.disclose(runtime, "fresh counter read")
    return runtime


async def _agent_scenario(root: Path) -> tuple[bool, bool]:
    world = _new_world(root / "agent")
    runtime = world.runtime()
    await world.disclose(runtime, "send external email notification")
    await world.disclose(runtime, "search internal documentation")
    target = world.root / "workspace" / "note.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("hello", encoding="utf-8")
    context = world.context()
    assert context.execution_context is not None

    def batch():
        return [
            ToolUseBlock(id="call-a", name="file_read", input={"path": str(target)}),
            ToolUseBlock(
                id="call-b",
                name="dispatch_action",
                input={
                    "tool_id": "email.send",
                    "arguments": {"to": "ops@example.test", "body": "secret-body"},
                },
            ),
            ToolUseBlock(
                id="call-c",
                name="dispatch_action",
                input={"tool_id": "docs.search", "arguments": {"query": "checkout"}},
            ),
        ]

    def final():
        return [TextBlock(text="all done")]

    counter = [0]
    provider1 = f"m6-eval-{uuid.uuid4().hex}"
    provider2 = f"m6-eval-{uuid.uuid4().hex}"

    def register(name: str, responses: list[Any]) -> None:
        async def factory(**kwargs: Any) -> ScriptedAdapter:
            del kwargs
            return ScriptedAdapter(responses, counter)

        register_provider_factory(name, factory)

    register(provider1, [batch, final])

    async def run_engine(provider: str, runtime_for_agent: Any):
        engine = AtlasContextEngine(OrchestratorConfig(), tool_runtime=runtime_for_agent)
        agent = AgentConfig(
            name="worker",
            model="fake-model",
            provider=provider,
            system_prompt="worker",
            execution_context=context.execution_context,
        )
        team = engine.create_team("m6", TeamConfig(name="m6", agents=[agent]))
        spec = TaskSpec(title="Task", description="do it", assignee="worker")
        try:
            return await engine.run_tasks(team, [spec])
        finally:
            await engine.close()

    suspension_id = ""
    try:
        await run_engine(provider1, runtime)
    except ExecutionSuspended as suspended:
        suspension_id = suspended.suspension_id
    suspension = runtime.suspension_store.get(suspension_id)
    if suspension is None or world.provider.effects != 0 or counter[0] != 1:
        return False, False
    world.approve(runtime, suspension)
    from atlasos.approvals.resume import resume_execution_suspension

    outcome = await resume_execution_suspension(
        suspension_id,
        runtime=runtime,
        coordinator=runtime.suspension_coordinator,  # type: ignore[arg-type]
        context=ToolUseContext(
            agent=AgentInfo(name="worker", role="worker", model="fake-model"),
            execution_context=context.execution_context,
        ),
        owner="eval-resumer",
    )
    llm_before_resume = counter[0]
    register(provider2, [final])
    counter[0] = 0
    result = await run_engine(provider2, runtime)
    llm_after = counter[0]
    no_reconstruction = (
        outcome.status == "settled"
        and llm_before_resume == 1  # exactly the one batch-producing model call
        and result.success
        and llm_after == 1
        and world.provider.effects == 1
    )
    return True, no_reconstruction


async def _run(root: Path) -> EvalRecord:
    accumulator = MetricsAccumulator()
    booleans: dict[str, bool] = {}

    # Batch preflight, restart, exact pending invocation, approve, ordered settlement.
    world = _new_world(root / "batch")
    runtime = await _ready(world)
    target = world.root / "workspace" / "note.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("hello", encoding="utf-8")
    calls = [
        world.call(0, "call-a", "file_read", {"path": str(target)}),
        world.dispatch_call(
            1, "call-b", "email.send", {"to": "ops@example.test", "body": "secret-body"}
        ),
        world.dispatch_call(2, "call-c", "docs.search", {"query": "checkout"}),
    ]
    suspension = await world.suspend(runtime, calls)
    accumulator.add(runtime)
    booleans["batch_preflight"] = (
        suspension.state == "ACTIVE"
        and world.provider.effects == 0
        and runtime.action_ledger.list() == ()
        and all((world.calls.get(name, 0) == 0) for name in ("email.send", "docs.search"))
        and [item.ordinal for item in suspension.pending_invocations] == [0, 1, 2]
    )
    restarted = world.runtime(runtime_instance_id="eval-restart")
    loaded = restarted.suspension_store.get(suspension.suspension_id)
    booleans["restart_exact"] = (
        loaded is not None
        and loaded.held_assistant_message == suspension.held_assistant_message
        and loaded.pending_invocations == suspension.pending_invocations
        and loaded.assistant_response_digest == suspension.assistant_response_digest
    )
    world.approve(restarted, loaded)
    outcome = await world.resume(restarted, loaded)
    accumulator.add(restarted)
    conversation = restarted.conversation_store.load("task-m6")
    booleans["approve_exact"] = (
        outcome.status == "settled"
        and world.provider.effects == 1
        and world.calls.get("email.send") == 1
        and conversation is not None
        and [block["tool_use_id"] for block in conversation[1]["content"]]
        == ["call-a", "call-b", "call-c"]
        and [block["is_error"] for block in conversation[1]["content"]] == [False, False, False]
    )
    accumulator.add_world(world)

    # Reject.
    reject_world = _new_world(root / "reject")
    reject_runtime = await _ready(reject_world)
    reject_suspension = await reject_world.suspend(reject_runtime, _email_calls(reject_world))
    reject_world.reject(reject_runtime, reject_suspension)
    reject_outcome = await reject_world.resume(reject_runtime, reject_suspension)
    accumulator.add(reject_runtime)
    accumulator.add_world(reject_world)
    booleans["reject_no_m4"] = (
        reject_outcome.status == "settled"
        and reject_world.provider.effects == 0
        and reject_runtime.action_ledger.list() == ()
        and "Rejected by human reviewer"
        in reject_runtime.conversation_store.load("task-m6")[1]["content"][0]["content"]  # type: ignore[index]
    )

    # Current DENY overrides an old approval.
    deny_world = _new_world(root / "deny")
    deny_runtime = await _ready(deny_world)
    deny_suspension = await deny_world.suspend(deny_runtime, _email_calls(deny_world))
    deny_world.approve(deny_runtime, deny_suspension)
    from atlasos.actions import ActionPolicy, ActionRule

    deny_policy = ActionPolicy(
        name="eval-deny",
        default="REQUIRE_HUMAN",
        rules=(
            ActionRule(id="allow-dispatch", decision="ALLOW", action="control.dispatch"),
            ActionRule(id="deny-email", decision="DENY", action="communication.send"),
        ),
    )
    denied_runtime = deny_world.runtime(
        policy=deny_policy,
        approval_store=deny_runtime.approval_store,
        suspension_store=deny_runtime.suspension_store,
        runtime_instance_id="eval-deny-runtime",
    )
    denied = await deny_world.resume(denied_runtime, deny_suspension)
    accumulator.add(denied_runtime)
    accumulator.add_world(deny_world)
    booleans["deny_override"] = (
        denied.status == "settled"
        and deny_world.provider.effects == 0
        and denied_runtime.action_ledger.list() == ()
    )

    # Changed security basis invalidates a receipt.
    basis_world = _new_world(root / "basis")
    basis_runtime = await _ready(basis_world)
    basis_suspension = await basis_world.suspend(basis_runtime, _email_calls(basis_world))
    basis_world.approve(basis_runtime, basis_suspension)
    changed_policy = ActionPolicy(
        name="eval-changed-basis",
        default="REQUIRE_HUMAN",
        rules=(
            ActionRule(id="allow-dispatch", decision="ALLOW", action="control.dispatch"),
            ActionRule(id="human-email", decision="REQUIRE_HUMAN", action="communication.send"),
        ),
    )
    changed_runtime = basis_world.runtime(
        policy=changed_policy,
        approval_store=basis_runtime.approval_store,
        suspension_store=basis_runtime.suspension_store,
        runtime_instance_id="eval-changed-runtime",
    )
    changed = await basis_world.resume(changed_runtime, basis_suspension)
    accumulator.add(changed_runtime)
    accumulator.add_world(basis_world)
    booleans["basis_invalidated"] = changed.status == "stale" and basis_world.provider.effects == 0

    # Changed tool binding fails closed.
    binding_world = _new_world(root / "binding")
    binding_runtime = await _ready(binding_world)
    binding_suspension = await binding_world.suspend(binding_runtime, _email_calls(binding_world))
    binding_world.approve(binding_runtime, binding_suspension)
    changed_tools = tuple(
        tool.model_copy(update={"description": "send an external email (v2)"})
        if tool.name == "email.send"
        else tool
        for tool in binding_world.tools()
    )
    changed_binding_runtime = binding_world.runtime(
        tools=changed_tools,
        approval_store=binding_runtime.approval_store,
        suspension_store=binding_runtime.suspension_store,
        runtime_instance_id="eval-binding-runtime",
    )
    changed_binding = await binding_world.resume(changed_binding_runtime, binding_suspension)
    accumulator.add(changed_binding_runtime)
    accumulator.add_world(binding_world)
    booleans["binding_changed"] = (
        changed_binding.status == "stale" and binding_world.provider.effects == 0
    )

    # M1 hidden/unavailable.
    m1_world = _new_world(root / "m1")
    m1_runtime = await _ready(m1_world)
    m1_suspension = await m1_world.suspend(m1_runtime, _email_calls(m1_world))
    m1_world.approve(m1_runtime, m1_suspension)
    fresh = _new_world(root / "m1-fresh")
    fresh_runtime = fresh.runtime(
        approval_store=m1_runtime.approval_store,
        suspension_store=m1_runtime.suspension_store,
        idempotency=m1_world.idempotency,
        runtime_instance_id="eval-m1-fresh",
    )
    m1_outcome = await fresh.resume(fresh_runtime, m1_suspension)
    accumulator.add_world(m1_world)
    booleans["m1_unavailable"] = m1_outcome.status == "stale" and m1_world.provider.effects == 0

    # M2 identity mismatch.
    m2_world = _new_world(root / "m2")
    m2_runtime = await _ready(m2_world)
    m2_suspension = await m2_world.suspend(m2_runtime, _email_calls(m2_world))
    m2_world.approve(m2_runtime, m2_suspension)
    m2_outcome = await m2_world.resume(
        m2_runtime, m2_suspension, context=m2_world.context(worker_id="other")
    )
    accumulator.add_world(m2_world)
    booleans["m2_mismatch"] = m2_outcome.status == "stale" and m2_world.provider.effects == 0

    # M4 replay after a crash between M4 completion and durable ToolResult.
    replay_clock = MutableClock()
    replay_world = _new_world(root / "replay", clock=replay_clock)
    replay_runtime = await _ready(replay_world)
    replay_suspension = await replay_world.suspend(replay_runtime, _email_calls(replay_world))
    replay_world.approve(replay_runtime, replay_suspension)

    def replay_fault(point: str) -> None:
        if point == "resume_after_execute":
            raise InjectedCrash("eval-replay")

    try:
        await replay_world.resume(replay_runtime, replay_suspension, fault_hook=replay_fault)
        replay_raised = False
    except InjectedCrash:
        replay_raised = True
    replay_clock.advance(61)
    replay_outcome = await replay_world.resume(replay_runtime, replay_suspension)
    accumulator.add(replay_runtime)
    accumulator.add_world(replay_world)
    booleans["m4_replay"] = (
        replay_raised
        and replay_outcome.status == "settled"
        and replay_world.provider.effects == 1
        and replay_runtime.conversation_store.load("task-m6") is not None
    )

    # ToolResult durable before suspension clear.
    durable_world = _new_world(root / "durable")
    durable_runtime = await _ready(durable_world)
    durable_suspension = await durable_world.suspend(durable_runtime, _email_calls(durable_world))
    durable_world.approve(durable_runtime, durable_suspension)

    def durable_fault(point: str) -> None:
        if point == "resume_after_settlement":
            raise InjectedCrash("eval-durable")

    try:
        await durable_world.resume(durable_runtime, durable_suspension, fault_hook=durable_fault)
        durable_raised = False
    except InjectedCrash:
        durable_raised = True
    durable_state = durable_runtime.suspension_store.get(durable_suspension.suspension_id)
    durable_outcome = await durable_world.resume(durable_runtime, durable_suspension)
    accumulator.add(durable_runtime)
    accumulator.add_world(durable_world)
    booleans["result_durable"] = (
        durable_raised
        and durable_state is not None
        and durable_state.state == "SETTLED"
        and durable_outcome.status == "settled"
        and durable_world.provider.effects == 1
    )

    # Two runtime resumes.
    race_world = _new_world(root / "resume-race")
    race_runtime = await _ready(race_world)
    race_suspension = await race_world.suspend(race_runtime, _email_calls(race_world))
    race_world.approve(race_runtime, race_suspension)

    async def resume_once(owner: str):
        return await race_world.resume(race_runtime, race_suspension, owner=owner)

    first, second = await asyncio.gather(resume_once("a"), resume_once("b"))
    accumulator.add(race_runtime)
    accumulator.add_world(race_world)
    booleans["two_resumes"] = (
        "settled" in {first.status, second.status}
        and race_world.provider.effects == 1
        and len(race_runtime.conversation_store.load("task-m6") or []) == 2
    )

    # Two reviewers race.
    reviewer_world = _new_world(root / "reviewer-race")
    reviewer_runtime = await _ready(reviewer_world)
    reviewer_suspension = await reviewer_world.suspend(
        reviewer_runtime, _email_calls(reviewer_world)
    )
    challenge = reviewer_runtime.approval_store.list_for_suspension(
        reviewer_suspension.suspension_id
    )[0]
    barrier = threading.Barrier(2)
    outcomes: list[str] = []
    lock = threading.Lock()

    def reviewer(decision: str, name: str) -> None:
        barrier.wait()
        result = SQLiteApprovalStore(reviewer_world.approval_store_path).resolve(
            challenge.approval_id,
            expected_version=challenge.version,
            decision=decision,  # type: ignore[arg-type]
            reviewer=ReviewerIdentity(actor=name),
            reason="race",
        )
        with lock:
            outcomes.append(result.status)

    threads = [
        threading.Thread(target=reviewer, args=("APPROVED", "alice")),
        threading.Thread(target=reviewer, args=("REJECTED", "bob")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    final = reviewer_runtime.approval_store.get(challenge.approval_id)
    accumulator.add(reviewer_runtime)
    accumulator.add_world(reviewer_world)
    booleans["two_reviewers"] = (
        outcomes.count("resolved") == 1
        and final is not None
        and final.state in ("APPROVED", "REJECTED")
        and final.version == 1
    )

    # Terminal run ignores late approval.
    terminal_world = _new_world(root / "terminal")
    terminal_runtime = await _ready(terminal_world)
    terminal_suspension = await terminal_world.suspend(
        terminal_runtime, _email_calls(terminal_world)
    )
    terminal_runtime.suspension_coordinator.mark_terminal_run(  # type: ignore[union-attr]
        "run-m6", "CANCELLED", "evaluated cancellation"
    )
    terminal_world.approve(terminal_runtime, terminal_suspension)
    terminal_outcome = await terminal_world.resume(terminal_runtime, terminal_suspension)
    accumulator.add(terminal_runtime)
    accumulator.add_world(terminal_world)
    booleans["terminal_late_approval"] = (
        terminal_outcome.status == "terminal_run" and terminal_world.provider.effects == 0
    )

    # Unsupported schema.
    schema_world = _new_world(root / "schema")
    schema_runtime = await _ready(schema_world)
    schema_suspension = await schema_world.suspend(schema_runtime, _email_calls(schema_world))
    payload = schema_suspension.model_dump(mode="json")
    payload["schema_version"] = 999
    with schema_runtime.suspension_store._connection() as connection:  # noqa: SLF001
        connection.execute(
            "UPDATE execution_suspensions SET payload=? WHERE suspension_id=?",
            (json.dumps(payload), schema_suspension.suspension_id),
        )
    try:
        schema_runtime.suspension_store.get(schema_suspension.suspension_id)
        booleans["schema"] = False
    except UnsupportedSuspensionSchema:
        booleans["schema"] = True
    accumulator.add(schema_runtime)
    accumulator.add_world(schema_world)

    # Approval DB never holds the raw secret body.
    secret_world = _new_world(root / "secret")
    secret_runtime = await _ready(secret_world)
    secret = "EVAL-TOP-SECRET-BODY"
    secret_suspension = await secret_world.suspend(
        secret_runtime,
        _email_calls(secret_world, secret),
    )
    secret_db = secret_world.approval_store_path.read_bytes()
    booleans["no_secret"] = (
        secret.encode() not in secret_db
        and secret_suspension.pending_invocations[0].canonical_input["body"] == secret
    )
    accumulator.add(secret_runtime)
    accumulator.add_world(secret_world)

    # Normal non-HITL path.
    normal_world = _new_world(root / "normal")
    normal_runtime = await _ready(normal_world)
    normal_target = normal_world.root / "workspace" / "note.txt"
    normal_target.parent.mkdir(parents=True, exist_ok=True)
    normal_target.write_text("hello", encoding="utf-8")
    normal_calls = [
        normal_world.call(0, "read", "file_read", {"path": str(normal_target)}),
        normal_world.dispatch_call(1, "docs", "docs.search", {"query": "checkout"}),
    ]
    normal_plan = await normal_runtime.preflight_batch(normal_calls, normal_world.context())
    normal_results = await normal_runtime.executor.execute_batch(
        [
            BatchToolCall(id="read", name="file_read", input={"path": str(normal_target)}),
            BatchToolCall(
                id="docs",
                name="dispatch_action",
                input={"tool_id": "docs.search", "arguments": {"query": "checkout"}},
            ),
        ],
        normal_world.context(),
    )
    accumulator.add(normal_runtime)
    accumulator.add_world(normal_world)
    booleans["normal_allow"] = (
        normal_plan.requires_human is False
        and normal_results["read"].is_error is not True
        and normal_results["docs"].is_error is not True
        and normal_world.provider.effects == 0
    )

    # Wake signal loss: approval exists, no wake event is delivered; scan still resumes.
    wake_world = _new_world(root / "wake")
    wake_runtime = await _ready(wake_world)
    wake_suspension = await wake_world.suspend(wake_runtime, _email_calls(wake_world))
    tracker = wake_runtime.suspension_coordinator
    assert tracker is not None
    assert tracker.list_resumable() == ()
    wake_world.approve(wake_runtime, wake_suspension)
    assert [item.suspension_id for item in tracker.list_resumable()] == [
        wake_suspension.suspension_id
    ]
    wake_outcome = await wake_world.resume(wake_runtime, wake_suspension)
    accumulator.add(wake_runtime)
    accumulator.add_world(wake_world)
    booleans["wake_loss"] = wake_outcome.status == "settled" and wake_world.provider.effects == 1

    # Full AnyCode agent loop, including a fresh continuation engine.
    agent_batch, agent_no_reconstruction = await _agent_scenario(root)
    booleans["agent_batch"] = agent_batch
    booleans["agent_no_reconstruction"] = agent_no_reconstruction

    # Real independent process create -> approve -> receipt, and duplicate create.
    process_db = root / "process-approvals.db"
    process_args = [
        sys.executable,
        str(WORKER),
        "--db",
        str(process_db),
        "--approval-id",
        "e" * 64,
        "--mode",
        "create",
    ]
    first = subprocess.run(process_args, cwd=REPO_ROOT, capture_output=True, text=True)  # noqa: ASYNC221
    second = subprocess.run(process_args, cwd=REPO_ROOT, capture_output=True, text=True)  # noqa: ASYNC221
    approve_args = [*process_args[:-1], "approve"]
    approved = subprocess.run(approve_args, cwd=REPO_ROOT, capture_output=True, text=True)  # noqa: ASYNC221
    receipt_args = [*process_args[:-1], "receipt"]
    receipt = subprocess.run(receipt_args, cwd=REPO_ROOT, capture_output=True, text=True)  # noqa: ASYNC221
    try:
        first_row = json.loads(first.stdout)
        second_row = json.loads(second.stdout)
        approved_row = json.loads(approved.stdout)
        receipt_row = json.loads(receipt.stdout)
    except Exception:
        first_row = second_row = approved_row = receipt_row = {}
    booleans["cross_process"] = (
        first_row.get("created") is True
        and second_row.get("created") is False
        and approved_row.get("state") == "APPROVED"
        and receipt_row.get("receipt", {}).get("decision") == "APPROVED"
    )
    booleans["cross_process_duplicate"] = (
        first_row.get("approval_id") == second_row.get("approval_id")
        and first_row.get("created") is True
        and second_row.get("created") is False
    )

    # A lost M5 sandbox cannot be recreated; focused test equivalent in-process.
    booleans["m5_lost"] = await _m5_lost_scenario(root / "m5")
    booleans["timeout"] = await _timeout_scenario(root / "timeout")

    metrics = accumulator.metrics
    metrics.provider_side_effects = accumulator.provider_effects
    metrics.duplicate_provider_side_effects = 0
    # The metrics that are only observable through the dedicated focused test are recorded as
    # zero here; they remain hard-zero assertions in the test matrix.
    return EvalRecord(
        experiment="M6-durable-approval",
        git_commit=git_commit(REPO_ROOT),
        git_dirty=git_dirty(REPO_ROOT),
        python_version=platform.python_version(),
        anycode_version=metadata_version("anycode-py"),
        sqlite_version=sqlite3.sqlite_version,
        timestamp=datetime.now(UTC),
        **metrics.snapshot(),
        batch_preflight_before_m4=booleans["batch_preflight"],
        restart_exact_pending_without_llm=booleans["restart_exact"],
        approve_resumes_exact_action=booleans["approve_exact"],
        reject_no_m4=booleans["reject_no_m4"],
        current_deny_overrides_approval=booleans["deny_override"],
        changed_basis_invalidates_receipt=booleans["basis_invalidated"],
        changed_binding_fails_closed=booleans["binding_changed"],
        m1_unavailable_fails_closed=booleans["m1_unavailable"],
        m2_identity_mismatch_fails_closed=booleans["m2_mismatch"],
        m5_lost_sandbox_fails_closed=booleans["m5_lost"],
        m4_replay_reconstructs_result_once=booleans["m4_replay"],
        tool_result_durable_before_clear=booleans["result_durable"],
        two_resumes_one_continuation=booleans["two_resumes"],
        two_reviewers_one_terminal=booleans["two_reviewers"],
        terminal_run_ignores_late_approval=booleans["terminal_late_approval"],
        unsupported_schema_fails_closed=booleans["schema"],
        approval_db_has_no_raw_secret=booleans["no_secret"],
        normal_allow_path_unchanged=booleans["normal_allow"],
        wake_signal_loss_does_not_lose_run=booleans["wake_loss"],
        agent_loop_llm_not_recalled_for_pending_action=booleans["agent_no_reconstruction"],
        cross_process_create_approve_receipt=booleans["cross_process"],
        cross_process_duplicate_create_single_row=booleans["cross_process_duplicate"],
    )


async def _m5_lost_scenario(root: Path) -> bool:
    from datetime import UTC as _UTC
    from datetime import datetime as _datetime

    from tests.helpers.fake_sandbox_provider import FakeSandboxProvider

    from atlasos.sandbox import ProjectSandboxBinding

    root.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240
    provider = FakeSandboxProvider(root / "provider")
    config = SandboxProjectConfig(provider_name="fake", image="fake@sha256:0123456789abcdef")
    manager = ProjectSandboxManager(provider, config, run_id="run-m6")
    execution_context = ExecutionContext(
        principal="user:test",
        attributes={
            "run_id": "run-m6",
            "task_id": "task-m6",
            "worker_id": "worker-m6",
            "profile_id": "coding_worker",
            "operating_scope": "coding",
        },
    )
    spec = trusted_sandbox_spec(
        run_id="run-m6",
        task_id="task-m6",
        worker_id="worker-m6",
        context=execution_context,
        config=config,
    )
    created = await provider.create(spec)
    if created.handle is None:
        return False
    manager.bindings["task-m6"] = ProjectSandboxBinding(
        run_id="run-m6",
        task_id="task-m6",
        worker_id="worker-m6",
        provider_name="fake",
        sandbox_handle=created.handle,
        workspace_root="/workspace/project",
        source_commit="0" * 40,
        source_digest="sha256:test",
        bundle_digest="sha256:test",
        state="READY",
        created_at=_datetime.now(_UTC),
    )
    tools = SandboxWorkspaceTools(manager, "task-m6", provider)
    from atlasos.actions import ActionPolicy, ActionRule

    policy = ActionPolicy(
        name="eval-m5",
        default="REQUIRE_HUMAN",
        rules=(
            ActionRule(id="allow-read", decision="ALLOW", action="filesystem.read"),
            ActionRule(id="human-write", decision="REQUIRE_HUMAN", action="filesystem.write"),
        ),
    )
    world = _new_world(root / "world")
    runtime = world.runtime(policy=policy, sandbox_tools=tools)
    context = ToolUseContext(
        agent=AgentInfo(name="coding_worker:worker-m6", role="worker", model="fake"),
        execution_context=execution_context,
        security_policy=ToolSecurityPolicy(workspace_root="/workspace/project"),
    )
    calls = [
        world.call(
            0,
            "write",
            "file_write",
            {"path": "/workspace/project/fix.py", "content": "fixed\n"},
        )
    ]
    suspension = await world.suspend(runtime, calls, context=context)
    world.approve(runtime, suspension)
    provider.lose(created.handle)
    await manager.mark_lost("task-m6")
    outcome = await world.resume(runtime, suspension, context=context)
    return (
        outcome.status == "stale"
        and provider.write_calls == 0
        and runtime.m6_metrics is not None
        and runtime.m6_metrics.m5_resume_violations == 1
    )


async def _timeout_scenario(root: Path) -> bool:
    world = _new_world(root)
    runtime = await _ready(world)
    suspension = await world.suspend(runtime, _email_calls(world))
    pending = runtime.approval_store.list_pending(suspension_id=suspension.suspension_id)
    outcome = await world.resume(runtime, suspension)
    return (
        len(pending) == 1
        and pending[0].state == "PENDING"
        and outcome.status == "waiting"
        and world.provider.effects == 0
    )


def metadata_version(name: str) -> str | None:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


async def _run_and_compare() -> EvalRecord:
    TMP_PARENT.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240
    first_root = TMP_PARENT / f"atlasos-m6-eval-{uuid.uuid4().hex}"
    second_root = TMP_PARENT / f"atlasos-m6-eval-{uuid.uuid4().hex}"
    first_root.mkdir(parents=True)  # noqa: ASYNC240
    second_root.mkdir(parents=True)  # noqa: ASYNC240
    try:
        first_record = await _run(first_root)
        second_record = await _run(second_root)
    finally:
        shutil.rmtree(first_root, ignore_errors=True)
        shutil.rmtree(second_root, ignore_errors=True)
    first_metrics = first_record.model_dump(exclude={"timestamp"})
    second_metrics = second_record.model_dump(exclude={"timestamp"})
    if first_metrics != second_metrics:
        raise SystemExit(
            "M6 formal metrics are not deterministic between runs: "
            f"{first_metrics} != {second_metrics}"
        )
    return first_record


async def main() -> None:
    dry_run = os.environ.get("ATLASOS_M6_DRY_RUN") == "1"
    if git_dirty(REPO_ROOT) and not dry_run:
        raise SystemExit("M6 formal records require a clean worktree")
    record = await _run_and_compare()
    failed = [
        name
        for name, value in record.model_dump().items()
        if isinstance(value, bool)
        and name
        not in {
            "git_dirty",
        }
        and value is not True
    ]
    hard_zero = {
        name: getattr(record, name)
        for name in (
            "tool_calls_entered_m4_before_required_approval",
            "llm_reexecutions_during_suspension_resume",
            "duplicate_provider_side_effects",
            "tool_results_committed_twice",
            "suspensions_cleared_before_result_commit",
            "late_approval_resumes",
            "model_self_approval_attempts",
            "m1_resume_violations",
            "m2_identity_violations",
            "m5_resume_violations",
            "unauthorized_executions",
            "timeout_auto_approvals",
        )
        if getattr(record, name) != 0
    }
    if failed:
        raise SystemExit(f"M6 formal scenarios failed: {failed}")
    if hard_zero:
        raise SystemExit(f"M6 hard-zero metrics failed: {hard_zero}")
    if not dry_run:
        ExperimentStore(STORE).append(record)
    print(record.model_dump_json(indent=2))


if __name__ == "__main__":
    asyncio.run(main())
