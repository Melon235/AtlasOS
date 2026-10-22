"""Thin AtlasOS runtime facade over AnyCode.

AtlasRuntime composes upstream building blocks; it does not schedule tasks, execute a DAG, or
own an agent lifecycle. Its responsibilities are configuration, provider registration, metrics
attribution, result normalization from upstream task state, and experiment persistence.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import replace
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
from anycode.types import LLMAdapter, OrchestratorEvent
from pydantic import BaseModel, ConfigDict

from atlasos.config import AtlasConfig, load_config
from atlasos.experiments import ExperimentStore, configuration_hash, git_commit
from atlasos.integrations.deepseek import DeepSeekAdapter
from atlasos.metrics import (
    InferenceObservation,
    InferenceRecord,
    MetricsCollector,
    RunMetrics,
    estimate_cost_for_run,
)
from atlasos.workflow import AtlasWorkflow, TaskStatusName, compile_workflow

DEFAULT_RUNS_ROOT = Path("experiments/runs")
SCHEDULER_POLICY = "upstream_default"
CONTEXT_POLICY = "upstream_default"

AdapterFactory = Callable[[str, Callable[[InferenceObservation], None]], LLMAdapter]
EngineFactory = Callable[[OrchestratorConfig], AnyCode]


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
    """Stable, experiment-facing representation of one baseline run."""

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

    # Frozen Stage 1 labels; see docs/stages/01-baseline-notes.md.
    scheduler_policy: str = SCHEDULER_POLICY
    context_policy: str = CONTEXT_POLICY
    contextpilot_enabled: bool = False


def _default_adapter_factory(
    agent_name: str,
    on_call: Callable[[InferenceObservation], None],
) -> LLMAdapter:
    del agent_name  # a single provider configuration serves every agent in Stage 1
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
        _validate_stage1_config(config)
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
        self._active_task: dict[str, str] = {}
        self._task_assignee: dict[str, str] = {}

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
        self._active_task.clear()
        self._task_assignee.clear()

        engine = self._engine_factory(
            OrchestratorConfig(
                checkpoint=CheckpointConfig(
                    enabled=True,
                    path=str(checkpoint_dir),
                    keep_last=10,
                ),
                on_progress=self._on_progress,
            )
        )
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
        )
        if persist:
            ExperimentStore(self._runs_root / "runs.jsonl").append(result)
        return result

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

        def handle(observation: InferenceObservation) -> None:
            usage = observation.usage
            collector.add(
                InferenceRecord(
                    run_id=run_id,
                    session_id=run_id,
                    task_id=self._active_task.get(agent_name),
                    agent_id=agent_name,
                    model=model,
                    prompt_tokens=usage.prompt_tokens,
                    completion_tokens=usage.completion_tokens,
                    cache_hit_tokens=usage.cache_hit_tokens,
                    cache_miss_tokens=usage.cache_miss_tokens,
                    cache_hit_ratio=usage.cache_hit_ratio,
                    latency_ms=usage.latency_ms,
                    scheduler_policy=SCHEDULER_POLICY,
                    context_policy=CONTEXT_POLICY,
                    contextpilot_enabled=False,
                    stable_prefix_hash=observation.stable_prefix_hash,
                    full_prompt_hash=observation.full_prompt_hash,
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


def _validate_stage1_config(config: AtlasConfig) -> None:
    """Reject configurations whose labels Stage 1 cannot honestly report."""

    if config.scheduler.policy != SCHEDULER_POLICY:
        raise ValueError(
            "Stage 1 supports only scheduler_policy='upstream_default'; "
            f"got {config.scheduler.policy!r}"
        )
    if config.context.contextpilot_enabled:
        raise ValueError("Stage 1 must not enable ContextPilot")
    if config.context.selector != "full":
        raise ValueError(
            "Stage 1 has no context selector yet; keep context.selector='full' "
            f"(got {config.context.selector!r})"
        )
