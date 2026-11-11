"""Inspect AI integration: Dataset/Task/Solver/Scorer over the AtlasOS adapter.

Inspect AI is the external evaluation harness. It never receives AtlasOS execution or approval
authority, and its model configuration is never used to execute a sample: the custom solver calls
the AtlasOS adapter, which uses the existing AtlasOS DeepSeek adapter internally. Inspect's model
name is carried only as provenance in the eval log.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from experiments.evaluation.models import BenchmarkTask
from experiments.evaluation.runner import RunnerSettings, run_sample
from experiments.evaluation.tasks import TASKS
from inspect_ai import Task
from inspect_ai import eval as inspect_eval
from inspect_ai.dataset import MemoryDataset, Sample
from inspect_ai.model import ModelOutput
from inspect_ai.scorer import Score, Scorer, Target, accuracy, scorer, stderr
from inspect_ai.solver import Generate, Solver, TaskState, solver

BENCHMARK_ID = "atlasos-agentbench-v1"
DATASET_VERSION = "1.0.0"
SCORER_VERSION = "1.0.0"
INSPECT_MODEL = "deepseek/deepseek-chat"


def benchmark_dataset(tasks: tuple[BenchmarkTask, ...] = TASKS) -> MemoryDataset:
    return MemoryDataset(
        [
            Sample(
                input=task.prompt,
                id=task.sample_id,
                target=task.target,
                metadata=task.public_metadata(),
                sandbox=None,
            )
            for task in tasks
        ],
        name=f"{BENCHMARK_ID}-{DATASET_VERSION}",
    )


def _settings_payload(settings: RunnerSettings) -> dict[str, Any]:
    return {
        "benchmark_id": settings.benchmark_id,
        "dataset_version": settings.dataset_version,
        "scorer_version": settings.scorer_version,
        "config_path": str(settings.config_path),
        "runs_root": str(settings.runs_root),
        "timeout_seconds": settings.timeout_seconds,
        "temperature": settings.temperature,
        "max_turns": settings.max_turns,
        "max_tokens": settings.max_tokens,
        "token_budget": settings.token_budget,
        "progressive_tools": settings.progressive_tools,
        "model_override": settings.model_override,
    }


def _settings_from_payload(payload: dict[str, Any]) -> RunnerSettings:
    return RunnerSettings(
        benchmark_id=payload["benchmark_id"],
        dataset_version=payload["dataset_version"],
        scorer_version=payload["scorer_version"],
        config_path=Path(payload["config_path"]),
        runs_root=Path(payload["runs_root"]),
        timeout_seconds=int(payload["timeout_seconds"]),
        temperature=float(payload["temperature"]),
        max_turns=int(payload["max_turns"]),
        max_tokens=int(payload["max_tokens"]),
        token_budget=int(payload["token_budget"]),
        progressive_tools=bool(payload["progressive_tools"]),
        model_override=payload["model_override"],
    )


@solver
def atlas_os_solver(settings_payload: dict[str, Any], repetition_id: int) -> Solver:
    """Run one sample through the AtlasOS adapter; never call Inspect's model."""

    async def solve(state: TaskState, generate: Generate) -> TaskState:
        del generate
        from experiments.evaluation.tasks import task_by_id

        task = task_by_id(str(state.sample_id))
        settings = _settings_from_payload(settings_payload)
        try:
            result = await run_sample(
                task,
                settings=settings,
                repetition_id=repetition_id,
            )
            payload = result.as_dict()
            state.store.set("atlas_result", payload)
            state.metadata = {
                **(state.metadata or {}),
                "atlas_failure_class": result.failure_class,
                "atlas_success": result.success,
                "atlas_run_id": result.identity.run_id,
                "atlas_run_dir": result.run_dir,
                "atlas_config_digest": result.identity.config_digest,
                "atlas_git_commit": result.identity.git_commit,
                "atlas_git_dirty": result.identity.git_dirty,
            }
            content = result.final_output or ""
            state.output = ModelOutput.from_content(
                model=INSPECT_MODEL,
                content=content,
                stop_reason="stop",
            )
        except BaseException as error:  # noqa: BLE001 - Inspect must record, not crash
            if not isinstance(error, Exception):
                raise
            state.store.set(
                "atlas_result",
                {
                    "success": False,
                    "failure_class": "RUNTIME_FAILURE",
                    "failure_reason": f"{type(error).__name__}: {error}",
                    "oracle": {"passed": False, "checks": {}, "failure_reasons": [str(error)]},
                },
            )
            state.metadata = {
                **(state.metadata or {}),
                "atlas_failure_class": "RUNTIME_FAILURE",
                "atlas_error": f"{type(error).__name__}: {error}",
            }
            state.output = ModelOutput.from_content(
                model=INSPECT_MODEL,
                content="",
                stop_reason="stop",
                error=str(error),
            )
        state.completed = True
        return state

    return solve


