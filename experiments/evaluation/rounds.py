"""Evaluation round orchestration: smoke, pilot, and formal Inspect AI runs."""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from experiments.evaluation.inspect_adapter import run_inspect_evaluation
from experiments.evaluation.runner import RunnerSettings
from experiments.evaluation.tasks import TASKS, task_by_id

from atlasos.experiments import git_commit, git_dirty

REPO_ROOT = Path(__file__).resolve().parents[2]
RESULTS_ROOT = REPO_ROOT / "experiments" / "results" / "evaluation"

SMOKE_SAMPLES = (
    "bugfix-005-date-range",
    "feature-005-bank",
    "tool-003-currency",
    "multiworker-002-stats-report",
    "longcontext-001-buried-constant",
)
PILOT_SAMPLES = (
    "bugfix-009-pagination",
    "feature-002-inventory",
    "tool-004-shipping",
    "multiworker-003-pipeline",
    "longcontext-003-module-map",
    "longcontext-005-requirements",
)


@dataclass(frozen=True)
class SuiteSpec:
    name: str
    samples: tuple[str, ...]
    repetitions: int


def _selected_tasks(suite: str) -> tuple[Any, ...]:
    if suite == "smoke":
        return tuple(task_by_id(sample) for sample in SMOKE_SAMPLES)
    if suite == "pilot":
        return tuple(task_by_id(sample) for sample in PILOT_SAMPLES)
    if suite == "formal":
        return TASKS
    raise ValueError(f"unknown suite: {suite}")


def _log_summary(log: Any) -> dict[str, Any]:
    samples = list(getattr(log, "samples", []) or [])
    scores = [score for sample in samples for score in (sample.scores or {}).values()]
    sample_errors = [
        {
            "sample_id": sample.id,
            "error": str(getattr(event, "error", "")),
        }
        for sample in samples
        for event in (sample.events or [])
        if getattr(event, "event", None) == "error"
    ]
    passed = sum(1 for score in scores if float(score.value or 0) >= 1.0)
    return {
        "status": str(getattr(log, "status", "unknown")),
        "location": str(getattr(log, "location", "")) or None,
        "sample_count": len(samples),
        "scored_sample_count": len(scores),
        "passed_sample_count": passed,
        "sample_errors": sample_errors,
    }


def run_suite(
    suite: str,
    *,
    repetitions: int,
    results_root: Path,
    max_turns: int,
    timeout_seconds: int,
    max_samples: int,
) -> dict[str, Any]:
    tasks = _selected_tasks(suite)
    round_id = f"{suite}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    round_root = results_root / "rounds" / round_id
    log_dir = round_root / "inspect-logs"
    runs_root = round_root / "runs"
    round_root.mkdir(parents=True, exist_ok=False)
    settings = RunnerSettings(
        runs_root=runs_root,
        timeout_seconds=timeout_seconds,
        max_turns=max_turns,
    )
    manifest: dict[str, Any] = {
        "round_id": round_id,
        "suite": suite,
        "tasks": [task.sample_id for task in tasks],
        "task_count": len(tasks),
        "repetitions_requested": repetitions,
        "settings": {
            "benchmark_id": settings.benchmark_id,
            "dataset_version": settings.dataset_version,
            "scorer_version": settings.scorer_version,
            "config_path": str(settings.config_path),
            "config_digest": settings.digest(),
            "temperature": settings.temperature,
            "max_turns": settings.max_turns,
            "max_tokens": settings.max_tokens,
            "token_budget": settings.token_budget,
            "timeout_seconds": settings.timeout_seconds,
            "progressive_tools": settings.progressive_tools,
        },
        "git_commit": git_commit(REPO_ROOT),
        "git_dirty": git_dirty(REPO_ROOT),
        "started_at": datetime.now(UTC).isoformat(),
        "repetitions": [],
    }
    started = time.perf_counter()
    for repetition in range(repetitions):
        rep_started = time.perf_counter()
        logs = run_inspect_evaluation(
            settings=settings,
            repetition_id=repetition,
            tasks=tasks,
            log_dir=str(log_dir),
            max_samples=max_samples,
        )
        summaries = [_log_summary(log) for log in logs]
        manifest["repetitions"].append(
            {
                "repetition_id": repetition,
                "elapsed_seconds": round(time.perf_counter() - rep_started, 3),
                "logs": summaries,
                "passed": all(
                    summary["status"] in ("success", "completed")
                    and summary["scored_sample_count"] == summary["sample_count"]
                    and summary["passed_sample_count"] == summary["sample_count"]
                    and not summary["sample_errors"]
                    for summary in summaries
                ),
            }
        )
        (round_root / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    manifest["finished_at"] = datetime.now(UTC).isoformat()
    manifest["elapsed_seconds"] = round(time.perf_counter() - started, 3)
    manifest["all_repetitions_passed"] = all(
        repetition["passed"] for repetition in manifest["repetitions"]
    )
    (round_root / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="atlas-eval-round")
    parser.add_argument("suite", choices=("smoke", "pilot", "formal"))
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--max-turns", type=int, default=14)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--max-samples", type=int, default=1)
    parser.add_argument("--results-root", default=str(RESULTS_ROOT))
    args = parser.parse_args(argv)
    manifest = run_suite(
        args.suite,
        repetitions=args.repetitions,
        results_root=Path(args.results_root),
        max_turns=args.max_turns,
        timeout_seconds=args.timeout,
        max_samples=args.max_samples,
    )
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0 if manifest["all_repetitions_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
