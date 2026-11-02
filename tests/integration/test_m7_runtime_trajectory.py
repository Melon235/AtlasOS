"""M7 end-to-end evidence chain across M1-M6, authority boundaries, and failures."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
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
    ToolUseBlock,
    ToolUseContext,
)
from tests.helpers.m6_harness import M6World

from atlasos.approvals.models import ReviewerIdentity
from atlasos.approvals.resume import resume_execution_suspension
from atlasos.config import AtlasConfig, ToolsConfig, TrajectoryConfig
from atlasos.durability.faults import InjectedCrash
from atlasos.experiments import semantic_config_hash
from atlasos.integrations.anycode_engine import AtlasContextEngine
from atlasos.sandbox import (
    ProjectSandboxBinding,
    ProjectSandboxManager,
    SandboxProjectConfig,
)
from atlasos.sandbox.models import trusted_sandbox_spec
from atlasos.suspension.batch import ExecutionSuspended
from atlasos.tools import AtlasToolRuntime
from atlasos.trajectory.models import (
    ApprovalResolvedData,
    ExecutionOutcomeData,
    TrajectoryCorrelation,
    derive_event_id,
)
from atlasos.trajectory.reader import TrajectoryReader
from atlasos.trajectory.reconcile import TrajectoryReconciler
from atlasos.trajectory.recorder import TrajectoryRecorder
from atlasos.workers import ScopedWorkerFactory, WorkerAssignment, WorkerStore


class MutableClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

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


def _workspace_file(world: M6World) -> Path:
    target = world.root / "workspace" / "note.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("hello", encoding="utf-8")
    return target


def _batch(target: Path) -> list[Any]:
    return [
        ToolUseBlock(id="call-a", name="file_read", input={"path": str(target)}),
        ToolUseBlock(
            id="call-b",
            name="dispatch_action",
            input={
                "tool_id": "email.send",
                "arguments": {"to": "ops@example.test", "body": "M7-SECRET-BODY"},
            },
        ),
        ToolUseBlock(
            id="call-c",
            name="dispatch_action",
            input={"tool_id": "docs.search", "arguments": {"query": "checkout"}},
        ),
    ]


async def test_m7_full_agent_chain_and_correlations(tmp_path: Path) -> None:
    world = M6World(tmp_path / "world")
    store = FilesystemRunStore(tmp_path / "trajectory")
    recorder = TrajectoryRecorder(store, "run-m6", agent_name="atlas", model="fake")
    runtime = world.runtime(trajectory_recorder=recorder)
    await world.disclose(runtime, "send external email notification")
    await world.disclose(runtime, "search internal documentation")
    target = _workspace_file(world)
    context = world.context()
    assert context.execution_context is not None
    counter = [0]
    provider = f"m7-{uuid.uuid4().hex}"
    _register(provider, [lambda: _batch(target), lambda: [TextBlock(text="done")]], counter)

    suspension_id = ""
    try:
        await _run_engine(runtime, provider, context.execution_context)
        raise AssertionError("expected suspension")
    except ExecutionSuspended as suspended:
        suspension_id = suspended.suspension_id
    assert runtime.suspension_store is not None
    suspension = runtime.suspension_store.get(suspension_id)
    assert suspension is not None
    assert world.provider.effects == 0
    world.approve(runtime, suspension)
    outcome = await resume_execution_suspension(
        suspension_id,
        runtime=runtime,
        coordinator=runtime.suspension_coordinator,  # type: ignore[arg-type]
        context=ToolUseContext(
            agent=AgentInfo(name="worker", role="worker", model="fake-model"),
            execution_context=context.execution_context,
        ),
        owner="m7-test",
    )
    assert outcome.status == "settled"
    assert world.provider.effects == 1

    view = TrajectoryReader(store).read("run-m6")
    assert view.metrics.evidence_gaps == 0
    assert view.metrics.evidence_conflicts == 0
    assert view.metrics.decode_failures == 0
    proposed = [event for event in view.events if event.event_type == "action_proposed"]
    assert len(proposed) == 3
    assert {event.correlation.model_response_id for event in proposed} == {"resp-0"}
    assert [event.correlation.ordinal for event in proposed] == [0, 1, 2]
    assert [event.correlation.call_id for event in proposed] == [
        "call-a",
        "call-b",
        "call-c",
    ]
    assert len({event.data["input_digest"] for event in proposed}) == 3
    first_decision_seq = min(
        event.seq for event in view.events if event.event_type == "action_decision"
    )
    assert max(event.seq for event in proposed) < first_decision_seq
    suspended = [event for event in view.events if event.event_type == "suspension_created"]
    assert len(suspended) == 1
    assert suspended[0].correlation.suspension_id == suspension_id
    assert suspended[0].correlation.model_response_id == "resp-0"
    approval = [event for event in view.events if event.event_type == "approval_resolved"]
    assert len(approval) == 1
    assert approval[0].correlation.approval_id is not None
    assert approval[0].correlation.call_id == "call-b"
    assert any(event.event_type == "resume_started" for event in view.events)
    outcomes = [event for event in view.events if event.event_type == "execution_outcome"]
    assert len(outcomes) == 1
    assert runtime.action_ledger is not None
    ledger = runtime.action_ledger.list()
    assert len(ledger) == 1
    assert outcomes[0].correlation.action_id == ledger[0].action_id
    assert outcomes[0].correlation.attempt_id == ledger[0].attempt_id
    assert outcomes[0].data["state"] == ledger[0].state
    observations = [event for event in view.events if event.event_type == "tool_observation"]
    assert [event.correlation.call_id for event in observations] == [
        "call-a",
        "call-b",
        "call-c",
    ]
    cleared = [event for event in view.events if event.event_type == "suspension_cleared"]
    assert len(cleared) == 1
    assert cleared[0].correlation.suspension_id == suspension_id
    # No raw model/tool content in the durable trajectory.
    transcript = (tmp_path / "trajectory" / "run-m6" / "transcript.jsonl").read_text(
        encoding="utf-8"
    )
    assert "M7-SECRET-BODY" not in transcript
    assert "secret-body" not in transcript
    assert "reasoning" not in transcript
    assert view.metrics.action_proposed_count == 3
    assert view.metrics.execution_outcome_count == 1
    assert view.metrics.approval_event_count == 1
    assert view.metrics.suspension_event_count == 3


async def test_m7_only_without_m6_records_proposals_and_decisions(tmp_path: Path) -> None:
    world = M6World(tmp_path / "world")
    store = FilesystemRunStore(tmp_path / "trajectory")
    recorder = TrajectoryRecorder(store, "run-m6", agent_name="atlas", model="fake")
    runtime = AtlasToolRuntime(
        "run-m6",
        state_path=world.state_path,
        conversation_path=world.conversation_path,
        external_tools=world.tools(),
        action_policy=world.policy(),
        action_catalog=world.catalog(),
        action_ledger=world.ledger,
        idempotency_store=world.idempotency,
        trajectory_recorder=recorder,
    )
    await world.disclose(runtime, "search internal documentation")
    target = _workspace_file(world)
    context = world.context()
    assert context.execution_context is not None
    counter = [0]
    provider = f"m7-{uuid.uuid4().hex}"
    _register(
        provider,
        [
            lambda: [
                ToolUseBlock(id="a", name="file_read", input={"path": str(target)}),
                ToolUseBlock(
                    id="b",
                    name="dispatch_action",
                    input={"tool_id": "docs.search", "arguments": {"query": "x"}},
                ),
            ],
            lambda: [TextBlock(text="done")],
        ],
        counter,
    )
    result = await _run_engine(runtime, provider, context.execution_context)
    assert result.success
    view = TrajectoryReader(store).read("run-m6")
    assert view.metrics.action_proposed_count == 2
    assert view.metrics.action_decision_count >= 2
    assert view.metrics.execution_outcome_count == 0
    assert [event.event_type for event in view.events].count("tool_observation") == 2
    assert world.provider.effects == 0
    assert world.calls["docs.search"] == 1


async def test_m7_recorder_failure_does_not_change_execution(tmp_path: Path) -> None:
    class FailingStore(FilesystemRunStore):
        def append_event(self, run_id, kind, payload=None):  # noqa: ANN001
            raise OSError("simulated evidence store append failure")

    world = M6World(tmp_path / "world")
    failing_store = FailingStore(tmp_path / "trajectory")
    recorder = TrajectoryRecorder(failing_store, "run-m6")
    runtime = world.runtime(trajectory_recorder=recorder)
    await world.disclose(runtime, "send external email notification")
    suspension = await world.suspend(
        runtime,
        [world.dispatch_call(0, "call", "email.send", {"to": "ops@example.test", "body": "x"})],
        model_response_id="resp-0",
    )
    world.approve(runtime, suspension)
    outcome = await world.resume(runtime, suspension)
    assert outcome.status == "settled"
    assert world.provider.effects == 1
    assert runtime.suspension_store.get(suspension.suspension_id).state == "CLEARED"  # type: ignore[union-attr]
    assert recorder.recording_failures > 0
    assert recorder.last_error is not None


async def test_m7_authority_wins_over_inconsistent_trajectory(tmp_path: Path) -> None:
    world = M6World(tmp_path / "world")
    store = FilesystemRunStore(tmp_path / "trajectory")
    recorder = TrajectoryRecorder(store, "run-m6")
    runtime = world.runtime(trajectory_recorder=recorder)
    await world.disclose(runtime, "send external email notification")
    suspension = await world.suspend(
        runtime,
        [world.dispatch_call(0, "call", "email.send", {"to": "ops@example.test", "body": "x"})],
        model_response_id="resp-0",
    )
    assert runtime.approval_store is not None
    challenge = runtime.approval_store.list_for_suspension(suspension.suspension_id)[0]
    # Plant a trajectory APPROVED fact that contradicts the later authoritative REJECTED CAS.
    fake_id = derive_event_id(
        event_type="approval_resolved",
        run_id="run-m6",
        identity={"approval_id": challenge.approval_id, "version": challenge.version + 10},
    )
    from atlasos.trajectory.codec import encode_envelope
    from atlasos.trajectory.models import TrajectoryEnvelope

    fake = TrajectoryEnvelope(
        event_id=fake_id,
        run_id="run-m6",
        event_type="approval_resolved",
        source="m6",
        correlation=TrajectoryCorrelation(
            approval_id=challenge.approval_id,
            suspension_id=suspension.suspension_id,
            call_id=challenge.call_id,
        ),
        data=ApprovalResolvedData(
            approval_id=challenge.approval_id,
            suspension_id=suspension.suspension_id,
            call_id=challenge.call_id,
            decision="APPROVED",
            version=challenge.version + 10,
            reviewer="host:fake",
        ).model_dump(mode="json"),
    )
    store.append_event("run-m6", "lifecycle", encode_envelope(fake))
    # Authority says REJECTED. Runtime must use the ApprovalStore, not trajectory evidence.
    assert runtime.approval_coordinator is not None
    runtime.approval_coordinator.resolve(
        challenge.approval_id,
        expected_version=challenge.version,
        decision="REJECTED",
        reviewer=ReviewerIdentity(actor="alice"),
        reason="authority rejects",
    )
    outcome = await world.resume(runtime, suspension)
    assert outcome.status == "settled"
    assert world.provider.effects == 0
    assert runtime.action_ledger is not None
    assert runtime.action_ledger.list() == ()
    result = runtime.conversation_store.load("task-m6")[1]["content"][0]  # type: ignore[index]
    assert result["is_error"] is True
    assert "Rejected by human reviewer" in result["content"]
    reader = TrajectoryReader(store)
    report = TrajectoryReconciler(
        reader=reader,
        recorder=recorder,
        ledger=runtime.action_ledger,
        approvals=runtime.approval_store,
        suspensions=runtime.suspension_store,
    ).reconcile("run-m6")
    assert report.consistency.failures >= 1


async def test_m7_m1_m2_m5_seams(tmp_path: Path) -> None:
    world = M6World(tmp_path / "world")
    store = FilesystemRunStore(tmp_path / "trajectory")
    recorder = TrajectoryRecorder(store, "run-m6")
    runtime = world.runtime(trajectory_recorder=recorder)
    await world.disclose(runtime, "search internal documentation")
    worker_store = WorkerStore(tmp_path / "workers.json")
    factory = ScopedWorkerFactory(
        model="fake-model",
        provider=None,
        store=worker_store,
        trajectory_recorder=recorder,
    )
    parent = world.context().execution_context
    assert parent is not None
    worker = factory.create(
        parent_context=parent,
        parent_agent="planner",
        assignment=WorkerAssignment(profile_id="research_worker", task="inspect"),
    )
    factory.resume(worker.worker_id)
    from tests.helpers.fake_sandbox_provider import FakeSandboxProvider

    provider = FakeSandboxProvider(tmp_path / "provider")
    config = SandboxProjectConfig(provider_name="fake", image="fake@sha256:0123456789abcdef")
    manager = ProjectSandboxManager(provider, config, run_id="run-m6", trajectory_recorder=recorder)
    spec = trusted_sandbox_spec(
        run_id="run-m6",
        task_id="task-m6",
        worker_id=worker.worker_id,
        context=parent,
        config=config,
    )
    created = await provider.create(spec)
    assert created.handle is not None
    manager.bindings["task-m6"] = ProjectSandboxBinding(
        run_id="run-m6",
        task_id="task-m6",
        worker_id=worker.worker_id,
        provider_name="fake",
        sandbox_handle=created.handle,
        workspace_root="/workspace/project",
        source_commit="0" * 40,
        source_digest="sha256:test",
        bundle_digest="sha256:test",
        state="READY",
        created_at=datetime.now(UTC),
    )
    manager._record_lifecycle(  # noqa: SLF001
        manager.bindings["task-m6"], "READY", occurred_at=datetime.now(UTC)
    )
    await manager.mark_lost("task-m6")
    view = TrajectoryReader(store).read("run-m6")
    lifecycle = [event for event in view.events if event.event_type == "worker_lifecycle"]
    assert [event.data["mode"] for event in lifecycle] == ["create", "restore"]
    assert [event.data["worker_id"] for event in lifecycle] == [
        worker.worker_id,
        worker.worker_id,
    ]
    sandbox = [event for event in view.events if event.event_type == "sandbox_lifecycle"]
    assert [event.data["state"] for event in sandbox] == ["READY", "LOST"]
    disclosed = [event for event in view.events if event.event_type == "tool_disclosed"]
    assert disclosed
    assert all(event.data["query_digest"] for event in disclosed)


def test_m7_disabled_mode_and_semantic_hash() -> None:
    base = AtlasConfig(tools=ToolsConfig(mode="progressive"))
    enabled = AtlasConfig(
        tools=ToolsConfig(mode="progressive"),
        trajectory=TrajectoryConfig(enabled=True),
    )
    assert base.trajectory.enabled is False
    assert enabled.trajectory.enabled is True
    # Trajectory is evidence, not an execution semantic.
    assert semantic_config_hash(base) == semantic_config_hash(enabled)


async def test_m7_m4_uncertainty_authority_beats_fake_trajectory(tmp_path: Path) -> None:
    world = M6World(tmp_path / "world")
    store = FilesystemRunStore(tmp_path / "trajectory")
    recorder = TrajectoryRecorder(store, "run-m6")
    policy = world.policy(human=("crash.mutate",))
    runtime = world.runtime(policy=policy, trajectory_recorder=recorder)
    await world.disclose(runtime, "crash after external effect")
    suspension = await world.suspend(
        runtime,
        [world.dispatch_call(0, "call", "crash.mutate", {"value": "x"})],
        model_response_id="resp-0",
    )
    world.approve(runtime, suspension)
    outcome = await world.resume(runtime, suspension)
    assert outcome.status == "settled"
    assert world.non_idempotent.effects == 1
    view = TrajectoryReader(store).read("run-m6")
    real = [event for event in view.events if event.event_type == "execution_outcome"][0]
    assert real.data["state"] == "UNCERTAIN"
    # Plant a fake EXECUTED claim with a distinct event id; authority still says UNCERTAIN.
    assert runtime.action_ledger is not None
    ledger = runtime.action_ledger.list()[-1]
    fake_id = derive_event_id(
        event_type="execution_outcome",
        run_id="run-m6",
        identity={
            "action_id": ledger.action_id,
            "attempt_id": ledger.attempt_id,
            "state": "COMPLETED",
            "completion_kind": "EXECUTED_SUCCESS",
            "result_digest": "fake",
        },
    )
    from atlasos.trajectory.codec import encode_envelope
    from atlasos.trajectory.models import TrajectoryEnvelope

    fake = TrajectoryEnvelope(
        event_id=fake_id,
        run_id="run-m6",
        event_type="execution_outcome",
        source="m4",
        correlation=TrajectoryCorrelation(
            action_id=ledger.action_id,
            attempt_id=ledger.attempt_id,
            tool_id=ledger.tool_name,
        ),
        data=ExecutionOutcomeData(
            action_id=ledger.action_id,
            attempt_id=ledger.attempt_id,
            tool_id=ledger.tool_name,
            canonical_action=ledger.canonical_action,
            state="COMPLETED",
            completion_kind="EXECUTED_SUCCESS",
            result_digest="fake",
        ).model_dump(mode="json"),
    )
    store.append_event("run-m6", "lifecycle", encode_envelope(fake))
    report = TrajectoryReconciler(
        reader=TrajectoryReader(store),
        recorder=recorder,
        ledger=runtime.action_ledger,
        approvals=runtime.approval_store,
        suspensions=runtime.suspension_store,
    ).reconcile("run-m6")
    assert report.consistency.failures >= 1


async def test_m7_real_repeated_resume_occurrences_are_preserved(tmp_path: Path) -> None:
    clock = MutableClock()
    world = M6World(tmp_path / "world", clock=clock)
    store = FilesystemRunStore(tmp_path / "trajectory")
    recorder = TrajectoryRecorder(store, "run-m6")
    runtime = world.runtime(trajectory_recorder=recorder)
    await world.disclose(runtime, "send external email notification")
    suspension = await world.suspend(
        runtime,
        [
            world.call(
                0,
                "call-email",
                "email.send",
                {"to": "ops@example.test", "body": "x"},
            )
        ],
        model_response_id="resp-1",
    )
    world.approve(runtime, suspension)

    def crash_after_revalidation(point: str) -> None:
        if point == "resume_after_revalidate":
            raise InjectedCrash("retry after revalidation")

    with pytest.raises(InjectedCrash):
        await world.resume(runtime, suspension, fault_hook=crash_after_revalidation)
    clock.advance(61)
    outcome = await world.resume(runtime, suspension, owner="retry")
    assert outcome.status == "settled"
    assert world.provider.effects == 1

    view = TrajectoryReader(store).read("run-m6")
    decisions = [
        event
        for event in view.events
        if event.event_type == "action_decision" and event.data["phase"] == "resume_revalidation"
    ]
    assert len(decisions) == 2
    assert len({event.correlation.occurrence_id for event in decisions}) == 2
    assert {event.correlation.call_id for event in decisions} == {"call-email"}
    assert sum(len(event.physical_refs) for event in decisions) == 2
    assert all(len(event.physical_refs) == 1 for event in decisions)
    assert view.metrics.evidence_conflicts == 0


async def test_m7_flag_does_not_change_m4_m3_provenance(tmp_path: Path) -> None:
    results: list[tuple[str | None, str, str | None, int]] = []
    for enabled in (False, True):
        world = M6World(tmp_path / f"world-{enabled}")
        recorder = (
            TrajectoryRecorder(
                FilesystemRunStore(world.root / "trajectory"),
                "run-m6",
            )
            if enabled
            else None
        )
        runtime = world.runtime(trajectory_recorder=recorder)
        await world.disclose(runtime, "send external email notification")
        suspension = await world.suspend(
            runtime,
            [
                world.call(
                    0,
                    "call-email",
                    "email.send",
                    {"to": "ops@example.test", "body": "x"},
                )
            ],
        )
        world.approve(runtime, suspension)
        outcome = await world.resume(runtime, suspension)
        assert outcome.status == "settled"
        record = world.ledger.list()[0]
        results.append(
            (
                record.m3_call_id,
                record.state,
                record.completion_kind,
                world.provider.effects,
            )
        )
        if not enabled:
            assert not (world.root / "trajectory").exists()

    assert (
        results[0]
        == results[1]
        == (
            "call-email",
            "COMPLETED",
            "EXECUTED_SUCCESS",
            1,
        )
    )
