"""Command-line entry points for the AtlasOS evaluation layer."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from experiments.evaluation.baseline import run_baseline_suite, summarize_baseline
from experiments.evaluation.inspect_adapter import run_inspect_evaluation
from experiments.evaluation.models import BenchmarkTask
from experiments.evaluation.runner import RunnerSettings, run_sample
from experiments.evaluation.tasks import TASKS, task_by_id

REPO_ROOT = Path(__file__).resolve().parents[2]


def _print_task(task: BenchmarkTask) -> None:
    print(
        json.dumps(
            {
                "sample_id": task.sample_id,
                "category": task.category,
                "mode": task.mode,
                "workers": len(task.workers),
                "fixture_digest": task.fixture_digest,
            },
            sort_keys=True,
        )
    )


def _list(_: argparse.Namespace) -> int:
    for task in TASKS:
        _print_task(task)
    return 0


def _run(args: argparse.Namespace) -> int:
    settings = RunnerSettings(
        runs_root=Path(args.runs_root).resolve(),
        timeout_seconds=args.timeout,
        max_turns=args.max_turns,
        progressive_tools=not args.no_progressive_tools,
    )
    tasks = [task_by_id(args.sample)] if args.sample else list(TASKS)
    if args.category:
        tasks = [task for task in tasks if task.category == args.category]
    if args.limit:
        tasks = tasks[: args.limit]
    failures = 0
    for task in tasks:
        for repetition in range(args.repetitions):
            result = asyncio.run(run_sample(task, settings=settings, repetition_id=repetition))
            print(
                json.dumps(
                    {
                        "sample_id": task.sample_id,
                        "repetition_id": repetition,
                        "success": result.success,
                        "failure_class": result.failure_class,
                        "oracle_passed": result.oracle.passed,
                        "run_id": result.identity.run_id,
                        "run_dir": result.run_dir,
                    },
                    sort_keys=True,
                )
            )
            failures += 0 if result.success else 1
    return 0 if failures == 0 else 1


def _inspect(args: argparse.Namespace) -> int:
    settings = RunnerSettings(
        runs_root=Path(args.runs_root).resolve(),
        timeout_seconds=args.timeout,
        max_turns=args.max_turns,
        progressive_tools=not args.no_progressive_tools,
    )
    if args.samples:
        tasks = [task_by_id(item.strip()) for item in args.samples.split(",") if item.strip()]
    elif args.sample:
        tasks = [task_by_id(args.sample)]
    else:
        tasks = list(TASKS)
    if args.category:
        tasks = [task for task in tasks if task.category == args.category]
    if args.limit:
        tasks = tasks[: args.limit]
    logs = run_inspect_evaluation(
        settings=settings,
        repetition_id=args.repetition,
        tasks=tuple(tasks),
        log_dir=args.log_dir,
        max_samples=args.max_samples,
    )
    failures = 0
    for log in logs:
        status = str(getattr(log, "status", "unknown"))
        location = getattr(log, "location", None)
        samples = list(getattr(log, "samples", []) or [])
        scores = [score for sample in samples for score in (sample.scores or {}).values()]
        sample_errors = sum(
            1
            for sample in samples
            for event in (sample.events or [])
            if getattr(event, "event", None) == "error"
        )
        passed = sum(1 for score in scores if float(score.value or 0) >= 1.0)
        print(
            json.dumps(
                {
                    "inspect_status": status,
                    "location": str(location) if location else None,
                    "samples": len(samples),
                    "scored_samples": len(scores),
                    "passed_samples": passed,
                    "sample_errors": sample_errors,
                },
                sort_keys=True,
            )
        )
        if (
            status not in ("success", "completed")
            or sample_errors
            or len(scores) != len(samples)
            or passed != len(samples)
        ):
            failures += 1
    return 0 if failures == 0 else 1


def _baseline(args: argparse.Namespace) -> int:
    settings = RunnerSettings(
        runs_root=Path(args.runs_root).resolve(),
        timeout_seconds=args.timeout,
        max_turns=args.max_turns,
    )
    if args.samples:
        tasks = [task_by_id(item.strip()) for item in args.samples.split(",") if item.strip()]
    elif args.sample:
        tasks = [task_by_id(args.sample)]
    else:
        tasks = [task for task in TASKS if task.category != "tool_discovery"]
    baseline_root = Path(args.runs_root).resolve()
    for repetition in range(args.repetitions):
        results = asyncio.run(
            run_baseline_suite(
                settings=settings,
                tasks=tuple(tasks),
                repetition_id=repetition,
                baseline_root=baseline_root,
            )
        )
        for result in results:
            print(
                json.dumps(
                    {
                        "sample_id": result.task.sample_id,
                        "repetition_id": repetition,
                        "success": result.success,
                        "failure_class": result.failure_class,
                        "run_id": result.identity.run_id,
                    },
                    sort_keys=True,
                )
            )
    print(json.dumps(summarize_baseline(baseline_root), indent=2, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="atlas-eval")
    sub = parser.add_subparsers(dest="command", required=True)
    list_parser = sub.add_parser("list")
    list_parser.set_defaults(func=_list)

    run_parser = sub.add_parser("run")
    run_parser.add_argument("--sample")
    run_parser.add_argument("--category")
    run_parser.add_argument("--limit", type=int, default=0)
    run_parser.add_argument("--repetitions", type=int, default=1)
    run_parser.add_argument("--max-turns", type=int, default=14)
    run_parser.add_argument("--timeout", type=int, default=900)
    run_parser.add_argument(
        "--runs-root",
        default=str(REPO_ROOT / "experiments" / "runs" / "evaluation"),
    )
    run_parser.add_argument("--no-progressive-tools", action="store_true")
    run_parser.set_defaults(func=_run)

    inspect_parser = sub.add_parser("inspect")
    inspect_parser.add_argument("--sample")
    inspect_parser.add_argument("--samples")
    inspect_parser.add_argument("--category")
    inspect_parser.add_argument("--limit", type=int, default=0)
    inspect_parser.add_argument("--repetition", type=int, default=0)
    inspect_parser.add_argument("--max-turns", type=int, default=14)
    inspect_parser.add_argument("--timeout", type=int, default=900)
    inspect_parser.add_argument("--max-samples", type=int, default=1)
    inspect_parser.add_argument("--log-dir", default=None)
    inspect_parser.add_argument(
        "--runs-root",
        default=str(REPO_ROOT / "experiments" / "runs" / "evaluation"),
    )
    inspect_parser.add_argument("--no-progressive-tools", action="store_true")
    inspect_parser.set_defaults(func=_inspect)

    baseline_parser = sub.add_parser("baseline")
    baseline_parser.add_argument("--sample")
    baseline_parser.add_argument("--samples")
    baseline_parser.add_argument("--repetitions", type=int, default=1)
    baseline_parser.add_argument("--max-turns", type=int, default=14)
    baseline_parser.add_argument("--timeout", type=int, default=600)
    baseline_parser.add_argument(
        "--runs-root",
        default=str(REPO_ROOT / "experiments" / "runs" / "evaluation-baseline"),
    )
    baseline_parser.set_defaults(func=_baseline)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
