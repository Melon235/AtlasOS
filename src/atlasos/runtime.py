"""Thin AtlasOS runtime facade over AnyCode.

AtlasRuntime composes upstream building blocks; it does not schedule tasks, execute a DAG, or
own an agent lifecycle. Its responsibilities are configuration, provider registration, context
projection/selection/rendering (Stage 2 modes), metrics attribution, result normalization from
upstream task state, and experiment persistence.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from anycode import (
    AnyCode,
    CheckpointConfig,
    CheckpointData,
    OrchestratorConfig,
    Task,
    TeamConfig,
    TeamRunResult,
)
from anycode.plugins.registry import register_provider_factory
from anycode.runstore.store import FilesystemRunStore
from anycode.tasks.queue import TaskQueue
from anycode.types import LLMAdapter, OrchestratorEvent, ToolDefinition, ToolUseContext
from pydantic import BaseModel, ConfigDict, Field

from atlasos.approvals.resume import ResumeOutcome, resume_execution_suspension
from atlasos.approvals.store import SQLiteApprovalStore
from atlasos.config import AtlasConfig, load_config
from atlasos.context import (
    ContextBlock,
    InMemoryContextStore,
    NaiveExecutionOrderRenderer,
    PromptRenderer,
    StablePromptRenderer,
    TaskScopedSelector,
)
from atlasos.context.task_prompt import AtlasTaskPromptBuilder
from atlasos.context.transform import (
    ContextPilotDedupTransform,
    ContextTransform,
    IdentityContextTransform,
)
from atlasos.durability import (
    AtlasSessionManifest,
    completed_task_ids,
    read_manifest,
    rebuild_context_store,
    task_ids_with_status,
    verify_compatible,
    workflow_hash,
    write_manifest,
)
from atlasos.experiments import (
    ExperimentStore,
    configuration_hash,
    git_commit,
    git_dirty,
    package_version,
    placed_assignment_hash,
    semantic_config_hash,
)
from atlasos.integrations.anycode_engine import AtlasContextEngine
from atlasos.integrations.deepseek import DeepSeekAdapter
from atlasos.metrics import (
    InferenceObservation,
    InferenceRecord,
    MetricsCollector,
    RunMetrics,
    estimate_cost_for_run,
)
from atlasos.placement import (
    CapabilityOnlyPolicy,
    ContextLocalityPolicy,
    PlacementSummary,
    PlacementWeights,
    StaticPlacementPolicy,
    plan_workflow,
)
from atlasos.suspension.models import ExecutionSuspension
from atlasos.suspension.store import SQLiteSuspensionStore
from atlasos.tools import AtlasToolRuntime, ToolRuntimeMetrics
from atlasos.tools.conversation import set_current_task_id
from atlasos.trajectory.models import canonical_digest
from atlasos.trajectory.recorder import TrajectoryRecorder
from atlasos.workflow import (
    AtlasAgentSpec,
    AtlasTaskSpec,
    AtlasWorkflow,
    CompiledWorkflow,
    TaskStatusName,
    compile_workflow,
    task_description,
)

DEFAULT_RUNS_ROOT = Path("experiments/runs")
SCHEDULER_POLICY = "upstream_default"

AdapterFactory = Callable[[str, Callable[[InferenceObservation], None]], LLMAdapter]
EngineFactory = Callable[[OrchestratorConfig], AnyCode]


@dataclass(frozen=True)
class _RenderInfo:
    """Per-task render metadata captured before the model call."""

    context_layout_hash: str
    rendered_dependency_order: tuple[str, ...]
    dependency_completion_order: tuple[str, ...]
    pre_transform_layout_hash: str | None = None
    post_transform_layout_hash: str | None = None
    transform_name: str = "identity"
    deduplicated: bool = False
    transform_latency_ms: float | None = None
    original_block_count: int | None = None
    optimized_block_count: int | None = None
    original_context_chars: int | None = None
    optimized_context_chars: int | None = None
    stable_prefix_tokens: int | None = None
    context_block_tokens: int | None = None
    dynamic_tail_tokens: int | None = None


class AtlasTaskResult(BaseModel):
    """One task's outcome, reconstructed from upstream task state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    title: str
    agent: str | None = None
    status: TaskStatusName
    output: str | None = None
    created_at: datetime
    updated_at: datetime
    duration_ms: float | None = None


class SuspensionResumeRequired(RuntimeError):
    """Normal checkpoint resume must not bypass an active Runtime suspension."""

    def __init__(self, run_id: str, suspension_ids: tuple[str, ...]) -> None:
        self.run_id = run_id
        self.suspension_ids = suspension_ids
        super().__init__(
            f"run {run_id!r} has active execution suspension(s) {list(suspension_ids)}; "
            "resume the suspension before normal checkpoint recovery"
        )


