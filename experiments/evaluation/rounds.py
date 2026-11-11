"""Evaluation round orchestration: smoke, pilot, and formal Inspect AI runs."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from experiments.evaluation.baseline import summarize_baseline
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
ABLATION_SAMPLES = (
    "bugfix-001-slugify",
    "bugfix-007-toposort",
    "feature-001-todo-store",
    "feature-003-line-diff",
    "tool-001-fare",
    "tool-003-currency",
    "multiworker-001-handoff",
    "multiworker-003-pipeline",
    "longcontext-001-buried-constant",
    "longcontext-003-module-map",
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
    if suite == "ablation-tools":
        return tuple(task_by_id(sample) for sample in ABLATION_SAMPLES)
    if suite == "baseline":
        return tuple(task for task in TASKS if task.category != "tool_discovery")
    raise ValueError(f"unknown suite: {suite}")


def _run_isolated_sample(
    *,
    sample_id: str,
    repetition_id: int,
    runs_root: Path,
    log_dir: Path,
    max_turns: int,
    timeout_seconds: int,
    progressive_tools: bool,
) -> dict[str, Any]:
    """Run one sample in its own process with a hard wall-clock kill.

    A hung provider call or MCP subprocess cannot block the rest of a formal round: the parent
    kills the whole child process tree on timeout and records the sample as a runtime failure.
    """

    command = [
        sys.executable,
        "-m",
        "experiments.evaluation.cli",
        "inspect",
        "--sample",
        sample_id,
        "--repetition",
        str(repetition_id),
        "--runs-root",
        str(runs_root),
        "--log-dir",
        str(log_dir),
        "--max-turns",
        str(max_turns),
        "--timeout",
        str(timeout_seconds),
        "--max-samples",
        "1",
    ]
    if not progressive_tools:
        command.append("--no-progressive-tools")
    log_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = log_dir / f"{sample_id}-r{repetition_id}.stdout.txt"
    stderr_path = log_dir / f"{sample_id}-r{repetition_id}.stderr.txt"
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(REPO_ROOT / "src"), str(REPO_ROOT)]),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    process = subprocess.Popen(  # noqa: S603 - fixed command, no shell
        command,
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds + 180)
    except subprocess.TimeoutExpired:
        timed_out = True
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                capture_output=True,
                check=False,
            )
        else:
            process.kill()
        stdout, stderr = process.communicate()
    stdout_path.write_text(stdout or "", encoding="utf-8")
    stderr_path.write_text(stderr or "", encoding="utf-8")
    summary: dict[str, Any] = {
        "sample_id": sample_id,
        "repetition_id": repetition_id,
        "timed_out": timed_out,
        "returncode": process.returncode,
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
    }
    parsed: dict[str, Any] | None = None
    for line in reversed((stdout or "").splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            candidate = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and "inspect_status" in candidate:
            parsed = candidate
            break
    summary.update(parsed or {})
    summary["passed"] = bool(
        not timed_out
        and parsed
        and parsed.get("inspect_status") in ("success", "completed")
        and parsed.get("passed_samples") == 1
        and parsed.get("sample_errors") == 0
    )
    return summary


def _run_isolated_baseline(
    *,
    sample_id: str,
    repetition_id: int,
    baseline_root: Path,
    max_turns: int,
    timeout_seconds: int,
) -> dict[str, Any]:
    command = [
        sys.executable,
        "-m",
        "experiments.evaluation.cli",
        "baseline",
        "--sample",
        sample_id,
        "--repetition",
        str(repetition_id),
        "--repetitions",
        "1",
        "--runs-root",
        str(baseline_root),
        "--max-turns",
        str(max_turns),
        "--timeout",
        str(timeout_seconds),
    ]
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(REPO_ROOT / "src"), str(REPO_ROOT)]),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    process = subprocess.Popen(  # noqa: S603 - fixed command, no shell
        command,
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds + 120)
    except subprocess.TimeoutExpired:
        timed_out = True
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                capture_output=True,
                check=False,
            )
        else:
            process.kill()
        stdout, stderr = process.communicate()
    parsed: dict[str, Any] | None = None
    for line in reversed((stdout or "").splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            candidate = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and "sample_id" in candidate and "success" in candidate:
            parsed = candidate
            break
    return {
        "sample_id": sample_id,
        "repetition_id": repetition_id,
        "timed_out": timed_out,
        "returncode": process.returncode,
        "success": bool(parsed.get("success")) if parsed else False,
        "failure_class": (parsed or {}).get("failure_class"),
        "passed": bool(
            not timed_out and process.returncode == 0 and parsed and parsed.get("success") is True
        ),
        "stderr_tail": (stderr or "")[-2000:],
    }


def run_suite(
    suite: str,
    *,
    repetitions: int,
    results_root: Path,
    max_turns: int,
    timeout_seconds: int,
    max_samples: int,
    progressive_tools: bool = True,
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
        progressive_tools=progressive_tools,
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
        if suite == "baseline":
            baseline_root = round_root / "baseline-runs"
            summaries = [
                _run_isolated_baseline(
                    sample_id=task.sample_id,
                    repetition_id=repetition,
                    baseline_root=baseline_root,
                    max_turns=max_turns,
                    timeout_seconds=timeout_seconds,
                )
                for task in tasks
            ]
        else:
            summaries = [
                _run_isolated_sample(
                    sample_id=task.sample_id,
                    repetition_id=repetition,
                    runs_root=runs_root,
                    log_dir=log_dir,
                    max_turns=max_turns,
                    timeout_seconds=timeout_seconds,
                    progressive_tools=progressive_tools,
                )
                for task in tasks
            ]
        manifest["repetitions"].append(
            {
                "repetition_id": repetition,
                "elapsed_seconds": round(time.perf_counter() - rep_started, 3),
                "samples": summaries,
                "passed": all(summary["passed"] for summary in summaries),
                "passed_samples": sum(1 for summary in summaries if summary["passed"]),
                "sample_count": len(summaries),
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
    if suite == "baseline":
        manifest["baseline_summary"] = summarize_baseline(round_root / "baseline-runs")
    (round_root / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="atlas-eval-round")
    parser.add_argument("suite", choices=("smoke", "pilot", "formal", "ablation-tools", "baseline"))
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--max-turns", type=int, default=14)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--max-samples", type=int, default=1)
    parser.add_argument("--no-progressive-tools", action="store_true")
    parser.add_argument("--results-root", default=str(RESULTS_ROOT))
    args = parser.parse_args(argv)
    manifest = run_suite(
        args.suite,
        repetitions=args.repetitions,
        results_root=Path(args.results_root),
        max_turns=args.max_turns,
        timeout_seconds=args.timeout,
        max_samples=args.max_samples,
        progressive_tools=not args.no_progressive_tools,
    )
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0 if manifest["all_repetitions_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
