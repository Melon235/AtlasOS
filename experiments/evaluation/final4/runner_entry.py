# ruff: noqa: E501
"""Final-4 live entry: run one manifest unit (task x configuration) through the shipped runner.

The three validation tasks are materialised as real ``BenchmarkTask`` objects (visible seed +
hidden oracle + candidate WorkerSteps), so nothing here duplicates the runner, the oracle or the
scoring logic.

    python -m experiments.evaluation.final4.runner_entry --unit validation-serial-R0
    python -m experiments.evaluation.final4.runner_entry --units validation-serial-R0,validation-serial-X0

Configuration mapping (frozen in ``pilot_manifest_v2.json``). Every Atlas unit resolves its context
configuration from a real YAML file; the expected value is declared per unit and enforced before the
first model request, so a manifest can no longer describe a Context Plane the runner did not load:

* ``X0`` multi worker over the candidate DAG, ``eval-benchmark.yaml`` (``upstream_default`` context),
  legacy handoff. This is the ablation arm.
* ``X1`` the Atlas Context Plane over the same DAG. ``X1`` is exactly ``<task>-R1``; it is not a
  separate unit, it is the name the ablation pair uses for that cell.
* ``R0`` single worker, ``final4-context.yaml`` (``task_scoped`` + ``stable_deterministic`` +
  ``identity``), progressive tools, guarded run budget.
* ``R1`` multi worker over the task's frozen candidate DAG, same core and the same run budget.
* ``R2`` same as R1 but the shipped router decides DIRECT (single) vs DELEGATE_PARALLEL (the DAG).
* ``N0`` native AnyCode single in a neutral sandbox (see ``baseline.py``); still blocked.

Every unit writes its own run directory under the git-ignored ``experiments/runs/final4``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from experiments.evaluation.final4.validation_tasks import DATASET_VERSION as TASK_DATASET_VERSION
from experiments.evaluation.final4.validation_tasks import TASKS as VALIDATION_TASKS
from experiments.evaluation.final4.validation_tasks import Task
from experiments.evaluation.models import BenchmarkTask, WorkerStep, canonical_digest
from experiments.evaluation.runner import (
    CONTEXT_PLANE_TASK_SCOPED,
    CONTEXT_PLANE_UNSPECIFIED,
    CONTEXT_PLANE_UPSTREAM,
    DELEGATION_ROUTE_MODES,
    RunnerSettings,
    run_sample,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
RUNS_ROOT = REPO_ROOT / "experiments" / "runs" / "final4"
BASELINE_CONFIG = REPO_ROOT / "experiments" / "configs" / "eval-benchmark.yaml"
CONTEXT_CONFIG = REPO_ROOT / "experiments" / "configs" / "final4-context.yaml"

#: ``shadow`` observes without changing execution; ``selective`` actually branches on the decision.
ROUTER_MODE = "selective" if "selective" in DELEGATION_ROUTE_MODES else "off"

BENCHMARK_ID = "atlasos-final4-validation-v2"
MAX_TURN_PER_WORKER = 14
TOKEN_BUDGET = 120_000
MAX_TOOL_CALLS = 40
TIMEOUT_SECONDS = 900

#: One entry per frozen unit. ``config`` selects the YAML the runner really loads; ``expectation`` is
#: the Context Plane state the unit claims and is enforced by ``runner._enforce_context_plane``.
UNITS: dict[str, dict[str, Any]] = {
    "N0": {
        "runner": "baseline",
        "config": BASELINE_CONFIG,
        "expectation": CONTEXT_PLANE_UNSPECIFIED,
    },
    "X0": {
        "runner": "atlas",
        "mode": "multi_worker",
        "route": "off",
        "config": BASELINE_CONFIG,
        "expectation": CONTEXT_PLANE_UPSTREAM,
    },
    "R0": {
        "runner": "atlas",
        "mode": "coding",
        "route": "off",
        "config": CONTEXT_CONFIG,
        "expectation": CONTEXT_PLANE_TASK_SCOPED,
    },
    "R1": {
        "runner": "atlas",
        "mode": "multi_worker",
        "route": "off",
        "config": CONTEXT_CONFIG,
        "expectation": CONTEXT_PLANE_TASK_SCOPED,
    },
    "R2": {
        "runner": "atlas",
        "mode": "multi_worker",
        "route": ROUTER_MODE,
        "config": CONTEXT_CONFIG,
        "expectation": CONTEXT_PLANE_TASK_SCOPED,
    },
}

#: ``X1`` is not a separate unit: it is the Atlas Context Plane cell of the same DAG, i.e. ``R1``.
X1_EQUIVALENT_CONFIGURATION = "R1"


def single_worker_turn(task: Task) -> str:
    """The whole-task turn: the shared public context plus an explicit whole-task assignment.

    The single-worker arms and the router's DIRECT collapse both consume this, so a task is never
    described more fully to a decomposed plan than to a single worker.
    """

    return "\n\n".join(
        [
            task.shared_context,
            "[YOUR ASSIGNMENT]\n"
            "You are the only worker on this task. Implement every module named in the public "
            "requirement above, in the same workspace.\n\n"
            f"DEFINITION OF DONE: no `NotImplementedError` is left in {', '.join(sorted(task.seed))}, "
            "`python -m pytest -q test_public.py` passes, and you report what you ran and what it "
            "printed.",
        ]
    )


def _worker_steps(task: Task) -> tuple[WorkerStep, ...]:
    """Candidate DAG steps: the worker's own assignment, plus the shared public context.

    ``prompt`` stays the worker-local text on purpose: it is what the router reads for declared
    write paths, and the write sets of the steps are disjoint. ``shared_context`` is prepended to the
    model-visible task turn only.
    """

    return tuple(
        WorkerStep(
            task_id=str(worker["task_id"]),
            title=str(worker["objective"])[:80],
            prompt=str(worker["assignment"]),
            depends_on=tuple(worker["depends_on"]),  # type: ignore[arg-type]
            max_turns=MAX_TURN_PER_WORKER,
            shared_context=task.shared_context,
        )
        for worker in task.workers
    )


def _single_step(task: Task) -> tuple[WorkerStep, ...]:
    return (
        WorkerStep(
            task_id="main",
            title=task.task_id,
            prompt=single_worker_turn(task),
            depends_on=(),
            max_turns=MAX_TURN_PER_WORKER,
        ),
    )


def build_task(task: Task, *, multi: bool) -> BenchmarkTask:
    return BenchmarkTask(
        sample_id=task.task_id,
        category="feature",
        mode="multi_worker" if multi else "coding",
        prompt=single_worker_turn(task),
        target="",
        visible_files=dict(task.seed),
        hidden_files=dict(task.oracle_tests),
        workers=_worker_steps(task) if multi else _single_step(task),
        allowed_paths=tuple(task.seed),
        required_artifacts=(),
        max_turns=MAX_TURN_PER_WORKER,
        tags=("final4", task.family),
        notes=task.notes,
    )


def _task_by_id(task_id: str) -> Task:
    for task in VALIDATION_TASKS:
        if task.task_id == task_id:
            return task
    raise SystemExit(f"unknown task_id {task_id!r}")


def settings_for(configuration: str, *, runs_root: Path) -> RunnerSettings:
    unit = UNITS[configuration]
    return RunnerSettings(
        benchmark_id=BENCHMARK_ID,
        dataset_version=TASK_DATASET_VERSION,
        scorer_version="oracle-1",
        config_path=Path(unit["config"]),
        runs_root=runs_root,
        timeout_seconds=TIMEOUT_SECONDS,
        temperature=0.0,
        max_turns=MAX_TURN_PER_WORKER,
        max_tokens=8000,
        token_budget=TOKEN_BUDGET,
        budget_mode="guarded",
        max_tool_calls=MAX_TOOL_CALLS,
        progressive_tools=True,
        handoff_mode="legacy",
        # Measurement hygiene, identical in every arm: under `legacy` a worker is terminated with
        # `side_effect_unknown` when a read-only command exits non-zero, which is what killed both
        # workers of the 2026-11-14 R1 run. See docs/final-4-legacy-pilot-reclassification.md §2.
        tool_error_mode="corrected",
        oracle_mode="sandbox",
        delegation_route_mode=str(unit["route"]),
        delegation_policy_version="r0.1.0",
        context_plane_expectation=str(unit["expectation"]),
    )


def preflight(configuration: str) -> dict[str, Any]:
    """Check the unit's claim against the YAML *before* a sandbox or a model call is created."""

    from atlasos.config import load_config

    unit = UNITS[configuration]
    config_path = Path(unit["config"])
    config = load_config(config_path)
    expectation = str(unit["expectation"])
    resolved = {
        "unit": configuration,
        "config_path": str(config_path),
        "policy": config.context.policy,
        "renderer": config.context.renderer,
        "transform": config.context.transform.name,
        "placement_policy": config.placement.policy,
        "sandbox_provider": config.sandbox.provider,
        "sandbox_image": config.sandbox.image,
        "expectation": expectation,
    }
    if expectation == CONTEXT_PLANE_TASK_SCOPED and (
        config.context.policy != CONTEXT_PLANE_TASK_SCOPED
        or config.context.renderer != "stable_deterministic"
        or config.context.transform.name != "identity"
    ):
        raise SystemExit(
            "EVALUATION_CONFIG_ERROR: "
            f"{configuration} declares task_scoped/stable_deterministic/identity but "
            f"{config_path.name} resolves {config.context.policy}/{config.context.renderer}/"
            f"{config.context.transform.name}"
        )
    if expectation == CONTEXT_PLANE_UPSTREAM and config.context.policy != CONTEXT_PLANE_UPSTREAM:
        raise SystemExit(
            "EVALUATION_CONFIG_ERROR: "
            f"{configuration} declares upstream_default context but {config_path.name} resolves "
            f"{config.context.policy}"
        )
    if config.sandbox.provider == "fake":
        raise SystemExit(
            f"EVALUATION_CONFIG_ERROR: {configuration} resolves sandbox.provider='fake'; a real "
            "provider is required"
        )
    return resolved