class RecoveryMetrics(BaseModel):
    """Observations about one resumed session.

    Counts are task-level and are computed from checkpoint state plus the inference records
    emitted by the resumed generation. They are observations, never checkpoint truth.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    resumed: bool = False
    resume_count: int = Field(default=0, ge=0)
    recovery_latency_ms: float | None = Field(default=None, ge=0)

    completed_tasks_before_resume: int = Field(default=0, ge=0)
    completed_tasks_reexecuted: int = Field(default=0, ge=0)

    inflight_tasks_before_resume: int = Field(default=0, ge=0)
    inflight_tasks_reexecuted: int = Field(default=0, ge=0)

    unfinished_tasks_executed_after_resume: int = Field(default=0, ge=0)
    context_blocks_rebuilt: int = Field(default=0, ge=0)


class AtlasRunResult(BaseModel):
    """Stable, experiment-facing representation of one run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    timestamp: datetime
    workflow: str
    experiment: str
    status: Literal["succeeded", "failed"]
    final_output: str
    tasks: list[AtlasTaskResult]
    metrics: RunMetrics
    inference_records: list[InferenceRecord]

    started_at: datetime
    finished_at: datetime
    model: str
    git_commit: str | None = None
    git_dirty: bool = False
    anycode_version: str | None = None
    contextpilot_version: str | None = None
    configuration_hash: str

    # Frozen experiment labels; validated against the configuration at run time.
    scheduler_policy: str = SCHEDULER_POLICY
    context_policy: str = "upstream_default"
    renderer_policy: str = "upstream_default"
    transform_name: str = "identity"
    deduplicated: bool = False
    total_transform_latency_ms: float = 0.0
    contextpilot_enabled: bool = False
    request_sequence: list[str] | None = None
    stress_scale: str | None = None

    # Stage 4 placement planning (pre-placement; not dynamic scheduling).
    placement_policy: str = "bypass"
    placement_weights: dict[str, float] = Field(default_factory=dict)
    placement_decisions: list[dict[str, Any]] = Field(default_factory=list)
    mean_assignment_locality: float | None = None
    weighted_assignment_locality: float | None = None
    total_logical_handoff_refs: int = 0
    total_logical_handoff_chars: int = 0
    dependency_agent_switches: int = 0
    assignment_distribution: dict[str, int] = Field(default_factory=dict)

    # Stage 5 recovery observation (None for uninterrupted runs).
    recovery: RecoveryMetrics | None = None

    # V2 M1 tool-surface/discovery observation (None when progressive tools are disabled).
    tool_runtime: ToolRuntimeMetrics | None = None


def _default_adapter_factory(
    agent_name: str,
    on_call: Callable[[InferenceObservation], None],
) -> LLMAdapter:
    del agent_name  # a single provider configuration serves every agent
    return DeepSeekAdapter.from_env(on_call=on_call)


