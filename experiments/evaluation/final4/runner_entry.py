# ruff: noqa: E501
"""Final-4 live entry: run one manifest unit (task x configuration) through the shipped runner.

The three validation tasks are materialised as real ``BenchmarkTask`` objects (visible seed +
hidden oracle + candidate WorkerSteps), so nothing here duplicates the runner, the oracle or the
scoring logic.

    python -m experiments.evaluation.final4.runner_entry --unit validation-serial-R0
    python -m experiments.evaluation.final4.runner_entry --units validation-serial-R0,validation-parallel-R1

Configuration mapping (frozen in ``pilot_manifest.json``):

* ``R0`` single worker, progressive tools, guarded run budget.
* ``R1`` multi worker over the task's frozen candidate DAG, same core and the same run budget.
* ``R2`` same as R1 but the shipped router decides DIRECT (single) vs DELEGATE_PARALLEL (the DAG).
* ``N0`` native AnyCode single in a neutral sandbox (see ``baseline.py``).

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

from experiments.evaluation.final4.validation_tasks import TASKS as VALIDATION_TASKS
from experiments.evaluation.final4.validation_tasks import Task
from experiments.evaluation.models import BenchmarkTask, WorkerStep
from experiments.evaluation.runner import (
    DELEGATION_ROUTE_MODES,
    RunnerSettings,
    run_sample,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
RUNS_ROOT = REPO_ROOT / "experiments" / "runs" / "final4"
CONFIG_PATH = REPO_ROOT / "experiments" / "configs" / "eval-benchmark.yaml"

#: ``shadow`` observes without changing execution; ``selective`` actually branches on the decision.
ROUTER_MODE = "selective" if "selective" in DELEGATION_ROUTE_MODES else "off"

#: One entry per frozen unit. ``120_000`` tokens / 40 tool calls / 14 turns / 900 s are the task-level
#: limits from ``pilot_manifest.json``; the budget is shared by every worker in the run.
UNITS: dict[str, dict[str, Any]] = {
    "N0": {"runner": "baseline"},
    "R0": {"runner": "atlas", "mode": "coding", "route": "off"},
    "R1": {"runner": "atlas", "mode": "multi_worker", "route": "off"},
    "R2": {"runner": "atlas", "mode": "multi_worker", "route": ROUTER_MODE},
}


def _worker_steps(task: Task) -> tuple[WorkerStep, ...]:
    return tuple(
        WorkerStep(
            task_id=str(worker["task_id"]),
            title=str(worker.get("objective", worker["task_id"]))[:80],
            prompt=str(worker["objective"]),
            depends_on=tuple(worker["depends_on"]),  # type: ignore[arg-type]
            max_turns=14,
        )
        for worker in task.workers
    )


def build_task(task: Task, *, multi: bool) -> BenchmarkTask:
    return BenchmarkTask(
        sample_id=task.task_id,
        category="feature",
        mode="multi_worker" if multi else "coding",
        prompt=task.prompt,
        target="",
        visible_files=dict(task.seed),
        hidden_files=dict(task.oracle_tests),
        workers=_worker_steps(task)
        if multi
        else (
            WorkerStep(
                task_id="main",
                title=task.task_id,
                prompt=task.prompt,
                depends_on=(),
                max_turns=14,
            ),
        ),
        allowed_paths=tuple(task.seed),
        required_artifacts=(),
        max_turns=14,
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
        benchmark_id="atlasos-final4-validation-v1",
        dataset_version="final4-validation-1",
        scorer_version="oracle-1",
        config_path=CONFIG_PATH,
        runs_root=runs_root,
        timeout_seconds=900,
        temperature=0.0,
        max_turns=14,
        max_tokens=8000,
        token_budget=120_000,
        budget_mode="guarded",
        max_tool_calls=40,
        progressive_tools=True,
        handoff_mode="legacy",
        oracle_mode="sandbox",
        delegation_route_mode=str(unit["route"]),
        delegation_policy_version="r0.1.0",
    )


async def run_unit(task_id: str, configuration: str, *, runs_root: Path) -> dict[str, Any]:
    task = _task_by_id(task_id)
    settings = settings_for(configuration, runs_root=runs_root)
    if UNITS[configuration]["runner"] == "baseline":
        from experiments.evaluation.baseline import run_baseline_sample

        result = await run_baseline_sample(
            build_task(task, multi=False), settings=settings, repetition_id=0
        )
        return {"unit": f"{task_id}-{configuration}", "runner": "baseline", "result": str(result)}

    result = await run_sample(
        build_task(task, multi=UNITS[configuration]["mode"] == "multi_worker"),
        settings=settings,
        repetition_id=0,
        config_path=CONFIG_PATH,
    )
    return {
        "unit": f"{task_id}-{configuration}",
        "runner": "atlas",
        "run_dir": str(result.run_dir),
        "oracle_pass": bool(getattr(result, "oracle_pass", False)),
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
        summary.append(asyncio.run(run_unit(task_id, configuration, runs_root=runs_root)))
        print(json.dumps(summary[-1], indent=2, sort_keys=True)[:800], flush=True)
    (runs_root / "unit_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
