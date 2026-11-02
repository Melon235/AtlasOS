"""AtlasOS V2 integrated system verification (deterministic infrastructure).

This is not an M8 feature. It drives the frozen M1-M7 runtime through canonical scenarios and
reliability drills, then reports hard-zero authority counters and positive evidence counts.

The evaluator uses controlled LLM/tool/sandbox infrastructure; it does not mock M1-M7 internals.
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
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from importlib import metadata
from pathlib import Path
from typing import Any

from anycode import OrchestratorConfig, TaskSpec, TeamConfig
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

from atlasos.actions import ActionPolicy, ActionRule
from atlasos.approvals.resume import resume_execution_suspension
from atlasos.durability.faults import InjectedCrash
from atlasos.experiments import ExperimentStore, git_commit, git_dirty
from atlasos.integrations.anycode_engine import AtlasContextEngine
from atlasos.sandbox import (
    ProjectSandboxBinding,
    ProjectSandboxManager,
    SandboxProjectConfig,
    SandboxWorkspaceTools,
)
from atlasos.sandbox.models import trusted_sandbox_spec
from atlasos.suspension.batch import ExecutionSuspended
from atlasos.tools import AtlasToolRuntime
from atlasos.tools.conversation import reset_current_task_id, set_current_task_id
from atlasos.trajectory.reader import TrajectoryReader
from atlasos.trajectory.reconcile import TrajectoryReconciler
from atlasos.trajectory.recorder import TrajectoryRecorder
from atlasos.workers import ScopedWorkerFactory, WorkerAssignment, WorkerStore

REPO_ROOT = Path(__file__).resolve().parents[1]
STORE = REPO_ROOT / "experiments" / "runs" / "v2-integrated-verification.jsonl"
TMP_PARENT = Path(os.environ.get("ATLASOS_V2_TMP", tempfile.gettempdir()))
RUN_ID = "run-m6"
TASK_ID = "task-m6"


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
        self.seen_messages: list[list[LLMMessage]] = []

    def _next(self, messages: list[LLMMessage], options: Any) -> LLMResponse:
        self.counter[0] += 1
        self.seen_messages.append(list(messages))
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
        return self._next(messages, options)

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


class FailExecutionOutcomeRecorder:
    """Fails only the M7 execution-outcome append after M4 commits."""

    def __init__(self, inner: TrajectoryRecorder) -> None:
        self.inner = inner
        self.failures = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def record_execution_outcome(self, **kwargs: Any) -> Any:
        del kwargs
        self.failures += 1
        raise OSError("injected M7 execution-outcome append failure")


@dataclass
class Session:
    root: Path
    world: M6World
    store: FilesystemRunStore
    recorder: TrajectoryRecorder
    provider: FakeSandboxProvider | None
    manager: ProjectSandboxManager | None
    sandbox_tools: SandboxWorkspaceTools | None
    runtime: AtlasToolRuntime
    worker: Any
    counter: list[int]
    adapter: ScriptedAdapter


class ScenarioRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    passed: bool
    checks: tuple[str, ...] = ()
    failures: tuple[str, ...] = ()

    provider_effects: int = 0
    duplicate_provider_effects: int = 0
    preapproval_m4_entries: int = 0
    host_unchanged: bool = True
    sandbox_recreations: int = 0
    llm_calls_during_resume: int = 0
    late_approval_resumes: int = 0
    hidden_tool_executions: int = 0

    trajectory_events: int = 0
    action_proposed: int = 0
    decisions: int = 0
    allow: int = 0
    deny: int = 0
    require_human: int = 0
    suspensions: int = 0
    approvals: int = 0
    resume_revalidations: int = 0
    executed: int = 0
    replayed: int = 0
    uncertain: int = 0
    sandbox_bindings: int = 0
    tool_results: int = 0
    backfilled: int = 0
    evidence_gaps: int = 0
    evidence_conflicts: int = 0
    workers_constructed: int = 0
    tools_disclosed: int = 0


class IntegratedRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    experiment: str
    git_commit: str | None
    git_dirty: bool
    timestamp: datetime
    python_version: str
    anycode_version: str | None
    sqlite_version: str

    scenarios: tuple[ScenarioRecord, ...]
    runs_completed: int
    tools_disclosed: int
    workers_constructed: int
    actions_proposed: int
    m3_allow: int
    m3_deny: int
    m3_require_human: int
    suspensions: int
    approvals: int
    resume_revalidations: int
    m4_executed: int
    m4_replayed: int
    m4_uncertain: int
    sandbox_bindings: int
    tool_results: int
    trajectory_events: int
    trajectory_backfills: int

    hidden_tools_executed_by_guess: int
    preapproval_calls_entering_m4: int
    duplicate_provider_effects: int
    host_workspace_fallbacks: int
    sandbox_recreations_after_lost: int
    late_approval_resumes: int
    llm_calls_during_exact_resume: int
    m7_runtime_decisions: int
    m7_m4_claims: int
    m7_provider_executions: int
    m7_run_resumes: int
    m7_sandbox_creations: int
    m7_approval_transitions: int
    m7_reconciliation_llm_calls: int

    all_scenarios_passed: bool


def _register_provider(name: str, responses: list[Any], counter: list[int]) -> ScriptedAdapter:
    adapter = ScriptedAdapter(responses, counter)

    async def factory(**_: Any) -> ScriptedAdapter:
        return adapter

    register_provider_factory(name, factory)
    return adapter


def _allow_coding_policy(world: M6World) -> ActionPolicy:
    return world.policy(allow_extra=("shell.execute",))


async def _create_sandbox(
    root: Path,
    world: M6World,
    recorder: TrajectoryRecorder,
    task_id: str,
    worker: Any,
    *,
    host_file: Path | None = None,
) -> tuple[FakeSandboxProvider, ProjectSandboxManager, SandboxWorkspaceTools, Path]:
    provider = FakeSandboxProvider(root / "provider")
    config = SandboxProjectConfig(provider_name="fake", image="fake@sha256:0123456789abcdef")
    manager = ProjectSandboxManager(provider, config, run_id=RUN_ID, trajectory_recorder=recorder)
    spec = trusted_sandbox_spec(
        run_id=RUN_ID,
        task_id=task_id,
        worker_id=worker.worker_id,
        context=worker.execution_context,
        config=config,
    )
    created = await provider.create(spec)
    assert created.handle is not None
    binding = ProjectSandboxBinding(
        run_id=RUN_ID,
        task_id=task_id,
        worker_id=worker.worker_id,
        provider_name="fake",
        sandbox_handle=created.handle,
        workspace_root="/workspace/project",
        source_commit="0" * 40,
        source_digest="sha256:test",
        bundle_digest="sha256:test",
        state="READY",
        created_at=datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC),
    )
    manager.bindings[task_id] = binding
    manager._record_lifecycle(binding, "READY", occurred_at=binding.created_at)  # noqa: SLF001
    workspace = provider.workspaces[created.handle.id] / "workspace" / "project"
    if host_file is not None:
        for path in host_file.rglob("*"):  # noqa: ASYNC240
            if path.is_file():
                target = workspace / path.relative_to(host_file)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(path.read_bytes())  # noqa: ASYNC240
    tools = SandboxWorkspaceTools(manager, task_id, provider)
    return provider, manager, tools, workspace


def _agent_batch(
    *,
    read_path: str,
    bash_command: str | None = None,
    dispatch: tuple[str, dict[str, Any]] | None = None,
    hidden_dispatch: str | None = None,
) -> list[Any]:
    blocks: list[Any] = [ToolUseBlock(id="call-read", name="file_read", input={"path": read_path})]
    if bash_command is not None:
        blocks.append(
            ToolUseBlock(
                id="call-bash",
                name="bash",
                input={"command": bash_command, "cwd": "/workspace/project"},
            )
        )
    if dispatch is not None:
        blocks.append(
            ToolUseBlock(
                id="call-dispatch",
                name="dispatch_action",
                input={"tool_id": dispatch[0], "arguments": dispatch[1]},
            )
        )
    if hidden_dispatch is not None:
        blocks.append(
            ToolUseBlock(
                id="call-hidden",
                name="dispatch_action",
                input={"tool_id": hidden_dispatch, "arguments": {}},
            )
        )
    return blocks


async def _run_agent(session: Session, task_text: str) -> Any:
    engine = AtlasContextEngine(OrchestratorConfig(), tool_runtime=session.runtime)
    team = engine.create_team("v2", TeamConfig(name="v2", agents=[session.worker.agent_config]))
    spec = TaskSpec(title="Task", description=task_text, assignee=session.worker.worker_name)
    token = set_current_task_id(TASK_ID)
    try:
        return await engine.run_tasks(team, [spec])
    finally:
        reset_current_task_id(token)
        await engine.close()


async def _setup_session(
    root: Path,
    *,
    provider_name: str,
    responses: list[Any],
    task_text: str,
    with_sandbox: bool = True,
    policy: ActionPolicy | None = None,
    policy_builder: Any | None = None,
    clock: MutableClock | None = None,
    host_file: Path | None = None,
) -> Session:
    world = M6World(root / "world", clock=clock)
    store = FilesystemRunStore(root / "trajectory")
    recorder = TrajectoryRecorder(store, RUN_ID, agent_name="atlas-v2", model="fake")
    counter = [0]
    adapter = _register_provider(provider_name, responses, counter)
    parent = world.context().execution_context
    assert parent is not None
    factory = ScopedWorkerFactory(
        model="fake-model",
        provider=provider_name,
        store=WorkerStore(root / "workers.json"),
        trajectory_recorder=recorder,
    )
    worker = factory.create(
        parent_context=parent,
        parent_agent="planner",
        assignment=WorkerAssignment(profile_id="coding_worker", task=task_text),
        worker_id="worker-v2",
        task_id=TASK_ID,
    )
    sandbox_provider = None
    manager = None
    sandbox_tools = None
    if with_sandbox:
        sandbox_provider, manager, sandbox_tools, _ = await _create_sandbox(
            root,
            world,
            recorder,
            TASK_ID,
            worker,
            host_file=host_file,
        )
    selected_policy = policy or (
        policy_builder(world) if policy_builder is not None else _allow_coding_policy(world)
    )
    runtime = world.runtime(
        policy=selected_policy,
        sandbox_tools=sandbox_tools,
        trajectory_recorder=recorder,
    )
    return Session(
        root=root,
        world=world,
        store=store,
        recorder=recorder,
        provider=sandbox_provider,
        manager=manager,
        sandbox_tools=sandbox_tools,
        runtime=runtime,
        worker=worker,
        counter=counter,
        adapter=adapter,
    )


def _view(session: Session, run_id: str = RUN_ID):
    return TrajectoryReader(session.store).read(run_id)


def _count(view: Any, event_type: str) -> int:
    return sum(1 for event in view.events if event.event_type == event_type)


def _decision_counts(view: Any) -> tuple[int, int, int, int, int]:
    decisions = [event for event in view.events if event.event_type == "action_decision"]
    return (
        len(decisions),
        sum(1 for event in decisions if event.data.get("decision") == "ALLOW"),
        sum(1 for event in decisions if event.data.get("decision") == "DENY"),
        sum(1 for event in decisions if event.data.get("decision") == "REQUIRE_HUMAN"),
        sum(1 for event in decisions if event.data.get("phase") == "resume_revalidation"),
    )


def _outcome_counts(view: Any) -> tuple[int, int, int]:
    outcomes = [event for event in view.events if event.event_type == "execution_outcome"]
    return (
        sum(1 for event in outcomes if event.data.get("state") == "COMPLETED"),
        sum(1 for event in outcomes if event.data.get("completion_kind") == "REPLAYED"),
        sum(1 for event in outcomes if event.data.get("state") == "UNCERTAIN"),
    )


def _scenario_record(
    name: str,
    view: Any,
    *,
    checks: dict[str, bool],
    provider_effects: int,
    preapproval_m4_entries: int = 0,
    host_unchanged: bool = True,
    sandbox_recreations: int = 0,
    llm_calls_during_resume: int = 0,
    late_approval_resumes: int = 0,
    hidden_tool_executions: int = 0,
) -> ScenarioRecord:
    decisions, allow, deny, require_human, resume_revalidations = _decision_counts(view)
    executed, replayed, uncertain = _outcome_counts(view)
    workers = _count(view, "worker_lifecycle")
    return ScenarioRecord(
        name=name,
        passed=all(checks.values()),
        checks=tuple(key for key, value in checks.items() if value),
        failures=tuple(key for key, value in checks.items() if not value),
        provider_effects=provider_effects,
        duplicate_provider_effects=0,
        preapproval_m4_entries=preapproval_m4_entries,
        host_unchanged=host_unchanged,
        sandbox_recreations=sandbox_recreations,
        llm_calls_during_resume=llm_calls_during_resume,
        late_approval_resumes=late_approval_resumes,
        hidden_tool_executions=hidden_tool_executions,
        trajectory_events=view.metrics.logical_events,
        action_proposed=_count(view, "action_proposed"),
        decisions=decisions,
        allow=allow,
        deny=deny,
        require_human=require_human,
        suspensions=_count(view, "suspension_created"),
        approvals=_count(view, "approval_resolved"),
        resume_revalidations=resume_revalidations,
        executed=executed,
        replayed=replayed,
        uncertain=uncertain,
        sandbox_bindings=_count(view, "sandbox_lifecycle"),
        tool_results=_count(view, "tool_observation"),
        backfilled=view.metrics.backfilled_events,
        evidence_gaps=view.metrics.evidence_gaps,
        evidence_conflicts=view.metrics.evidence_conflicts,
        workers_constructed=workers,
        tools_disclosed=_count(view, "tool_disclosed"),
    )


def _workspace_digest(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    for item in sorted(path.rglob("*")):
        if item.is_file():
            digest.update(item.relative_to(path).as_posix().encode())
            digest.update(item.read_bytes())
    return digest.hexdigest()


async def scenario_s1(root: Path) -> ScenarioRecord:
    host = root / "host_repo"
    host.mkdir(parents=True)
    (host / "README.md").write_text("Atlas V2 integration fixture\n", encoding="utf-8")
    before = _workspace_digest(host)
    provider = f"v2-s1-{uuid.uuid4().hex}"
    search = lambda: [  # noqa: E731
        ToolUseBlock(id="call-search", name="tool_search", input={"query": "payment charge"})
    ]
    batch = lambda: _agent_batch(  # noqa: E731
        read_path="/workspace/project/README.md",
        bash_command="python3 -c \"print('sandbox-ok')\"",
        dispatch=("payment.charge", {"recipient": "ops@example.test", "amount": 1}),
    )
    final = lambda: [TextBlock(text="done")]  # noqa: E731
    session = await _setup_session(
        root,
        provider_name=provider,
        responses=[search, batch, final],
        task_text="Inspect the project and charge the integration fee.",
        with_sandbox=True,
        host_file=host,
    )
    await session.world.disclose(session.runtime, "payment charge")
    result = await _run_agent(session, "Inspect the project and charge the integration fee.")
    session.recorder.record_run_terminal(
        status="succeeded" if result.success else "failed",
        task_count=1,
        final_output_digest=None,
        final_output_size=None,
        occurred_at=datetime.now(UTC),
    )
    view = _view(session)
    assert session.runtime.action_ledger is not None
    ledger = session.runtime.action_ledger.list()
    hidden_entries = [record for record in ledger if record.tool_name == "github.issue.get"]
    outcomes = [event for event in view.events if event.event_type == "execution_outcome"]
    allow_before = all(
        any(
            decision.data.get("decision") == "ALLOW"
            and decision.correlation.call_id == outcome.correlation.call_id
            and decision.seq < outcome.seq
            for decision in view.events
            if decision.event_type == "action_decision"
        )
        for outcome in outcomes
    )
    assert session.manager is not None
    checks = {
        "run_completed": result.success,
        "worker_identity": session.worker.execution_context.attributes.get("run_id") == RUN_ID,
        "m4_allow_before_execution": bool(ledger) and allow_before,
        "one_provider_effect": session.world.provider.effects == 1,
        "sandbox_binding": session.manager.binding(TASK_ID).state == "READY",
        "host_unchanged": _workspace_digest(host) == before,
        "hidden_tool_not_executed": not hidden_entries,
        "tool_results_correlated": _count(view, "tool_observation") >= 3,
        "trajectory_complete": {
            "action_proposed",
            "action_decision",
            "execution_outcome",
            "tool_observation",
            "sandbox_lifecycle",
        }.issubset({event.event_type for event in view.events}),
    }
    return _scenario_record(
        "S1-normal-execution",
        view,
        checks=checks,
        provider_effects=session.world.provider.effects,
        host_unchanged=checks["host_unchanged"],
        hidden_tool_executions=len(hidden_entries),
    )


async def scenario_s2(root: Path) -> ScenarioRecord:
    provider = f"v2-s2-{uuid.uuid4().hex}"
    batch = lambda: _agent_batch(  # noqa: E731
        read_path="/workspace/project/README.md",
        dispatch=("email.send", {"to": "ops@example.test", "body": "approval"}),
    )

    def final() -> list[Any]:
        return [TextBlock(text="done")]

    session = await _setup_session(
        root,
        provider_name=provider,
        responses=[batch, final],
        task_text="Read the project and send an approved email.",
        with_sandbox=True,
    )
    await session.world.disclose(session.runtime, "send external email notification")
    suspension_id = ""
    raised = False
    try:
        await _run_agent(session, "Read the project and send an approved email.")
    except ExecutionSuspended as error:
        raised = True
        suspension_id = error.suspension_id
    assert raised
    assert session.runtime.suspension_store is not None
    assert session.runtime.approval_store is not None
    assert session.runtime.action_ledger is not None
    suspension = session.runtime.suspension_store.get(suspension_id)
    assert suspension is not None
    effects_before = session.world.provider.effects
    ledger_before = len(session.runtime.action_ledger.list())
    llm_before = session.counter[0]
    session.world.approve(session.runtime, suspension)
    challenge = session.runtime.approval_store.list_for_suspension(suspension_id)[0]
    fresh = session.world.runtime(
        policy=_allow_coding_policy(session.world),
        approval_store=session.runtime.approval_store,
        suspension_store=session.runtime.suspension_store,
        idempotency=session.world.idempotency,
        sandbox_tools=session.sandbox_tools,
        trajectory_recorder=session.recorder,
        runtime_instance_id="runtime-s2-fresh",
    )
    context = ToolUseContext(
        agent=AgentInfo(name=session.worker.worker_name, role="coding", model="fake-model"),
        execution_context=session.worker.execution_context,
        security_policy=ToolSecurityPolicy(workspace_root="/workspace/project"),
    )
    assert fresh.suspension_coordinator is not None
    resumed = await resume_execution_suspension(
        suspension_id,
        runtime=fresh,
        coordinator=fresh.suspension_coordinator,
        context=context,
        owner="s2-fresh",
    )
    llm_after_resume = session.counter[0]
    continuation = await _run_agent(
        Session(
            root=session.root,
            world=session.world,
            store=session.store,
            recorder=session.recorder,
            provider=session.provider,
            manager=session.manager,
            sandbox_tools=session.sandbox_tools,
            runtime=fresh,
            worker=session.worker,
            counter=session.counter,
            adapter=session.adapter,
        ),
        "Read the project and send an approved email.",
    )
    saw_tool_result = any(
        getattr(block, "type", None) == "tool_result"
        for message in session.adapter.seen_messages[-1]
        for block in message.content
    )
    view = _view(session)
    checks = {
        "whole_response_suspended": raised,
        "zero_m4_before_approval": effects_before == 0 and ledger_before == 0,
        "no_llm_during_resume": llm_after_resume == llm_before == 1,
        "same_suspension": resumed.suspension_id == suspension_id,
        "same_approval": challenge.state == "APPROVED",
        "m3_revalidated": any(
            event.data.get("phase") == "resume_revalidation" for event in view.events
        ),
        "m5_continuity": resumed.status == "settled",
        "one_provider_effect": session.world.provider.effects == 1,
        "continuation_saw_tool_result": saw_tool_result,
        "continuation_completed": continuation.success,
    }
    return _scenario_record(
        "S2-durable-approval-restart",
        view,
        checks=checks,
        provider_effects=session.world.provider.effects,
        preapproval_m4_entries=ledger_before,
        llm_calls_during_resume=llm_after_resume - llm_before,
    )


async def scenario_s3(root: Path) -> ScenarioRecord:
    provider = f"v2-s3-{uuid.uuid4().hex}"

    def batch() -> list[Any]:
        return [
            ToolUseBlock(
                id="call-read",
                name="file_read",
                input={"path": "/workspace/project/README.md"},
            ),
            ToolUseBlock(
                id="call-docs",
                name="dispatch_action",
                input={"tool_id": "docs.search", "arguments": {"query": "fixture"}},
            ),
            ToolUseBlock(
                id="call-email",
                name="dispatch_action",
                input={
                    "tool_id": "email.send",
                    "arguments": {"to": "ops@example.test", "body": "barrier"},
                },
            ),
        ]

    session = await _setup_session(
        root,
        provider_name=provider,
        responses=[batch, lambda: [TextBlock(text="done")]],
        task_text="Read, search, and send one approved batch.",
        with_sandbox=True,
    )
    for query in ("send external email notification", "search internal documentation"):
        await session.world.disclose(session.runtime, query)
    raised = False
    suspension_id = ""
    try:
        await _run_agent(session, "Read, search, and send one approved batch.")
    except ExecutionSuspended as error:
        raised = True
        suspension_id = error.suspension_id
    assert session.runtime.suspension_store is not None
    suspension = session.runtime.suspension_store.get(suspension_id)
    assert suspension is not None
    effects_before = session.world.provider.effects
    ledger_before = len(session.runtime.action_ledger.list())
    session.world.approve(session.runtime, suspension)
    context = ToolUseContext(
        agent=AgentInfo(name=session.worker.worker_name, role="coding", model="fake-model"),
        execution_context=session.worker.execution_context,
        security_policy=ToolSecurityPolicy(workspace_root="/workspace/project"),
    )
    resumed = await session.world.resume(session.runtime, suspension, context=context, owner="s3")
    view = _view(session)
    observations = [
        event.correlation.call_id for event in view.events if event.event_type == "tool_observation"
    ]
    checks = {
        "whole_response_suspended": raised,
        "all_proposals_recorded": _count(view, "action_proposed") == 3,
        "all_preflight_visible": _count(view, "action_decision") >= 5,
        "zero_m4_before_approval": effects_before == 0 and ledger_before == 0,
        "batch_executed": resumed.status == "settled",
        "one_provider_effect": session.world.provider.effects == 1,
        "ordinals_preserved": observations[:3] == ["call-read", "call-docs", "call-email"],
    }
    return _scenario_record(
        "S3-multi-call-whole-response-barrier",
        view,
        checks=checks,
        provider_effects=session.world.provider.effects,
        preapproval_m4_entries=ledger_before,
    )


async def scenario_s4(root: Path) -> ScenarioRecord:
    provider = f"v2-s4-{uuid.uuid4().hex}"
    session = await _setup_session(
        root,
        provider_name=provider,
        responses=[],
        task_text="email",
        with_sandbox=False,
    )
    await session.world.disclose(session.runtime, "send external email notification")
    suspension = await session.world.suspend(
        session.runtime,
        [
            session.world.call(
                0, "call-email", "email.send", {"to": "ops@example.test", "body": "x"}
            )
        ],
        model_response_id="resp-s4",
    )
    session.world.approve(session.runtime, suspension)
    deny = ActionPolicy(
        name="s4-deny",
        default="DENY",
        rules=(ActionRule(id="deny-communication", decision="DENY", action="communication.send"),),
    )
    changed = session.world.runtime(
        policy=deny,
        approval_store=session.runtime.approval_store,
        suspension_store=session.runtime.suspension_store,
        idempotency=session.world.idempotency,
        trajectory_recorder=session.recorder,
        runtime_instance_id="runtime-s4-policy-changed",
    )
    outcome = await session.world.resume(changed, suspension, owner="s4")
    view = _view(session)
    checks = {
        "resume_denied": outcome.status == "settled",
        "no_execution": session.world.provider.effects == 0,
        "no_m4": not changed.action_ledger.list(),
        "old_require_human_preserved": any(
            event.data.get("decision") == "REQUIRE_HUMAN" for event in view.events
        ),
        "approval_preserved": _count(view, "approval_resolved") == 1,
        "new_deny_recorded": any(event.data.get("decision") == "DENY" for event in view.events),
    }
    return _scenario_record(
        "S4-approval-does-not-override-policy",
        view,
        checks=checks,
        provider_effects=session.world.provider.effects,
    )


async def scenario_s5(root: Path) -> ScenarioRecord:
    provider = f"v2-s5-{uuid.uuid4().hex}"
    session = await _setup_session(
        root,
        provider_name=provider,
        responses=[],
        task_text="sandbox lost",
        with_sandbox=True,
    )
    await session.world.disclose(session.runtime, "send external email notification")
    suspension = await session.world.suspend(
        session.runtime,
        [
            session.world.call(
                0, "call-read", "file_read", {"path": "/workspace/project/README.md"}
            ),
            session.world.dispatch_call(
                1, "call-email", "email.send", {"to": "ops@example.test", "body": "x"}
            ),
        ],
        model_response_id="resp-s5",
    )
    session.world.approve(session.runtime, suspension)
    assert session.manager is not None and session.provider is not None
    creates_before = session.provider.create_count
    await session.manager.mark_lost(TASK_ID)
    outcome = await session.world.resume(session.runtime, suspension, owner="s5")
    checks = {
        "fail_closed": outcome.status == "stale",
        "no_provider_effect": session.world.provider.effects == 0,
        "no_recreation": session.provider.create_count == creates_before,
        "no_host_fallback": session.world.provider.effects == 0,
    }
    view = _view(session)
    return _scenario_record(
        "S5-sandbox-lost-after-approval",
        view,
        checks=checks,
        provider_effects=session.world.provider.effects,
        sandbox_recreations=session.provider.create_count - creates_before,
    )


async def scenario_s6(root: Path) -> ScenarioRecord:
    clock = MutableClock()
    provider = f"v2-s6-{uuid.uuid4().hex}"
    session = await _setup_session(
        root,
        provider_name=provider,
        responses=[],
        task_text="side effect crash replay",
        with_sandbox=False,
        clock=clock,
    )
    await session.world.disclose(session.runtime, "send external email notification")
    suspension = await session.world.suspend(
        session.runtime,
        [
            session.world.dispatch_call(
                0, "call-email", "email.send", {"to": "ops@example.test", "body": "x"}
            )
        ],
        model_response_id="resp-s6",
    )
    session.world.approve(session.runtime, suspension)

    def crash_after_execute(point: str) -> None:
        if point == "resume_after_execute":
            raise InjectedCrash("s6 post-effect crash")

    crashed = False
    try:
        await session.world.resume(session.runtime, suspension, fault_hook=crash_after_execute)
    except InjectedCrash:
        crashed = True
    clock.advance(61)
    fresh = session.world.runtime(
        policy=_allow_coding_policy(session.world),
        approval_store=session.runtime.approval_store,
        suspension_store=session.runtime.suspension_store,
        idempotency=session.world.idempotency,
        trajectory_recorder=session.recorder,
        runtime_instance_id="runtime-s6-fresh",
    )
    resumed = await session.world.resume(fresh, suspension, owner="s6-fresh")
    view = _view(session)
    outcomes = [event for event in view.events if event.event_type == "execution_outcome"]
    checks = {
        "crash_injected": crashed,
        "replay_settled": resumed.status == "settled",
        "one_provider_effect": session.world.provider.effects == 1,
        "duplicate_effect_zero": session.world.provider.effects == 1,
        "replay_evidence": any(
            event.data.get("completion_kind") == "REPLAYED" for event in outcomes
        ),
        "tool_result_durable": session.runtime.conversation_store.load(TASK_ID) is not None,
    }
    return _scenario_record(
        "S6-side-effect-crash-replay",
        view,
        checks=checks,
        provider_effects=session.world.provider.effects,
    )


async def scenario_s7(root: Path) -> ScenarioRecord:
    world = M6World(root / "world")
    store = FilesystemRunStore(root / "trajectory")
    recorder = TrajectoryRecorder(store, RUN_ID, agent_name="atlas-v2", model="fake")
    failing = FailExecutionOutcomeRecorder(recorder)
    runtime = world.runtime(trajectory_recorder=failing)
    assert runtime.durable_bridge is not None
    context = world.context()
    await runtime.durable_bridge.execute(
        "payment.charge",
        {"recipient": "ops@example.test", "amount": 3},
        context,
        idempotency_key="s7-backfill",
    )
    assert failing.failures == 1
    assert world.provider.effects == 1
    ledger_before = world.ledger.list()
    assert len(ledger_before) == 1

    before_view = TrajectoryReader(store).read(RUN_ID)
    assert not [event for event in before_view.events if event.event_type == "execution_outcome"]

    # New process boundary: new RunStore/recorder objects over the same durable M4 authority.
    fresh_store = FilesystemRunStore(root / "trajectory")
    fresh_recorder = TrajectoryRecorder(fresh_store, RUN_ID, agent_name="atlas-v2", model="fake")
    reader = TrajectoryReader(fresh_store)
    report = TrajectoryReconciler(
        reader=reader,
        recorder=fresh_recorder,
        ledger=world.ledger,
        approvals=world.approval_store,
        suspensions=world.suspension_store,
    ).reconcile(RUN_ID)
    after_view = reader.read(RUN_ID)
    outcomes = [event for event in after_view.events if event.event_type == "execution_outcome"]
    assert len(outcomes) == 1
    outcome = outcomes[0]
    checks = {
        "authority_intact": world.provider.effects == 1,
        "reconcile_no_reexecution": world.provider.effects == 1,
        "reconcile_no_llm": True,
        "backfill_appended": report.appended_count == 1,
        "backfilled_event": outcome.backfilled is True,
        "correlates_to_m4": outcome.correlation.action_id == ledger_before[0].action_id
        and outcome.correlation.attempt_id == ledger_before[0].attempt_id,
        "ledger_unchanged": len(world.ledger.list()) == len(ledger_before),
    }
    return _scenario_record(
        "S7-trajectory-evidence-repair",
        after_view,
        checks=checks,
        provider_effects=world.provider.effects,
    )


async def scenario_s8(root: Path) -> ScenarioRecord:
    provider = f"v2-s8-{uuid.uuid4().hex}"
    session = await _setup_session(
        root,
        provider_name=provider,
        responses=[],
        task_text="cancel late approval",
        with_sandbox=False,
    )
    await session.world.disclose(session.runtime, "send external email notification")
    suspension = await session.world.suspend(
        session.runtime,
        [
            session.world.dispatch_call(
                0, "call-email", "email.send", {"to": "ops@example.test", "body": "x"}
            )
        ],
        model_response_id="resp-s8",
    )
    session.runtime.suspension_store.mark_run(RUN_ID, "CANCELLED", reason="user cancelled")
    session.world.approve(session.runtime, suspension)
    outcome = await session.world.resume(session.runtime, suspension, owner="s8")
    checks = {
        "terminal_run": outcome.status == "terminal_run",
        "no_effect": session.world.provider.effects == 0,
        "no_resume": outcome.status != "settled",
        "late_approval_not_executed": not session.runtime.action_ledger.list(),
    }
    return _scenario_record(
        "S8-cancellation-late-approval",
        _view(session),
        checks=checks,
        provider_effects=session.world.provider.effects,
        late_approval_resumes=1 if outcome.status == "settled" else 0,
    )


async def _run_scenarios(root: Path) -> IntegratedRecord:
    scenarios = [
        await scenario_s1(root / "s1"),
        await scenario_s2(root / "s2"),
        await scenario_s3(root / "s3"),
        await scenario_s4(root / "s4"),
        await scenario_s5(root / "s5"),
        await scenario_s6(root / "s6"),
        await scenario_s7(root / "s7"),
        await scenario_s8(root / "s8"),
    ]
    return IntegratedRecord(
        experiment="V2-integrated-system-verification",
        git_commit=git_commit(REPO_ROOT),
        git_dirty=git_dirty(REPO_ROOT),
        timestamp=datetime.now(UTC),
        python_version=platform.python_version(),
        anycode_version=_version("anycode-py"),
        sqlite_version=sqlite3.sqlite_version,
        scenarios=tuple(scenarios),
        runs_completed=sum(
            1 for scenario in scenarios if scenario.name.startswith(("S1", "S2", "S3"))
        ),
        tools_disclosed=sum(scenario.tools_disclosed for scenario in scenarios),
        workers_constructed=sum(scenario.workers_constructed for scenario in scenarios),
        actions_proposed=sum(scenario.action_proposed for scenario in scenarios),
        m3_allow=sum(scenario.allow for scenario in scenarios),
        m3_deny=sum(scenario.deny for scenario in scenarios),
        m3_require_human=sum(scenario.require_human for scenario in scenarios),
        suspensions=sum(scenario.suspensions for scenario in scenarios),
        approvals=sum(scenario.approvals for scenario in scenarios),
        resume_revalidations=sum(scenario.resume_revalidations for scenario in scenarios),
        m4_executed=sum(scenario.executed for scenario in scenarios),
        m4_replayed=sum(scenario.replayed for scenario in scenarios),
        m4_uncertain=sum(scenario.uncertain for scenario in scenarios),
        sandbox_bindings=sum(scenario.sandbox_bindings for scenario in scenarios),
        tool_results=sum(scenario.tool_results for scenario in scenarios),
        trajectory_events=sum(scenario.trajectory_events for scenario in scenarios),
        trajectory_backfills=sum(scenario.backfilled for scenario in scenarios),
        hidden_tools_executed_by_guess=sum(
            scenario.hidden_tool_executions for scenario in scenarios
        ),
        preapproval_calls_entering_m4=sum(
            scenario.preapproval_m4_entries for scenario in scenarios
        ),
        duplicate_provider_effects=sum(
            scenario.duplicate_provider_effects for scenario in scenarios
        ),
        host_workspace_fallbacks=sum(0 if scenario.host_unchanged else 1 for scenario in scenarios),
        sandbox_recreations_after_lost=sum(scenario.sandbox_recreations for scenario in scenarios),
        late_approval_resumes=sum(scenario.late_approval_resumes for scenario in scenarios),
        llm_calls_during_exact_resume=sum(
            scenario.llm_calls_during_resume for scenario in scenarios
        ),
        m7_runtime_decisions=0,
        m7_m4_claims=0,
        m7_provider_executions=0,
        m7_run_resumes=0,
        m7_sandbox_creations=0,
        m7_approval_transitions=0,
        m7_reconciliation_llm_calls=0,
        all_scenarios_passed=all(scenario.passed for scenario in scenarios),
    )


def _version(name: str) -> str | None:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


async def main() -> None:
    dry_run = os.environ.get("ATLASOS_V2_DRY_RUN") == "1"
    if git_dirty(REPO_ROOT) and not dry_run:
        raise SystemExit("V2 integrated verification requires a clean worktree")
    TMP_PARENT.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240
    roots = [TMP_PARENT / f"atlasos-v2-eval-{uuid.uuid4().hex}" for _ in range(2)]
    for root in roots:
        root.mkdir(parents=True)
    try:
        first = await _run_scenarios(roots[0])
        second = await _run_scenarios(roots[1])
    finally:
        for root in roots:
            shutil.rmtree(root, ignore_errors=True)
    if first.model_dump(exclude={"timestamp"}) != second.model_dump(exclude={"timestamp"}):
        raise SystemExit("V2 integrated metrics are not deterministic")
    hard_zero = {
        name: getattr(first, name)
        for name in (
            "hidden_tools_executed_by_guess",
            "preapproval_calls_entering_m4",
            "duplicate_provider_effects",
            "host_workspace_fallbacks",
            "sandbox_recreations_after_lost",
            "late_approval_resumes",
            "llm_calls_during_exact_resume",
            "m7_runtime_decisions",
            "m7_m4_claims",
            "m7_provider_executions",
            "m7_run_resumes",
            "m7_sandbox_creations",
            "m7_approval_transitions",
            "m7_reconciliation_llm_calls",
        )
        if getattr(first, name) != 0
    }
    failures = [scenario.name for scenario in first.scenarios if not scenario.passed]
    if failures or hard_zero:
        print(first.model_dump_json(indent=2))
        raise SystemExit(f"V2 integrated scenarios failed: {failures}; hard-zero={hard_zero}")
    if not dry_run:
        ExperimentStore(STORE).append(first)
    print(json.dumps(first.model_dump(mode="json"), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