async def run_unit(
    task_id: str, configuration: str, *, runs_root: Path, repetition_id: int = 0
) -> dict[str, Any]:
    task = _task_by_id(task_id)
    settings = settings_for(configuration, runs_root=runs_root)
    resolved = preflight(configuration)
    unit = f"{task_id}-{configuration}"
    if UNITS[configuration]["runner"] == "baseline":
        from experiments.evaluation.baseline import run_baseline_sample

        result = await run_baseline_sample(
            build_task(task, multi=False), settings=settings, repetition_id=repetition_id
        )
        return {
            "unit": unit,
            "runner": "baseline",
            "task_id": task_id,
            "configuration": configuration,
            "resolved": resolved,
            "result": str(result),
        }

    result = await run_sample(
        build_task(task, multi=UNITS[configuration]["mode"] == "multi_worker"),
        settings=settings,
        repetition_id=repetition_id,
        config_path=Path(UNITS[configuration]["config"]),
    )
    context_evidence = result.metrics.extra.get("context", {})
    return {
        "unit": unit,
        "runner": "atlas",
        "task_id": task_id,
        "configuration": configuration,
        "run_dir": str(result.run_dir),
        "run_id": result.identity.run_id,
        "oracle_pass": bool(result.oracle.passed),
        "success": bool(result.success),
        "failure_class": result.failure_class,
        "failure_reason": result.failure_reason,
        "config_digest": result.identity.config_digest,
        "candidate_plan_digest": canonical_digest(
            [
                {
                    "task_id": step["task_id"],
                    "depends_on": list(step["depends_on"]),  # type: ignore[arg-type]
                    "objective": step["objective"],
                    "owned_paths": list(step["owned_paths"]),  # type: ignore[arg-type]
                }
                for step in task.workers
            ]
        ),
        "resolved": resolved,
        "context": {
            "enabled": context_evidence.get("enabled"),
            "builder": context_evidence.get("builder"),
            "builder_invocations": context_evidence.get("builder_invocations"),
            "store_block_ids": context_evidence.get("store_block_ids"),
            "tasks": context_evidence.get("tasks"),
        },
        "routing": result.metrics.extra.get("routing"),
        "worker_count": len(result.worker_results),
        "budget": result.metrics.extra.get("budget"),
        "prompt_tokens": result.metrics.prompt_tokens,
        "cache_hit_tokens": result.metrics.cache_hit_tokens,
        "cache_miss_tokens": result.metrics.cache_miss_tokens,
        "completion_tokens": result.metrics.completion_tokens,
        "estimated_cost_usd": result.metrics.estimated_cost_usd,
        "latency_ms": result.metrics.latency_ms,
        "sandbox_destroyed": result.metrics.sandbox_destroyed,
    }


