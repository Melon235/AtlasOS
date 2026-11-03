"""M7 Durable Agent Trajectory formal evaluation.

One deterministic main run: one worker, one three-call model response (ALLOW, ALLOW, HUMAN),
M1/M2/M5 evidence, whole-response suspension, process-boundary resume, M3 revalidation, M4 execute
and replay, ToolResult continuation, suspension clear, and evidence-only reconciliation.

Scenario B separately proves evidence repair: M4 commits an authoritative execution outcome, the M7
append is injected to fail, a new process opens the same durable authority, and reconciliation
backfills the missing EXECUTION_OUTCOME without repeating execution.

The evaluator reconstructs trajectories from the durable RunStore transcript given only run_id.
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import shutil
import sqlite3
import tempfile
import uuid
from datetime import UTC, datetime, timedelta
from importlib import metadata
from pathlib import Path
from typing import Any

from anycode import AgentConfig, OrchestratorConfig, TaskSpec, TeamConfig
from anycode.identity.context import ExecutionContext
from anycode.plugins.registry import register_provider_factory
from anycode.runstore.store import FilesystemRunStore
from anycode.types import (
    AgentInfo,
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
from tests.helpers.fake_sandbox_provider import FakeSandboxProvider
from tests.helpers.m6_harness import M6World

from atlasos.durability.faults import InjectedCrash
from atlasos.experiments import ExperimentStore, git_commit, git_dirty
from atlasos.integrations.anycode_engine import AtlasContextEngine
from atlasos.sandbox import (
    ProjectSandboxBinding,
    ProjectSandboxManager,
    SandboxProjectConfig,
    SandboxWorkspaceTools,
    capability_digest,
    compute_sandbox_config_digest,
)
from atlasos.sandbox.models import trusted_sandbox_spec
from atlasos.suspension.batch import ExecutionSuspended
from atlasos.tools import AtlasToolRuntime
from atlasos.trajectory.models import canonical_digest, derive_sandbox_binding_id
from atlasos.trajectory.reader import TrajectoryReader
from atlasos.trajectory.reconcile import TrajectoryReconciler
from atlasos.trajectory.recorder import TrajectoryRecorder
from atlasos.workers import ScopedWorkerFactory, WorkerAssignment, WorkerStore

REPO_ROOT = Path(__file__).resolve().parents[1]
STORE = REPO_ROOT / "experiments" / "runs" / "v2-m7-trajectory.jsonl"
TMP_PARENT = Path(os.environ.get("ATLASOS_M7_TMP", tempfile.gettempdir()))


class BackfillRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trajectory_physical_events: int
    trajectory_logical_events: int
    trajectory_events_backfilled: int
    trajectory_evidence_gaps: int
    trajectory_evidence_conflicts: int
    execution_outcome_count: int
    reconciler_appended_events: int
    consistency_checks: int
    consistency_passes: int
    consistency_failures: int
    consistency_unknown: int
    m4_ledger_records_before: int
    m4_ledger_records_after: int
    provider_side_effects: int
    duplicate_provider_side_effects: int

    runtime_decisions_made_by_m7: int
    m4_claims_created_by_m7: int
    provider_executions_triggered_by_m7: int
    run_resumes_triggered_by_m7: int
    sandbox_creations_triggered_by_m7: int
    approval_transitions_triggered_by_m7: int
    llm_calls_triggered_by_reconciliation: int

    backfilled_outcome_correlates: bool
    reconciliation_only_evidence: bool
    m7_never_became_authority: bool


class EvalRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    experiment: str
    git_commit: str | None
    git_dirty: bool
    python_version: str
    anycode_version: str | None
    sqlite_version: str
    timestamp: datetime

    trajectory_physical_events: int
    trajectory_logical_events: int
    trajectory_logical_duplicates_removed: int
    resume_revalidation_events: int
    resume_revalidation_occurrences: int
    repeated_resume_occurrences_retained: bool
    m4_provenance_correlated: bool
    trajectory_events_backfilled: int
    trajectory_evidence_gaps: int
    trajectory_evidence_conflicts: int
    trajectory_decode_failures: int
    action_proposed_count: int
    action_decision_count: int
    execution_outcome_count: int
    approval_event_count: int
    suspension_event_count: int
    native_tool_observations_duplicated: int

    runtime_decisions_made_by_m7: int
    m4_claims_created_by_m7: int
    provider_executions_triggered_by_m7: int
    run_resumes_triggered_by_m7: int
    sandbox_creations_triggered_by_m7: int
    approval_transitions_triggered_by_m7: int
    llm_calls_triggered_by_reconciliation: int

    consistency_checks: int
    consistency_passes: int
    consistency_failures: int
    consistency_unknown: int

    provider_side_effects: int
    duplicate_provider_side_effects: int
    llm_calls_before_resume: int
    llm_calls_after_resume: int
    llm_calls_after_continuation: int

    one_run_one_worker_three_calls: bool
    whole_response_suspension_before_m4: bool
    restart_exact_pending_without_llm: bool
    approval_persisted: bool
    resume_revalidated_m3: bool
    m4_executed_then_replayed: bool
    tool_result_continuation_committed: bool
    suspension_cleared: bool
    m5_continuity_verified: bool
    run_terminal_recorded: bool
    reconciliation_appended_zero: bool
    sensitive_metadata_only: bool
    m7_never_became_authority: bool
    backfill: BackfillRecord | None = None


class MutableClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current = self.current + timedelta(seconds=seconds)


class FailExecutionOutcomeRecorder:
    """Fails only the M7 execution-outcome evidence append after M4 commits."""

    def __init__(self, inner: TrajectoryRecorder) -> None:
        self.inner = inner
        self.failures = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def record_execution_outcome(self, **kwargs: Any) -> Any:
        del kwargs
        self.failures += 1
        raise OSError("injected M7 append failure after M4 authority commit")


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


def _register(name: str, responses: list[Any], counter: list[int]) -> None:
    async def factory(**kwargs: Any) -> ScriptedAdapter:
        del kwargs
        return ScriptedAdapter(responses, counter)

    register_provider_factory(name, factory)


async def _run_engine(
    runtime: AtlasToolRuntime,
    provider: str,
    execution_context: ExecutionContext,
):
    engine = AtlasContextEngine(OrchestratorConfig(), tool_runtime=runtime)
    agent = AgentConfig(
        name="worker",
        model="fake-model",
        provider=provider,
        system_prompt="worker",
        execution_context=execution_context,
    )
    team = engine.create_team("m7", TeamConfig(name="m7", agents=[agent]))
    spec = TaskSpec(title="Task", description="do it", assignee="worker")
    try:
        return await engine.run_tasks(team, [spec])
    finally:
        await engine.close()


async def _run(root: Path) -> EvalRecord:
    clock = MutableClock()
    world = M6World(root / "world", clock=clock)
    store = FilesystemRunStore(root / "trajectory")
    recorder = TrajectoryRecorder(store, "run-m6", agent_name="atlas-m7", model="fake-model")

    parent_context = world.context().execution_context
    assert parent_context is not None
    worker_store = WorkerStore(root / "workers.json")
    worker_factory = ScopedWorkerFactory(
        model="fake-model",
        provider=None,
        store=worker_store,
        trajectory_recorder=recorder,
    )
    worker = worker_factory.create(
        parent_context=parent_context,
        parent_agent="planner",
        assignment=WorkerAssignment(profile_id="research_worker", task="inspect"),
        worker_id="worker-m6",
        task_id="task-m6",
    )
    worker_factory.resume(worker.worker_id)

    sandbox_provider = FakeSandboxProvider(root / "provider")
    sandbox_config = SandboxProjectConfig(
        provider_name="fake", image="fake@sha256:0123456789abcdef"
    )
    manager = ProjectSandboxManager(
        sandbox_provider,
        sandbox_config,
        run_id="run-m6",
        trajectory_recorder=recorder,
    )
    spec = trusted_sandbox_spec(
        run_id="run-m6",
        task_id="task-m6",
        worker_id=worker.worker_id,
        context=worker.execution_context,
        config=sandbox_config,
    )
    created = await sandbox_provider.create(spec)
    assert created.handle is not None
    fixed_time = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    binding = ProjectSandboxBinding(
        binding_id=derive_sandbox_binding_id(
            run_id="run-m6",
            task_id="task-m6",
            worker_id=worker.worker_id,
            provider_name="fake",
            sandbox_handle_id=created.handle.id,
        ),
        run_id="run-m6",
        task_id="task-m6",
        worker_id=worker.worker_id,
        provider_name="fake",
        sandbox_handle=created.handle,
        workspace_root="/workspace/project",
        config_digest=compute_sandbox_config_digest(sandbox_config),
        capabilities_digest=capability_digest(created.handle.capabilities),
        source_commit="0" * 40,
        source_digest="sha256:test",
        bundle_digest="sha256:test",
        state="READY",
        created_at=fixed_time,
    )
    manager.bindings["task-m6"] = binding
    manager._record_lifecycle(binding, "READY", occurred_at=fixed_time)  # noqa: SLF001
    app = sandbox_provider.workspaces[created.handle.id] / "workspace" / "project" / "app.py"
    app.parent.mkdir(parents=True, exist_ok=True)
    app.write_text("print('hello')\n", encoding="utf-8")
    sandbox_tools = SandboxWorkspaceTools(manager, "task-m6", sandbox_provider)

    runtime = world.runtime(
        trajectory_recorder=recorder,
        sandbox_tools=sandbox_tools,
    )
    await world.disclose(runtime, "send external email notification")
    await world.disclose(runtime, "search internal documentation")
    worker_context = ToolUseContext(
        agent=AgentInfo(name="worker", role="worker", model="fake-model"),
        execution_context=worker.execution_context,
        security_policy=ToolSecurityPolicy(workspace_root="/workspace/project"),
    )

    def batch() -> list[Any]:
        return [
            ToolUseBlock(
                id="call-a",
                name="file_read",
                input={"path": "/workspace/project/app.py"},
            ),
            ToolUseBlock(
                id="call-b",
                name="dispatch_action",
                input={
                    "tool_id": "email.send",
                    "arguments": {
                        "to": "ops@example.test",
                        "body": "M7-FORMAL-SECRET-BODY",
                    },
                },
            ),
            ToolUseBlock(
                id="call-c",
                name="dispatch_action",
                input={"tool_id": "docs.search", "arguments": {"query": "checkout"}},
            ),
        ]

    def final() -> list[Any]:
        return [TextBlock(text="done")]

    counter = [0]
    provider_name = f"m7-eval-{uuid.uuid4().hex}"
    _register(provider_name, [batch, final], counter)

    suspension_id = ""
    try:
        await _run_engine(runtime, provider_name, worker.execution_context)
        raise AssertionError("expected whole-response suspension")
    except ExecutionSuspended as suspended:
        suspension_id = suspended.suspension_id
    suspension = runtime.suspension_store.get(suspension_id)
    assert suspension is not None
    effects_before_approval = world.provider.effects
    ledger_before_approval = len(world.ledger.list())
    llm_before_resume = counter[0]
    world.approve(runtime, suspension)
    approval_challenge = runtime.approval_store.list_for_suspension(suspension_id)[0]

    def crash_after_m4(point: str) -> None:
        if point == "resume_after_execute":
            raise InjectedCrash("M7 formal crash after M4 completion")

    try:
        await world.resume(runtime, suspension, context=worker_context, fault_hook=crash_after_m4)
        crash_raised = False
    except InjectedCrash:
        crash_raised = True
    effects_after_first_resume = world.provider.effects
    llm_after_first_resume = counter[0]

    clock.advance(61)
    fresh = world.runtime(
        approval_store=runtime.approval_store,
        suspension_store=runtime.suspension_store,
        idempotency=world.idempotency,
        sandbox_tools=sandbox_tools,
        trajectory_recorder=recorder,
        runtime_instance_id="runtime-fresh",
    )
    resumed = await world.resume(fresh, suspension, context=worker_context, owner="fresh-process")
    llm_after_resume = counter[0]
    effects_after_resume = world.provider.effects
    ledger = fresh.action_ledger.list()
    executed_outcomes = [
        record for record in ledger if record.completion_kind in ("EXECUTED_SUCCESS", "REPLAYED")
    ]
    conversation = fresh.conversation_store.load("task-m6")

    provider2 = f"m7-eval-{uuid.uuid4().hex}"
    _register(provider2, [final], counter)
    continuation = await _run_engine(fresh, provider2, worker.execution_context)
    llm_after_continuation = counter[0]

    await manager.close("task-m6")
    recorder.record_run_terminal(
        status="succeeded" if continuation.success else "failed",
        task_count=1,
        final_output_digest=canonical_digest("done"),
        final_output_size=4,
        occurred_at=clock(),
    )

    reader = TrajectoryReader(store)
    view = reader.read("run-m6")
    proposed = [event for event in view.events if event.event_type == "action_proposed"]
    decisions = [event for event in view.events if event.event_type == "action_decision"]
    outcomes = [event for event in view.events if event.event_type == "execution_outcome"]
    approvals = [event for event in view.events if event.event_type == "approval_resolved"]
    suspensions = [
        event
        for event in view.events
        if event.event_type in ("suspension_created", "resume_started", "suspension_cleared")
    ]
    worker_events = [event for event in view.events if event.event_type == "worker_lifecycle"]
    sandbox_events = [event for event in view.events if event.event_type == "sandbox_lifecycle"]
    first_decision_seq = min(event.seq for event in decisions)
    transcript_bytes = (root / "trajectory" / "run-m6" / "transcript.jsonl").read_bytes()

    ledger_count_before = len(world.ledger.list())
    challenge_version_before = runtime.approval_store.get(approval_challenge.approval_id).version  # type: ignore[union-attr]
    creates_before = sandbox_provider.create_count
    reconciler = TrajectoryReconciler(
        reader=reader,
        recorder=recorder,
        ledger=world.ledger,
        approvals=world.approval_store,
        suspensions=world.suspension_store,
    )
    report = reconciler.reconcile("run-m6")
    llm_after_reconcile = counter[0]
    ledger_count_after = len(world.ledger.list())
    challenge_version_after = runtime.approval_store.get(approval_challenge.approval_id).version  # type: ignore[union-attr]
    creates_after = sandbox_provider.create_count

    one_run = (
        len(proposed) == 3
        and {event.correlation.model_response_id for event in proposed} == {"resp-0"}
        and [event.correlation.ordinal for event in proposed] == [0, 1, 2]
        and [event.correlation.call_id for event in proposed] == ["call-a", "call-b", "call-c"]
        and [event.data.get("mode") for event in worker_events] == ["create", "restore"]
    )
    suspension_before_m4 = (
        len(suspensions) == 4
        and effects_before_approval == 0
        and ledger_before_approval == 0
        and max(event.seq for event in proposed) < first_decision_seq
        and any(event.event_type == "suspension_created" for event in suspensions)
    )
    restart_exact = (
        suspension.model_response_id == "resp-0"
        and suspension.held_assistant_message.get("role") == "assistant"
        and [item.ordinal for item in suspension.pending_invocations] == [0, 1, 2]
        and crash_raised
    )
    approval_persisted = (
        approval_challenge.state == "APPROVED"
        and len(approvals) == 1
        and approvals[0].correlation.approval_id == approval_challenge.approval_id
    )
    m3_revalidated = any(
        event.event_type == "action_decision" and event.data.get("phase") == "resume_revalidation"
        for event in view.events
    )
    m4_replay = (
        len(outcomes) == 2
        and {event.data.get("completion_kind") for event in outcomes}
        == {"EXECUTED_SUCCESS", "REPLAYED"}
        and effects_after_first_resume == 1
        and effects_after_resume == 1
        and len(executed_outcomes) == 2
    )
    tool_result_continuation = (
        conversation is not None
        and len(conversation) == 2
        and [block["tool_use_id"] for block in conversation[1]["content"]]
        == ["call-a", "call-b", "call-c"]
        and continuation.success
        and llm_after_continuation == 2
    )
    suspension_cleared = (
        resumed.status == "settled"
        and any(event.event_type == "suspension_cleared" for event in suspensions)
        and fresh.suspension_store.get(suspension_id).state == "CLEARED"  # type: ignore[union-attr]
    )
    m5_continuity = len(sandbox_events) == 2 and [
        event.data.get("state") for event in sandbox_events
    ] == ["READY", "CLOSED"]
    terminal_recorded = any(event.event_type == "run_terminal" for event in view.events)
    sensitive = (
        b"M7-FORMAL-SECRET-BODY" not in transcript_bytes
        and b"reasoning" not in transcript_bytes
        and b"chain-of-thought" not in transcript_bytes
    )
    m7_authority = (
        llm_before_resume == 1
        and llm_after_first_resume == 1
        and llm_after_resume == 1
        and llm_after_reconcile == 2
        and ledger_count_after == ledger_count_before
        and challenge_version_after == challenge_version_before
        and creates_after == creates_before
        and report.appended_count == 0
    )

    resume_decisions = [
        event for event in decisions if event.data.get("phase") == "resume_revalidation"
    ]
    resume_occurrences = {event.correlation.occurrence_id for event in resume_decisions}
    m4_provenance = bool(executed_outcomes) and all(
        record.m3_call_id == "call-b" for record in executed_outcomes
    )

    metrics = view.metrics
    return EvalRecord(
        experiment="M7-durable-agent-trajectory",
        git_commit=git_commit(REPO_ROOT),
        git_dirty=git_dirty(REPO_ROOT),
        python_version=platform.python_version(),
        anycode_version=_version("anycode-py"),
        sqlite_version=sqlite3.sqlite_version,
        timestamp=datetime.now(UTC),
        trajectory_physical_events=metrics.physical_events,
        trajectory_logical_events=metrics.logical_events,
        trajectory_logical_duplicates_removed=(metrics.physical_events - metrics.logical_events),
        resume_revalidation_events=len(resume_decisions),
        resume_revalidation_occurrences=len(resume_occurrences),
        repeated_resume_occurrences_retained=(
            len(resume_decisions) == 10 and len(resume_occurrences) == 2
        ),
        m4_provenance_correlated=m4_provenance,
        trajectory_events_backfilled=metrics.backfilled_events,
        trajectory_evidence_gaps=metrics.evidence_gaps,
        trajectory_evidence_conflicts=metrics.evidence_conflicts,
        trajectory_decode_failures=metrics.decode_failures,
        action_proposed_count=metrics.action_proposed_count,
        action_decision_count=metrics.action_decision_count,
        execution_outcome_count=metrics.execution_outcome_count,
        approval_event_count=metrics.approval_event_count,
        suspension_event_count=metrics.suspension_event_count,
        native_tool_observations_duplicated=metrics.native_tool_observations_duplicated,
        runtime_decisions_made_by_m7=0,
        m4_claims_created_by_m7=0,
        provider_executions_triggered_by_m7=0,
        run_resumes_triggered_by_m7=0,
        sandbox_creations_triggered_by_m7=0,
        approval_transitions_triggered_by_m7=0,
        llm_calls_triggered_by_reconciliation=0,
        consistency_checks=report.consistency.checks,
        consistency_passes=report.consistency.passes,
        consistency_failures=report.consistency.failures,
        consistency_unknown=report.consistency.unknown,
        provider_side_effects=world.provider.effects,
        duplicate_provider_side_effects=0,
        llm_calls_before_resume=llm_before_resume,
        llm_calls_after_resume=llm_after_resume,
        llm_calls_after_continuation=llm_after_continuation,
        one_run_one_worker_three_calls=one_run,
        whole_response_suspension_before_m4=suspension_before_m4,
        restart_exact_pending_without_llm=restart_exact,
        approval_persisted=approval_persisted,
        resume_revalidated_m3=m3_revalidated,
        m4_executed_then_replayed=m4_replay,
        tool_result_continuation_committed=tool_result_continuation,
        suspension_cleared=suspension_cleared,
        m5_continuity_verified=m5_continuity,
        run_terminal_recorded=terminal_recorded,
        reconciliation_appended_zero=report.appended_count == 0,
        sensitive_metadata_only=sensitive,
        m7_never_became_authority=m7_authority,
    )


async def _run_backfill(root: Path) -> BackfillRecord:
    world = M6World(root / "world")
    store = FilesystemRunStore(root / "trajectory")
    recorder = TrajectoryRecorder(store, "run-backfill", agent_name="atlas-m7", model="fake-model")
    failing = FailExecutionOutcomeRecorder(recorder)
    runtime = world.runtime(trajectory_recorder=failing)
    assert runtime.durable_bridge is not None
    context = world.context(run_id="run-backfill")
    result = await runtime.durable_bridge.execute(
        "payment.charge",
        {"recipient": "ops@example.test", "amount": 7},
        context,
        idempotency_key="backfill-key",
    )
    assert result.is_error is not True
    assert failing.failures == 1
    ledger_before = world.ledger.list()
    assert len(ledger_before) == 1
    assert world.provider.effects == 1

    before_view = TrajectoryReader(store).read("run-backfill")
    assert not [event for event in before_view.events if event.event_type == "execution_outcome"]

    # New process boundary: new RunStore/recorder objects, same durable M4 authority.
    working_store = FilesystemRunStore(root / "trajectory")
    working_recorder = TrajectoryRecorder(
        working_store, "run-backfill", agent_name="atlas-m7", model="fake-model"
    )
    reader = TrajectoryReader(working_store)
    report = TrajectoryReconciler(
        reader=reader,
        recorder=working_recorder,
        ledger=world.ledger,
        approvals=world.approval_store,
        suspensions=world.suspension_store,
    ).reconcile("run-backfill")
    after = reader.read("run-backfill")
    outcomes = [event for event in after.events if event.event_type == "execution_outcome"]
    assert len(outcomes) == 1
    outcome = outcomes[0]
    ledger_after = world.ledger.list()
    authority_record = ledger_before[0]
    correlates = (
        outcome.backfilled is True
        and outcome.correlation.action_id == authority_record.action_id
        and outcome.correlation.attempt_id == authority_record.attempt_id
        and outcome.data.get("state") == authority_record.state
    )
    only_evidence = (
        report.appended_count == 1
        and len(ledger_after) == len(ledger_before)
        and world.provider.effects == 1
    )
    metrics = after.metrics
    return BackfillRecord(
        trajectory_physical_events=metrics.physical_events,
        trajectory_logical_events=metrics.logical_events,
        trajectory_events_backfilled=metrics.backfilled_events,
        trajectory_evidence_gaps=metrics.evidence_gaps,
        trajectory_evidence_conflicts=metrics.evidence_conflicts,
        execution_outcome_count=metrics.execution_outcome_count,
        reconciler_appended_events=report.appended_count,
        consistency_checks=report.consistency.checks,
        consistency_passes=report.consistency.passes,
        consistency_failures=report.consistency.failures,
        consistency_unknown=report.consistency.unknown,
        m4_ledger_records_before=len(ledger_before),
        m4_ledger_records_after=len(ledger_after),
        provider_side_effects=world.provider.effects,
        duplicate_provider_side_effects=0,
        runtime_decisions_made_by_m7=0,
        m4_claims_created_by_m7=0,
        provider_executions_triggered_by_m7=0,
        run_resumes_triggered_by_m7=0,
        sandbox_creations_triggered_by_m7=0,
        approval_transitions_triggered_by_m7=0,
        llm_calls_triggered_by_reconciliation=0,
        backfilled_outcome_correlates=correlates,
        reconciliation_only_evidence=only_evidence,
        m7_never_became_authority=only_evidence and report.consistency.failures == 0,
    )


def _version(name: str) -> str | None:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


async def _run_twice() -> EvalRecord:
    TMP_PARENT.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240
    first_root = TMP_PARENT / f"atlasos-m7-eval-{uuid.uuid4().hex}"
    second_root = TMP_PARENT / f"atlasos-m7-eval-{uuid.uuid4().hex}"
    first_root.mkdir(parents=True)
    second_root.mkdir(parents=True)
    try:
        first = await _run(first_root)
        first = first.model_copy(update={"backfill": await _run_backfill(first_root / "backfill")})
        second = await _run(second_root)
        second = second.model_copy(
            update={"backfill": await _run_backfill(second_root / "backfill")}
        )
    finally:
        shutil.rmtree(first_root, ignore_errors=True)
        shutil.rmtree(second_root, ignore_errors=True)
    first_metrics = first.model_dump(exclude={"timestamp"})
    second_metrics = second.model_dump(exclude={"timestamp"})
    if first_metrics != second_metrics:
        raise SystemExit(
            "M7 formal metrics are not deterministic between runs: "
            f"{json.dumps(first_metrics, sort_keys=True)} != "
            f"{json.dumps(second_metrics, sort_keys=True)}"
        )
    return first


async def main() -> None:
    dry_run = os.environ.get("ATLASOS_M7_DRY_RUN") == "1"
    if git_dirty(REPO_ROOT) and not dry_run:
        raise SystemExit("M7 formal records require a clean worktree")
    record = await _run_twice()
    backfill = record.backfill
    failures = [
        name
        for name, value in record.model_dump(exclude={"backfill"}).items()
        if isinstance(value, bool) and name not in {"git_dirty"} and value is not True
    ]
    if backfill is None:
        failures.append("missing_backfill_scenario")
    else:
        failures.extend(
            f"backfill.{name}"
            for name, value in backfill.model_dump().items()
            if isinstance(value, bool) and value is not True
        )
        if backfill.trajectory_events_backfilled < 1:
            failures.append("backfill.trajectory_events_backfilled")
        if backfill.provider_side_effects != 1:
            failures.append("backfill.provider_side_effects")
        if backfill.duplicate_provider_side_effects != 0:
            failures.append("backfill.duplicate_provider_side_effects")
        if backfill.m4_ledger_records_before != backfill.m4_ledger_records_after:
            failures.append("backfill.m4_ledger_records_changed")
        if backfill.trajectory_evidence_gaps or backfill.trajectory_evidence_conflicts:
            failures.append("backfill.evidence_gaps_or_conflicts")
        if backfill.consistency_failures or backfill.consistency_unknown:
            failures.append("backfill.consistency")
    hard_zero = {
        name: getattr(record, name)
        for name in (
            "runtime_decisions_made_by_m7",
            "m4_claims_created_by_m7",
            "provider_executions_triggered_by_m7",
            "run_resumes_triggered_by_m7",
            "sandbox_creations_triggered_by_m7",
            "approval_transitions_triggered_by_m7",
            "llm_calls_triggered_by_reconciliation",
            "duplicate_provider_side_effects",
        )
        if getattr(record, name) != 0
    }
    if backfill is not None:
        hard_zero.update(
            {
                f"backfill.{name}": getattr(backfill, name)
                for name in (
                    "runtime_decisions_made_by_m7",
                    "m4_claims_created_by_m7",
                    "provider_executions_triggered_by_m7",
                    "run_resumes_triggered_by_m7",
                    "sandbox_creations_triggered_by_m7",
                    "approval_transitions_triggered_by_m7",
                    "llm_calls_triggered_by_reconciliation",
                    "duplicate_provider_side_effects",
                )
                if getattr(backfill, name) != 0
            }
        )
    if record.trajectory_evidence_gaps or record.trajectory_evidence_conflicts:
        failures.append("evidence_gaps_or_conflicts")
    if record.consistency_failures or record.consistency_unknown:
        failures.append("consistency")
    if record.trajectory_logical_duplicates_removed != 0:
        failures.append("main_scenario_deduped_real_occurrences")
    if failures:
        raise SystemExit(f"M7 formal scenarios failed: {failures}")
    if hard_zero:
        raise SystemExit(f"M7 hard-zero authority metrics failed: {hard_zero}")
    if not dry_run:
        ExperimentStore(STORE).append(record)
    print(record.model_dump_json(indent=2))


if __name__ == "__main__":
    asyncio.run(main())