@scorer(metrics=[accuracy(), stderr()])
def atlas_oracle_scorer() -> Scorer:
    """Score the independent oracle result, not the agent's prose."""

    async def score(state: TaskState, target: Target) -> Score:
        del target
        result = state.store.get("atlas_result") or {}
        success = bool(result.get("success"))
        oracle = result.get("oracle") or {}
        checks = oracle.get("checks") or {}
        metrics = result.get("metrics") or {}
        identity = result.get("identity") or {}
        failure_class = str(result.get("failure_class") or "NOT_EVALUATED")
        return Score(
            value=1.0 if success else 0.0,
            answer=str(result.get("final_output") or "")[:2000],
            explanation=(
                "oracle passed"
                if success
                else f"{failure_class}: {result.get('failure_reason') or 'oracle failed'}"
            ),
            metadata={
                "sample_id": identity.get("sample_id") or state.sample_id,
                "category": result.get("category"),
                "mode": result.get("mode"),
                "failure_class": failure_class,
                "oracle_passed": bool(oracle.get("passed")),
                "oracle_checks": checks,
                "oracle_failure_reasons": oracle.get("failure_reasons") or [],
                "run_id": identity.get("run_id"),
                "repetition_id": identity.get("repetition_id"),
                "config_digest": identity.get("config_digest"),
                "fixture_digest": identity.get("fixture_digest"),
                "git_commit": identity.get("git_commit"),
                "git_dirty": identity.get("git_dirty"),
                "model": identity.get("model"),
                "resolved_models": (result.get("environment") or {}).get("resolved_models"),
                "metrics": metrics,
                "safety": result.get("safety") or {},
                "tool_search_calls": metrics.get("tool_search_calls"),
                "dispatch_calls": metrics.get("dispatch_calls"),
                "worker_selection": metrics.get("worker_selection"),
            },
        )

    return score


def build_task(
    *,
    settings: RunnerSettings,
    repetition_id: int,
    tasks: tuple[BenchmarkTask, ...] = TASKS,
    name: str | None = None,
) -> Task:
    load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=False)
    return Task(
        dataset=benchmark_dataset(tasks),
        solver=atlas_os_solver(_settings_payload(settings), repetition_id),
        scorer=atlas_oracle_scorer(),
        model=INSPECT_MODEL,
        name=name or BENCHMARK_ID,
        version=f"{DATASET_VERSION}+{SCORER_VERSION}",
        metadata={
            "benchmark_id": settings.benchmark_id,
            "dataset_version": settings.dataset_version,
            "scorer_version": settings.scorer_version,
            "config_digest": settings.digest(),
            "git_commit": None,
            "model_requested": settings.model_override or INSPECT_MODEL,
            "repetition_id": repetition_id,
            "execution_authority": "atlasos",
            "inspect_model_is_used_for_generation": False,
        },
        message_limit=None,
        token_limit=200_000,
        time_limit=settings.timeout_seconds * len(tasks) + 300,
        fail_on_error=False,
    )


def run_inspect_evaluation(
    *,
    settings: RunnerSettings,
    repetition_id: int,
    tasks: tuple[BenchmarkTask, ...] = TASKS,
    log_dir: str | None = None,
    max_samples: int = 1,
) -> list[Any]:
    load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=False)
    task = build_task(settings=settings, repetition_id=repetition_id, tasks=tasks)
    return inspect_eval(
        task,
        model=INSPECT_MODEL,
        log_dir=log_dir,
        display="none",
        max_samples=max_samples,
        log_samples=True,
        fail_on_error=False,
    )