class AtlasRuntime:
    """Facade that composes AnyCode for reproducible AtlasOS experiments."""

    def __init__(
        self,
        config: AtlasConfig,
        *,
        runs_root: str | Path = DEFAULT_RUNS_ROOT,
        adapter_factory: AdapterFactory | None = None,
        engine_factory: EngineFactory | None = None,
        event_hook: Callable[[OrchestratorEvent], None] | None = None,
        tool_definitions: Sequence[ToolDefinition] = (),
        repo_root: str | Path | None = None,
        suspension_store: SQLiteSuspensionStore | None = None,
        approval_store: SQLiteApprovalStore | None = None,
    ) -> None:
        _validate_runtime_config(config)
        if (suspension_store is None) != (approval_store is None):
            raise ValueError("M6 requires both suspension_store and approval_store")
        if suspension_store is not None and config.tools.mode != "progressive":
            raise ValueError("M6 requires tools.mode='progressive'")
        if config.trajectory.enabled and config.tools.mode != "progressive":
            raise ValueError("M7 requires tools.mode='progressive'")
        self._config = config
        self._suspension_store = suspension_store
        self._approval_store = approval_store
        self._trajectory_store: FilesystemRunStore | None = None
        self._trajectory_recorder: TrajectoryRecorder | None = None
        self._runs_root = Path(runs_root)
        self._adapter_factory = adapter_factory or _default_adapter_factory
        self._engine_factory: EngineFactory = engine_factory or (lambda options: AnyCode(options))
        # Test/experiment-only observation hook for deterministic durable-boundary interruption.
        self._event_hook = event_hook
        self._tool_definitions = tuple(tool_definitions)
        self._tool_runtime: AtlasToolRuntime | None = None
        self._tool_setup_errors: list[str] = []
        self._repo_root = (
            Path(repo_root) if repo_root is not None else Path(__file__).resolve().parents[2]
        )
        self._running = False
        self._run_id = ""
        self._run_dir: Path | None = None
        self._inference_path: Path | None = None
        self._recovered_task_ids: frozenset[str] = frozenset()
        self._title_to_id: dict[str, str] = {}
        self._id_by_title: dict[str, str] = {}
        self._active_task: dict[str, str] = {}
        self._task_assignee: dict[str, str] = {}
        self._effective_specs: dict[str, AtlasTaskSpec] = {}
        self._agents_by_name: dict[str, AtlasAgentSpec] = {}
        self._store: InMemoryContextStore | None = None
        self._selector: TaskScopedSelector | None = None
        self._renderer: PromptRenderer | None = None
        self._transform: ContextTransform | None = None
        self._prompt_builder: AtlasTaskPromptBuilder | None = None
        self._render_info: dict[str, _RenderInfo] = {}

    @classmethod
    def from_config(cls, path: str | Path, **kwargs: Any) -> AtlasRuntime:
        return cls(load_config(path), **kwargs)

    @property
    def config(self) -> AtlasConfig:
        return self._config

    @property
    def tool_runtime(self) -> AtlasToolRuntime | None:
        return self._tool_runtime

    @property
    def suspension_store(self) -> SQLiteSuspensionStore | None:
        return self._suspension_store

    def active_suspensions(self, run_id: str) -> tuple[ExecutionSuspension, ...]:
        if self._suspension_store is None:
            return ()
        return self._suspension_store.active_for_run(run_id)

    async def _setup_tool_runtime(self, run_id: str, run_dir: Path) -> None:
        self._tool_runtime = None
        self._tool_setup_errors = []
        self._trajectory_store = None
        self._trajectory_recorder = None
        if self._config.tools.mode != "progressive":
            return
        if self._config.trajectory.enabled:
            self._trajectory_store = FilesystemRunStore(
                run_dir / self._config.trajectory.store_root
            )
            self._trajectory_recorder = TrajectoryRecorder(
                self._trajectory_store,
                run_id,
                agent_name="atlas-runtime",
                model=self._config.provider.model,
            )
        runtime = AtlasToolRuntime(
            run_id,
            state_path=run_dir / "run_tool_state.json",
            conversation_path=run_dir / "tool_conversations.json",
            external_tools=self._tool_definitions,
            search_backend=self._config.tools.search_backend,
            embedding_model=self._config.tools.embedding_model,
            max_results=self._config.tools.max_results,
            suspension_store=self._suspension_store,
            approval_store=self._approval_store,
            trajectory_recorder=self._trajectory_recorder,
        )
        try:
            await runtime.connect_mcp_servers(self._config.tools.mcp_servers)
        except Exception as error:
            # A missing MCP server must not corrupt an otherwise valid session. Disclosed tool
            # names remain durable and will surface as tool_unavailable on dispatch.
            self._tool_setup_errors.append(str(error))
        self._tool_runtime = runtime

    async def _close_tool_runtime(self) -> None:
        if self._tool_runtime is not None:
            await self._tool_runtime.close()

    async def run(
        self,
        workflow: AtlasWorkflow,
        *,
        experiment: str = "baseline",
        input_text: str | None = None,
        persist: bool = True,
    ) -> AtlasRunResult:
        """Execute one workflow through AnyCode and normalize the outcome."""

        if self._running:
            raise RuntimeError("AtlasRuntime.run is not re-entrant")
        self._running = True
        try:
            return await self._run(
                workflow,
                experiment=experiment,
                input_text=input_text,
                persist=persist,
            )
        finally:
            self._running = False
            await self._close_tool_runtime()

    async def resume(
        self,
        run_id: str,
        workflow: AtlasWorkflow,
        *,
        experiment: str = "baseline",
        persist: bool = True,
    ) -> AtlasRunResult:
        """Resume one interrupted session from its latest durable checkpoint.

        This is the only recovery entry point. Atlas rebuilds its derived ContextStore from
        completed checkpoint tasks *before* AnyCode is asked to resume unfinished work.
        """

        if self._running:
            raise RuntimeError("AtlasRuntime.resume is not re-entrant")
        active = self.active_suspensions(run_id)
        if active:
            raise SuspensionResumeRequired(run_id, tuple(item.suspension_id for item in active))
        self._running = True
        try:
            return await self._resume(
                run_id,
                workflow,
                experiment=experiment,
                persist=persist,
            )
        finally:
            self._running = False
            await self._close_tool_runtime()

    async def resume_suspension(
        self,
        suspension_id: str,
        *,
        context: ToolUseContext,
        owner: str | None = None,
        lease_seconds: float = 60.0,
        commit_continuation: bool = True,
    ) -> ResumeOutcome:
        """Settle one durable suspension through the trusted M6 resume path.

        Normal checkpoint resume never reaches this method's state: it raises
        ``SuspensionResumeRequired`` while an active suspension exists.
        """

        if self._running:
            raise RuntimeError("AtlasRuntime.resume_suspension is not re-entrant")
        if self._suspension_store is None:
            raise RuntimeError("M6 is not configured on this AtlasRuntime")
        suspension = self._suspension_store.get(suspension_id)
        if suspension is None:
            raise RuntimeError(f"no execution suspension {suspension_id!r}")
        run_dir = self._runs_root / suspension.run_id
        self._running = True
        try:
            self._begin_session(suspension.run_id, run_dir)
            await self._setup_tool_runtime(suspension.run_id, run_dir)
            if self._tool_runtime is None or self._tool_runtime.suspension_coordinator is None:
                raise RuntimeError("M6 tool runtime is unavailable for suspension resume")
            return await resume_execution_suspension(
                suspension_id,
                runtime=self._tool_runtime,
                coordinator=self._tool_runtime.suspension_coordinator,
                context=context,
                owner=owner or self._tool_runtime.runtime_instance_id,
                lease_seconds=lease_seconds,
                commit_continuation=commit_continuation,
            )
        finally:
            self._running = False
            await self._close_tool_runtime()

    def context_blocks(self) -> list[ContextBlock]:
        """Observation only: logical blocks currently held by the derived store."""

        if self._store is None:
            return []
        return self._store.all_blocks()

    async def _run(
        self,
        workflow: AtlasWorkflow,
        *,
        experiment: str,
        input_text: str | None,
        persist: bool,
    ) -> AtlasRunResult:
        run_id = uuid.uuid4().hex
        started_at = datetime.now(UTC)
        run_dir = self._runs_root / run_id
        checkpoint_dir = run_dir / "checkpoints"
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        self._begin_session(run_id, run_dir)
        await self._setup_tool_runtime(run_id, run_dir)

        effective = replace(workflow, input=input_text) if input_text is not None else workflow
        effective, placement_decisions, placement_summary = self._place(effective)

        collector = MetricsCollector()
        providers = self._register_providers(effective, run_id, collector, generation=0)
        compiled = compile_workflow(
            effective,
            providers=providers,
            model=self._config.provider.model,
        )
        self._install_workflow(effective, compiled)

        manifest = self._build_manifest(
            run_id=run_id,
            workflow=effective,
            created_at=started_at,
            checkpoint_dir=checkpoint_dir,
        )
        manifest_path = run_dir / "atlas_manifest.json"
        write_manifest(manifest_path, manifest)

        engine = self._create_engine(checkpoint_dir)
        team_result = await self._execute(engine, compiled, resume_from=None)

        return self._finalize(
            run_id=run_id,
            workflow=effective,
            experiment=experiment,
            started_at=started_at,
            collector=collector,
            team_result=team_result,
            checkpoint_dir=checkpoint_dir,
            run_dir=run_dir,
            manifest=manifest,
            manifest_path=manifest_path,
            placement_decisions=placement_decisions,
            placement_summary=placement_summary,
            recovery=None,
            persist=persist,
        )

    async def _resume(
        self,
        run_id: str,
        workflow: AtlasWorkflow,
        *,
        experiment: str,
        persist: bool,
    ) -> AtlasRunResult:
        resume_started = time.perf_counter()
        started_at = datetime.now(UTC)
        run_dir = self._runs_root / run_id
        manifest_path = run_dir / "atlas_manifest.json"
        manifest = read_manifest(manifest_path)
        checkpoint_dir = Path(manifest.checkpoint_location)

        # Reconstruct the effective workflow exactly as the original run did. The manifest owns
        # the run input; structural changes are rejected by the workflow hash below.
        effective = replace(workflow, input=manifest.workflow_input)
        effective, placement_decisions, placement_summary = self._place(effective)

        verify_compatible(
            manifest,
            workflow=effective,
            semantic_config_hash=semantic_config_hash(self._config),
            placed_assignment_hash=placed_assignment_hash(effective),
            placement_policy=self._config.placement.policy,
            context_policy=self._config.context.policy,
            renderer_policy=self._config.context.renderer,
            context_transform=self._config.context.transform.name,
        )

        checkpoint = self._load_checkpoint(checkpoint_dir)
        completed_before = completed_task_ids(checkpoint, effective)
        inflight_before = task_ids_with_status(checkpoint, effective, "in_progress")
        rebuilt = rebuild_context_store(checkpoint, effective)
        rebuilt_block_count = len(rebuilt.all_blocks())

        self._begin_session(run_id, run_dir)
        self._recovered_task_ids = completed_before
        await self._setup_tool_runtime(run_id, run_dir)

        collector = MetricsCollector()
        providers = self._register_providers(effective, run_id, collector, generation=1)
        compiled = compile_workflow(
            effective,
            providers=providers,
            model=self._config.provider.model,
        )
        self._install_workflow(effective, compiled)
        if self._store is not None:
            # The critical ordering: durable outputs exist in Atlas's store before AnyCode
            # resumes the first unfinished task.
            self._store = rebuilt
            # The builder captured the store created by _install_workflow; rebind it to the
            # rebuilt one so the resumed render reads the durable blocks.
            self._prompt_builder = self._create_prompt_builder()

        engine = self._create_engine(checkpoint_dir)
        team_result = await self._execute(engine, compiled, resume_from=checkpoint.id)

        executed = {record.task_id for record in collector.records if record.task_id is not None}
        recovery = RecoveryMetrics(
            resumed=True,
            resume_count=1,
            recovery_latency_ms=(time.perf_counter() - resume_started) * 1000.0,
            completed_tasks_before_resume=len(completed_before),
            completed_tasks_reexecuted=len(completed_before & executed),
            inflight_tasks_before_resume=len(inflight_before),
            inflight_tasks_reexecuted=len(inflight_before & executed),
            unfinished_tasks_executed_after_resume=len(executed - completed_before),
            context_blocks_rebuilt=rebuilt_block_count,
        )

        return self._finalize(
            run_id=run_id,
            workflow=effective,
            experiment=experiment,
            started_at=started_at,
            collector=collector,
            team_result=team_result,
            checkpoint_dir=checkpoint_dir,
            run_dir=run_dir,
            manifest=manifest,
            manifest_path=manifest_path,
            placement_decisions=placement_decisions,
            placement_summary=placement_summary,
            recovery=recovery,
            persist=persist,
        )

    def _begin_session(self, run_id: str, run_dir: Path) -> None:
        self._run_id = run_id
        self._run_dir = run_dir
        self._inference_path = run_dir / "inference.jsonl"
        self._recovered_task_ids = frozenset()

    def _place(
        self,
        workflow: AtlasWorkflow,
    ) -> tuple[AtlasWorkflow, list[dict[str, Any]], PlacementSummary | None]:
        placement_config = self._config.placement
        if placement_config.policy == "bypass":
            return workflow, [], None

        policies = {
            "static_baseline": StaticPlacementPolicy,
            "capability_only": CapabilityOnlyPolicy,
            "context_locality": ContextLocalityPolicy,
        }
        policy = policies[placement_config.policy]()
        plan = plan_workflow(
            workflow,
            policy=policy,
            weights=PlacementWeights(
                locality=placement_config.weights.locality,
                load=placement_config.weights.load,
            ),
        )
        return (
            plan.workflow,
            [asdict(decision) for decision in plan.decisions],
            plan.summary,
        )

    def _install_workflow(
        self,
        effective: AtlasWorkflow,
        compiled: CompiledWorkflow,
    ) -> None:
        self._title_to_id = dict(compiled.id_by_title)
        self._id_by_title = dict(compiled.id_by_title)
        self._active_task.clear()
        self._task_assignee.clear()
        self._render_info.clear()
        self._effective_specs = {
            task.task_id: replace(task, description=task_description(task, effective.input))
            for task in effective.tasks
        }
        self._agents_by_name = {agent.name: agent for agent in effective.agents}
        self._configure_context_mode(self._run_id)
        self._prompt_builder = self._create_prompt_builder()

    def _create_prompt_builder(self) -> AtlasTaskPromptBuilder | None:
        """One shared task-prompt builder for this run, or ``None`` without a context mode."""

        if (
            self._store is None
            or self._selector is None
            or self._renderer is None
            or self._transform is None
        ):
            return None
        return AtlasTaskPromptBuilder(
            store=self._store,
            selector=self._selector,
            renderer=self._renderer,
            transform=self._transform,
            specs_by_title={spec.title: spec for spec in self._effective_specs.values()},
            agents_by_name={
                name: (agent.system_prompt, agent.role)
                for name, agent in self._agents_by_name.items()
            },
            recovered_task_ids=self._recovered_task_ids,
        )

    def _build_manifest(
        self,
        *,
        run_id: str,
        workflow: AtlasWorkflow,
        created_at: datetime,
        checkpoint_dir: Path,
    ) -> AtlasSessionManifest:
        placement_config = self._config.placement
        return AtlasSessionManifest(
            run_id=run_id,
            created_at=created_at,
            git_commit=git_commit(self._repo_root),
            git_dirty=git_dirty(self._repo_root),
            workflow_hash=workflow_hash(workflow),
            workflow_input=workflow.input,
            semantic_config_hash=semantic_config_hash(self._config),
            placed_assignment_hash=placed_assignment_hash(workflow),
            anycode_version=package_version("anycode-py"),
            contextpilot_version=package_version("contextpilot"),
            model=self._config.provider.model,
            placement_policy=placement_config.policy,
            placement_weights={
                "locality": placement_config.weights.locality,
                "load": placement_config.weights.load,
            },
            context_policy=self._config.context.policy,
            renderer_policy=self._config.context.renderer,
            context_transform=self._config.context.transform.name,
            checkpoint_location=str(checkpoint_dir),
        )

    def _create_engine(self, checkpoint_dir: Path) -> AnyCode:
        options = OrchestratorConfig(
            checkpoint=CheckpointConfig(
                enabled=True,
                path=str(checkpoint_dir),
                keep_last=10,
            ),
            on_progress=self._on_progress,
        )
        if self._renderer is None and self._tool_runtime is None:
            return self._engine_factory(options)
        return AtlasContextEngine(
            options,
            prompt_builder=(self._build_atlas_prompt if self._renderer is not None else None),
            tool_runtime=self._tool_runtime,
        )

    @staticmethod
    async def _execute(
        engine: AnyCode,
        compiled: CompiledWorkflow,
        *,
        resume_from: str | None,
    ) -> TeamRunResult:
        try:
            team = engine.create_team(
                compiled.workflow.name,
                TeamConfig(name=compiled.workflow.name, agents=list(compiled.agent_configs)),
            )
            return await engine.run_tasks(
                team,
                list(compiled.task_specs),
                resume_from=resume_from,
            )
        finally:
            await engine.close()

    def _finalize(
        self,
        *,
        run_id: str,
        workflow: AtlasWorkflow,
        experiment: str,
        started_at: datetime,
        collector: MetricsCollector,
        team_result: TeamRunResult,
        checkpoint_dir: Path,
        run_dir: Path,
        manifest: AtlasSessionManifest,
        manifest_path: Path,
        placement_decisions: list[dict[str, Any]],
        placement_summary: PlacementSummary | None,
        recovery: RecoveryMetrics | None,
        persist: bool,
    ) -> AtlasRunResult:
        checkpoint = self._load_checkpoint(checkpoint_dir)
        tasks = self._normalize_tasks(workflow, checkpoint)
        succeeded = team_result.success and all(task.status == "completed" for task in tasks)

        metrics = collector.aggregate(task_success=succeeded)
        total_transform_ms = sum(
            info.transform_latency_ms or 0.0 for info in self._render_info.values()
        )
        metrics = metrics.model_copy(
            update={
                "estimated_cost": estimate_cost_for_run(metrics, self._config.pricing),
                "total_transform_latency_ms": total_transform_ms,
            }
        )

        final_task = workflow.final_task()
        final_output = next(
            (task.output or "" for task in tasks if task.task_id == final_task.task_id),
            "",
        )

        transform_config = self._config.context.transform
        placement_config = self._config.placement
        result = AtlasRunResult(
            run_id=run_id,
            timestamp=started_at,
            workflow=workflow.name,
            experiment=experiment,
            status="succeeded" if succeeded else "failed",
            final_output=final_output,
            tasks=tasks,
            metrics=metrics,
            inference_records=list(collector.records),
            started_at=started_at,
            finished_at=datetime.now(UTC),
            model=self._config.provider.model,
            git_commit=git_commit(self._repo_root),
            git_dirty=git_dirty(self._repo_root),
            anycode_version=package_version("anycode-py"),
            contextpilot_version=package_version("contextpilot"),
            configuration_hash=configuration_hash(self._config),
            context_policy=self._config.context.policy,
            renderer_policy=self._config.context.renderer,
            transform_name=transform_config.name,
            deduplicated=any(info.deduplicated for info in self._render_info.values()),
            total_transform_latency_ms=total_transform_ms,
            placement_policy=placement_config.policy,
            placement_weights={
                "locality": placement_config.weights.locality,
                "load": placement_config.weights.load,
            },
            placement_decisions=placement_decisions,
            mean_assignment_locality=(
                placement_summary.mean_assignment_locality if placement_summary else None
            ),
            total_logical_handoff_refs=(
                placement_summary.total_logical_handoff_refs if placement_summary else 0
            ),
            total_logical_handoff_chars=(
                placement_summary.total_logical_handoff_chars if placement_summary else 0
            ),
            dependency_agent_switches=(
                placement_summary.dependency_agent_switches if placement_summary else 0
            ),
            assignment_distribution=(
                dict(placement_summary.assignment_distribution) if placement_summary else {}
            ),
            recovery=recovery,
            tool_runtime=(self._tool_runtime.metrics() if self._tool_runtime is not None else None),
        )

        # The manifest records session completion; result.json is the final observation. A
        # session with a manifest and checkpoint but no result.json is incomplete/recoverable.
        write_manifest(
            manifest_path,
            manifest.model_copy(update={"status": "succeeded" if succeeded else "failed"}),
        )
        if self._trajectory_recorder is not None:
            try:  # noqa: SIM105
                self._trajectory_recorder.record_run_terminal(
                    status="succeeded" if succeeded else "failed",
                    task_count=len(tasks),
                    final_output_digest=canonical_digest(final_output),
                    final_output_size=len(final_output.encode("utf-8")),
                    occurred_at=result.finished_at,
                )
            except Exception:  # noqa: BLE001 - evidence is optional
                pass
        result_path = run_dir / "result.json"
        result_path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
        if persist:
            ExperimentStore(self._runs_root / "runs.jsonl").append(result)
        return result

    def _configure_context_mode(self, run_id: str) -> None:
        if self._config.context.policy == "task_scoped":
            self._store = InMemoryContextStore()
            self._selector = TaskScopedSelector()
            self._renderer = (
                NaiveExecutionOrderRenderer()
                if self._config.context.renderer == "naive_execution_order"
                else StablePromptRenderer(preserve_input_order=True)
            )
            transform_config = self._config.context.transform
            self._transform = (
                IdentityContextTransform()
                if transform_config.name == "identity"
                else ContextPilotDedupTransform()
            )
        else:
            self._store = None
            self._selector = None
            self._renderer = None
            self._transform = None

    def _build_atlas_prompt(self, task: Task, queue: TaskQueue) -> str:
        """Atlas-rendered prompt for one task (C1/C2 modes only).

        Rendering itself lives in :class:`AtlasTaskPromptBuilder` so the coding compositions can
        reuse exactly one implementation; this method keeps the workflow's evidence capture.
        """

        if self._prompt_builder is None:
            raise RuntimeError("Atlas prompt builder called without a context mode")
        if not task.assignee:
            raise RuntimeError(f"task {task.title!r} has no assignee")

        atlas_id = self._id_by_title[task.title]
        prompt = self._prompt_builder(task, queue)
        render = self._prompt_builder.last(atlas_id)
        if render is not None:
            self._render_info[atlas_id] = _RenderInfo(
                context_layout_hash=render.context_layout_hash,
                rendered_dependency_order=render.rendered_dependency_order,
                dependency_completion_order=render.dependency_completion_order,
                pre_transform_layout_hash=render.pre_transform_layout_hash,
                post_transform_layout_hash=render.post_transform_layout_hash,
                transform_name=render.transform_name,
                deduplicated=render.deduplicated,
                transform_latency_ms=render.transform_latency_ms,
                original_block_count=render.original_block_count,
                optimized_block_count=render.optimized_block_count,
                original_context_chars=render.original_context_chars,
                optimized_context_chars=render.optimized_context_chars,
                stable_prefix_tokens=render.stable_prefix_tokens,
                context_block_tokens=render.context_block_tokens,
                dynamic_tail_tokens=render.dynamic_tail_tokens,
            )
        return prompt

    def _register_providers(
        self,
        workflow: AtlasWorkflow,
        run_id: str,
        collector: MetricsCollector,
        *,
        generation: int,
    ) -> dict[str, str]:
        """Register one AnyCode provider per agent, carrying usage attribution."""

        providers: dict[str, str] = {}
        for agent in workflow.agents:
            # The generation suffix keeps a resumed session's provider registration distinct from
            # the original (registry names are immutable for the process lifetime).
            provider_name = f"atlasos-{run_id}-g{generation}-{agent.name}"
            handler = self._call_handler(collector, run_id, agent.name, generation)

            async def factory(
                _agent_name: str = agent.name,
                _handler: Callable[[InferenceObservation], None] = handler,
                **_: Any,
            ) -> LLMAdapter:
                return self._adapter_factory(_agent_name, _handler)

            register_provider_factory(provider_name, factory)
            providers[agent.name] = provider_name
        return providers

    def _call_handler(
        self,
        collector: MetricsCollector,
        run_id: str,
        agent_name: str,
        generation: int,
    ) -> Callable[[InferenceObservation], None]:
        model = self._config.provider.model
        context_policy = self._config.context.policy
        renderer_policy = self._config.context.renderer

        def handle(observation: InferenceObservation) -> None:
            usage = observation.usage
            task_id = self._active_task.get(agent_name)
            info = self._render_info.get(task_id) if task_id is not None else None
            record = InferenceRecord(
                run_id=run_id,
                session_id=run_id,
                task_id=task_id,
                agent_id=agent_name,
                resume_generation=generation,
                model=model,
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                cache_hit_tokens=usage.cache_hit_tokens,
                cache_miss_tokens=usage.cache_miss_tokens,
                cache_hit_ratio=usage.cache_hit_ratio,
                latency_ms=usage.latency_ms,
                scheduler_policy=SCHEDULER_POLICY,
                context_policy=context_policy,
                renderer_policy=renderer_policy,
                contextpilot_enabled=False,
                stable_prefix_hash=observation.stable_prefix_hash,
                full_prompt_hash=observation.full_prompt_hash,
                context_layout_hash=info.context_layout_hash if info else None,
                rendered_dependency_order=(list(info.rendered_dependency_order) if info else None),
                dependency_completion_order=(
                    list(info.dependency_completion_order) if info else None
                ),
                stable_prefix_tokens=info.stable_prefix_tokens if info else None,
                context_block_tokens=info.context_block_tokens if info else None,
                dynamic_tail_tokens=info.dynamic_tail_tokens if info else None,
                transform_name=info.transform_name if info else "identity",
                deduplicated=info.deduplicated if info else False,
                transform_latency_ms=info.transform_latency_ms if info else None,
                original_block_count=info.original_block_count if info else None,
                optimized_block_count=info.optimized_block_count if info else None,
                original_context_chars=info.original_context_chars if info else None,
                optimized_context_chars=info.optimized_context_chars if info else None,
                pre_transform_layout_hash=(info.pre_transform_layout_hash if info else None),
                post_transform_layout_hash=(info.post_transform_layout_hash if info else None),
            )
            collector.add(record)
            if self._inference_path is not None:
                # Append-only: pre-crash observations survive a resume instead of being
                # overwritten by the recovered generation.
                with self._inference_path.open("a", encoding="utf-8", newline="\n") as handle:
                    handle.write(record.model_dump_json() + "\n")

        return handle

    def _on_progress(self, event: OrchestratorEvent) -> None:
        if event.type == "task_start" and isinstance(event.data, Task):
            task = event.data
            if task.assignee:
                self._task_assignee[task.id] = task.assignee
                atlas_id = self._title_to_id.get(task.title)
                if atlas_id is not None:
                    self._active_task[task.assignee] = atlas_id
                    set_current_task_id(atlas_id)
        elif event.type == "task_complete" and event.task:
            agent = self._task_assignee.pop(event.task, None)
            if agent is not None:
                self._active_task.pop(agent, None)
            set_current_task_id(None)
        if self._event_hook is not None:
            self._event_hook(event)

    def _normalize_tasks(
        self,
        workflow: AtlasWorkflow,
        checkpoint: CheckpointData,
    ) -> list[AtlasTaskResult]:
        """Read task outcomes from upstream task state, never from ``agent_results``.

        ``TeamRunResult.agent_results`` is keyed by agent and overwrites earlier tasks when one
        agent executes several tasks, so it cannot represent task-level results.
        """

        by_title = {task.title: task for task in checkpoint.tasks}
        results: list[AtlasTaskResult] = []
        for spec in workflow.tasks:
            state = by_title.get(spec.title)
            if state is None:
                raise RuntimeError(
                    f"upstream checkpoint is missing task {spec.title!r}; cannot normalize run"
                )
            elapsed_ms = (state.updated_at - state.created_at).total_seconds() * 1000.0
            results.append(
                AtlasTaskResult(
                    task_id=spec.task_id,
                    title=spec.title,
                    agent=state.assignee or spec.assignee,
                    status=state.status,
                    output=state.result,
                    created_at=state.created_at,
                    updated_at=state.updated_at,
                    duration_ms=elapsed_ms if elapsed_ms >= 0 else None,
                )
            )
        return results

    @staticmethod
    def _load_checkpoint(checkpoint_dir: Path) -> CheckpointData:
        """Load the most advanced checkpoint written for this run.

        Filenames and ids embed only a millisecond timestamp, so same-tick checkpoints can sort
        in the wrong order. Selection therefore uses execution truth first: the number of
        durably completed tasks is monotonic across checkpoints, then the wave index, then
        ``created_at`` and the file's nanosecond modification time. A same-tick tie therefore
        cannot select an older, less-complete state.
        """

        files = list(checkpoint_dir.rglob("*.json"))
        if not files:
            raise RuntimeError(f"no upstream checkpoint written under {checkpoint_dir}")
        candidates = []
        for path in files:
            checkpoint = CheckpointData.model_validate_json(path.read_text(encoding="utf-8"))
            completed = sum(1 for task in checkpoint.tasks if task.status == "completed")
            candidates.append((checkpoint, completed, path.stat().st_mtime_ns))
        checkpoint, _, _ = max(
            candidates,
            key=lambda pair: (
                pair[1],
                pair[0].wave_index,
                pair[0].created_at,
                pair[2],
                pair[0].id,
            ),
        )
        return checkpoint


def _validate_runtime_config(config: AtlasConfig) -> None:
    """Reject configurations whose labels the runtime cannot honestly report."""

    if config.scheduler.policy != SCHEDULER_POLICY:
        raise ValueError(
            "AtlasOS supports only scheduler_policy='upstream_default' until Stage 4; "
            f"got {config.scheduler.policy!r}"
        )
    if config.context.contextpilot_enabled:
        raise ValueError("ContextPilot is disabled until Stage 3")