def main() -> int:
    load_dotenv(REPO_ROOT / ".env", override=False)
    if not os.environ.get("DEEPSEEK_API_KEY"):
        print("LIVE_PROVIDER_BLOCKED: no DEEPSEEK_API_KEY (process env or repository .env)")
        return 2

    parser = argparse.ArgumentParser(description="run frozen final-4 pilot units")
    parser.add_argument("--unit", help="single unit, e.g. validation-serial-R0")
    parser.add_argument("--units", help="comma separated units")
    parser.add_argument("--runs-root", default=str(RUNS_ROOT))
    parser.add_argument(
        "--repetition",
        type=int,
        default=0,
        help="repetition id; a replicated unit gets its own run directory",
    )
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="resolve every requested unit's configuration and exit without calling a model",
    )
    args = parser.parse_args()

    requested = args.units.split(",") if args.units else ([args.unit] if args.unit else [])
    if not requested:
        print("nothing to do: pass --unit or --units")
        return 2

    runs_root = Path(args.runs_root)
    runs_root.mkdir(parents=True, exist_ok=True)
    summary: list[dict[str, Any]] = []
    for unit in requested:
        unit = unit.strip()
        task_id, _, configuration = unit.rpartition("-")
        if configuration not in UNITS:
            print(f"skip {unit}: unknown configuration {configuration!r}")
            continue
        print(f"=== {task_id} / {configuration} ===", flush=True)
        if args.preflight_only:
            summary.append({"unit": unit, "resolved": preflight(configuration)})
            print(json.dumps(summary[-1], indent=2, sort_keys=True)[:800], flush=True)
            continue
        summary.append(
            asyncio.run(
                run_unit(
                    task_id,
                    configuration,
                    runs_root=runs_root,
                    repetition_id=args.repetition,
                )
            )
        )
        print(json.dumps(summary[-1], indent=2, sort_keys=True)[:1200], flush=True)
    (runs_root / "unit_summary.json").write_text(
        json.dumps(
            {
                "benchmark_id": BENCHMARK_ID,
                "dataset_version": TASK_DATASET_VERSION,
                "x1_equivalent_configuration": X1_EQUIVALENT_CONFIGURATION,
                "units": summary,
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
