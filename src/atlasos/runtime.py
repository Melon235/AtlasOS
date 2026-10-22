"""Thin AtlasOS runtime facade over AnyCode.

AtlasRuntime composes upstream building blocks; it does not schedule tasks, execute a DAG, or
own an agent lifecycle. Its responsibilities are configuration, provider registration, context
projection/selection/rendering (Stage 2 modes), metrics attribution, result normalization from
upstream task state, and experiment persistence.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
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
)
from anycode.plugins.registry import register_provider_factory
from anycode.tasks.queue import TaskQueue
from anycode.types import LLMAdapter, OrchestratorEvent
from pydantic import BaseModel, ConfigDict

from atlasos.config import AtlasConfig, load_config
from atlasos.context import (
    ContextBlock,
    ContextKind,
    ContextRef,
    InMemoryContextStore,
    NaiveExecutionOrderRenderer,
    PromptRenderer,
    StablePromptRenderer,
    TaskScopedSelector,
)
from atlasos.experiments import ExperimentStore, configuration_hash, git_commit
from atlasos.integrations.anycode_engine import AtlasContextEngine
from atlasos.integrations.deepseek import DeepSeekAdapter
from atlasos.metrics import (
    InferenceObservation,
    InferenceRecord,
    MetricsCollector,
    RunMetrics,
    estimate_cost_for_run,
)
from atlasos.workflow import (
    AtlasAgentSpec,
    AtlasTaskSpec,
    AtlasWorkflow,
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
    configuration_hash: str

    # Frozen experiment labels; validated against the configuration at run time.
    scheduler_policy: str = SCHEDULER_POLICY
    context_policy: str = "upstream_default"
    renderer_policy: str = "upstream_default"
    contextpilot_enabled: bool = False


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
        repo_root: str | Path | None = None,
    ) -> None:
        _validate_runtime_config(config)
        self._config = config
        self._runs_root = Path(runs_root)
        self._adapter_factory = adapter_factory or _default_adapter_factory
        self._engine_factory: EngineFactory = engine_factory or (lambda options: AnyCode(options))
        self._repo_root = (
            Path(repo_root) if repo_root is not None else Path(__file__).resolve().parents[2]
        )
        self._running = False
        self._run_id = ""
        self._title_to_id: dict[str, str] = {}
        self._id_by_title: dict[str, str] = {}
        self._active_task: dict[str, str] = {}
        self._task_assignee: dict[str, str] = {}
        self._effective_specs: dict[str, AtlasTaskSpec] = {}
        self._agents_by_name: dict[str, AtlasAgentSpec] = {}
        self._store: InMemoryContextStore | None = None
        self._selector: TaskScopedSelector | None = None
        self._renderer: PromptRenderer | None = None
        self._render_info: dict[str, _RenderInfo] = {}

    @classmethod
    def from_config(cls, path: str | Path, **kwargs: Any) -> AtlasRuntime:
        return cls(load_config(path), **kwargs)

    @property
    def config(self) -> AtlasConfig:
        return self._config

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

        collector = MetricsCollector()
        providers = self._register_providers(workflow, run_id, collector)

        effective = replace(workflow, input=input_text) if input_text is not None else workflow
        compiled = compile_workflow(
            effective,
            providers=providers,
            model=self._config.provider.model,
        )

        self._run_id = run_id
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
        self._configure_context_mode()

        options = OrchestratorConfig(
            checkpoint=CheckpointConfig(
                enabled=True,
                path=str(checkpoint_dir),
                keep_last=10,
            ),
            on_progress=self._on_progress,
        )
        engine: AnyCode
        if self._renderer is None:
            engine = self._engine_factory(options)
        else:
            engine = AtlasContextEngine(options, prompt_builder=self._build_atlas_prompt)

        try:
            team = engine.create_team(
                effective.name,
                TeamConfig(name=effective.name, agents=list(compiled.agent_configs)),
            )
            team_result = await engine.run_tasks(team, list(compiled.task_specs))
        finally:
            await engine.close()

        checkpoint = self._load_checkpoint(checkpoint_dir)
        tasks = self._normalize_tasks(effective, checkpoint)
        succeeded = team_result.success and all(task.status == "completed" for task in tasks)

        metrics = collector.aggregate(task_success=succeeded)
        metrics = metrics.model_copy(
            update={"estimated_cost": estimate_cost_for_run(metrics, self._config.pricing)}
        )

        final_task = effective.final_task()
        final_output = next(
            (task.output or "" for task in tasks if task.task_id == final_task.task_id),
            "",
        )

        result = AtlasRunResult(
            run_id=run_id,
            timestamp=started_at,
            workflow=effective.name,
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
            configuration_hash=configuration_hash(self._config),
            context_policy=self._config.context.policy,
            renderer_policy=self._config.context.renderer,
        )
        if persist:
            ExperimentStore(self._runs_root / "runs.jsonl").append(result)
        return result

    def _configure_context_mode(self) -> None:
        if self._config.context.policy == "task_scoped":
            self._store = InMemoryContextStore()
            self._selector = TaskScopedSelector()
            self._renderer = (
                NaiveExecutionOrderRenderer()
                if self._config.context.renderer == "naive_execution_order"
                else StablePromptRenderer()
            )
        else:
            self._store = None
            self._selector = None
            self._renderer = None

    def _build_atlas_prompt(self, task: Task, queue: TaskQueue) -> str:
        """Atlas-rendered prompt for one task (C1/C2 modes only)."""

        if self._store is None or self._selector is None or self._renderer is None:
            raise RuntimeError("Atlas prompt builder called without a context mode")
        if not task.assignee:
            raise RuntimeError(f"task {task.title!r} has no assignee")

        atlas_id = self._id_by_title[task.title]
        spec = self._effective_specs[atlas_id]
        agent = self._agents_by_name[task.assignee]

        completed = [
            candidate
            for candidate in queue.list()
            if candidate.status == "completed" and candidate.result is not None
        ]
        # Projection order is completion order; the renderer decides whether it matters.
        completed.sort(key=lambda candidate: (candidate.updated_at, candidate.title))

        refs: list[ContextRef] = []
        direct_completion_order: list[str] = []
        dependencies = set(spec.depends_on)
        for done in completed:
            done_id = self._id_by_title[done.title]
            block = ContextBlock(
                block_id=f"task-output:{done_id}",
                kind=ContextKind.DEPENDENCY_OUTPUT,
                content=done.result or "",
                version=1,
                source=f"anycode_task:{done_id}",
                task_id=done_id,
                owner_agent_id=done.assignee,
            )
            self._store.put(block)
            refs.append(ContextRef(block_id=block.block_id, version=block.version))
            if done_id in dependencies:
                direct_completion_order.append(done_id)

        selected = self._selector.select(
            task=spec,
            agent_id=task.assignee,
            store=self._store,
            available_refs=refs,
        )
        rendered = self._renderer.render(
            stable_system_prefix=agent.system_prompt,
            agent_role=agent.role,
            task=spec,
            context_blocks=selected,
        )

        rendered_order = tuple(
            ref.block_id.removeprefix("task-output:")
            for ref in rendered.context_layout
            if ref.block_id.startswith("task-output:")
        )
        self._render_info[atlas_id] = _RenderInfo(
            context_layout_hash=rendered.context_layout_hash,
            rendered_dependency_order=rendered_order,
            dependency_completion_order=tuple(direct_completion_order),
            stable_prefix_tokens=rendered.stable_prefix_tokens,
            context_block_tokens=rendered.context_block_tokens,
            dynamic_tail_tokens=rendered.dynamic_tail_tokens,
        )
        return rendered.messages[-1]["content"]

    def _register_providers(
        self,
        workflow: AtlasWorkflow,
        run_id: str,
        collector: MetricsCollector,
    ) -> dict[str, str]:
        """Register one AnyCode provider per agent, carrying usage attribution."""

        providers: dict[str, str] = {}
        for agent in workflow.agents:
            provider_name = f"atlasos-{run_id}-{agent.name}"
            handler = self._call_handler(collector, run_id, agent.name)

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
    ) -> Callable[[InferenceObservation], None]:
        model = self._config.provider.model
        context_policy = self._config.context.policy
        renderer_policy = self._config.context.renderer

        def handle(observation: InferenceObservation) -> None:
            usage = observation.usage
            task_id = self._active_task.get(agent_name)
            info = self._render_info.get(task_id) if task_id is not None else None
            collector.add(
                InferenceRecord(
                    run_id=run_id,
                    session_id=run_id,
                    task_id=task_id,
                    agent_id=agent_name,
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
                    rendered_dependency_order=(
                        list(info.rendered_dependency_order) if info else None
                    ),
                    dependency_completion_order=(
                        list(info.dependency_completion_order) if info else None
                    ),
                    stable_prefix_tokens=info.stable_prefix_tokens if info else None,
                    context_block_tokens=info.context_block_tokens if info else None,
                    dynamic_tail_tokens=info.dynamic_tail_tokens if info else None,
                )
            )

        return handle

    def _on_progress(self, event: OrchestratorEvent) -> None:
        if event.type == "task_start" and isinstance(event.data, Task):
            task = event.data
            if task.assignee:
                self._task_assignee[task.id] = task.assignee
                atlas_id = self._title_to_id.get(task.title)
                if atlas_id is not None:
                    self._active_task[task.assignee] = atlas_id
        elif event.type == "task_complete" and event.task:
            agent = self._task_assignee.pop(event.task, None)
            if agent is not None:
                self._active_task.pop(agent, None)

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
        """Load the newest checkpoint file written for this run.

        Each Atlas run owns its checkpoint directory, so the newest file belongs to this run;
        no private workflow-id derivation is needed.
        """

        files = sorted(checkpoint_dir.rglob("*.json"), key=lambda path: path.name)
        if not files:
            raise RuntimeError(f"no upstream checkpoint written under {checkpoint_dir}")
        return CheckpointData.model_validate_json(files[-1].read_text(encoding="utf-8"))


def _validate_runtime_config(config: AtlasConfig) -> None:
    """Reject configurations whose labels the runtime cannot honestly report."""

    if config.scheduler.policy != SCHEDULER_POLICY:
        raise ValueError(
            "AtlasOS supports only scheduler_policy='upstream_default' until Stage 4; "
            f"got {config.scheduler.policy!r}"
        )
    if config.context.contextpilot_enabled:
        raise ValueError("ContextPilot is disabled until Stage 3")
