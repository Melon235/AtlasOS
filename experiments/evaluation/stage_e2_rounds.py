"""Stage E2 Track A round runner: C0/C1/C2 and placement P0/P1."""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from experiments.evaluation.stage_e2_tasks import DEFINITIONS

from atlasos.experiments import git_commit, git_dirty

REPO_ROOT = Path(__file__).resolve().parents[2]
RESULTS_ROOT = REPO_ROOT / "experiments" / "results" / "stage-e2"
CONTEXT_CONFIGS = ("C0", "C1", "C2")
PLACEMENT_DEFINITIONS = tuple(item.task_id for item in DEFINITIONS[:4])


def _run_one(
    *,
    task_id: str,
    context: str,
    placement: str,
    workflow: str,
    repetition: int,
    round_root: Path,
    timeout_seconds: int,
    call_timeout: float,
) -> dict[str, Any]:
    output = round_root / "records" / f"{task_id}-{context}-{placement}-r{repetition}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-m",
        "experiments.evaluation.stage_e2_cli",
        "--task",
        task_id,
        "--context",
        context,
        "--placement",
        placement,
        "--workflow",
        workflow,
        "--repetition",
        str(repetition),
        "--runs-root",
        str(round_root / "runs"),
        "--output",
        str(output),
        "--call-timeout",
        str(call_timeout),
    ]
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(REPO_ROOT / "src"), str(REPO_ROOT)]),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    process = subprocess.Popen(  # noqa: S603
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
        stdout, stderr = process.communicate(timeout=timeout_seconds)
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
        if line.startswith("{"):
            try:
                candidate = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict) and "task_id" in candidate:
                parsed = candidate
                break
    result = {
        "task_id": task_id,
        "context": context,
        "placement": placement,
        "workflow": workflow,
        "repetition": repetition,
        "timed_out": timed_out,
        "returncode": process.returncode,
        "record_path": str(output),
        "stdout_tail": (stdout or "")[-1000:],
        "stderr_tail": (stderr or "")[-2000:],
    }
    result.update(parsed or {})
    if output.is_file():
        detail = json.loads(output.read_text(encoding="utf-8"))
        result["correct"] = bool(detail.get("correct"))
        result["prompt_tokens"] = detail.get("prompt_tokens")
        result["completion_tokens"] = detail.get("completion_tokens")
        result["cache_hit_tokens"] = detail.get("cache_hit_tokens")
        result["cache_miss_tokens"] = detail.get("cache_miss_tokens")
        result["latency_ms"] = detail.get("latency_ms")
        result["estimated_cost"] = detail.get("estimated_cost")
        result["transform_latency_ms"] = detail.get("transform_latency_ms")
        result["final_output"] = detail.get("final_output")
        result["inference_records"] = detail.get("inference_records")
        result["placement_decisions"] = detail.get("placement_decisions")
        result["mean_assignment_locality"] = detail.get("mean_assignment_locality")
        result["dependency_agent_switches"] = detail.get("dependency_agent_switches")
        result["total_logical_handoff_refs"] = detail.get("total_logical_handoff_refs")
        result["git_commit"] = detail.get("git_commit")
        result["git_dirty"] = detail.get("git_dirty")
    return result


def _aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_config: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        key = f"{row['workflow']}:{row['context']}:{row['placement']}"
        by_config.setdefault(key, []).append(row)
    summary = {}
    for key, values in sorted(by_config.items()):
        prompt = [int(item.get("prompt_tokens") or 0) for item in values]
        hit = [int(item.get("cache_hit_tokens") or 0) for item in values]
        miss = [int(item.get("cache_miss_tokens") or 0) for item in values]
        summary[key] = {
            "runs": len(values),
            "correct": sum(1 for item in values if item.get("correct")),
            "success_rate": sum(1 for item in values if item.get("correct")) / len(values),
            "prompt_tokens": sum(prompt),
            "prompt_tokens_mean": statistics.mean(prompt) if prompt else None,
            "cache_hit_tokens": sum(hit),
            "cache_miss_tokens": sum(miss),
            "cache_hit_ratio": (sum(hit) / (sum(hit) + sum(miss)))
            if (sum(hit) + sum(miss))
            else None,
            "estimated_cost": round(
                sum(float(item.get("estimated_cost") or 0.0) for item in values), 8
            ),
            "latency_p50_ms": statistics.median(
                [float(item["latency_ms"]) for item in values if item.get("latency_ms")]
            )
            if any(item.get("latency_ms") for item in values)
            else None,
            "transform_latency_ms": sum(
                float(item.get("transform_latency_ms") or 0.0) for item in values
            ),
            "deduplicated_runs": sum(
                1
                for item in values
                for record in (item.get("inference_records") or [])
                if record.get("deduplicated")
            ),
            "block_reduction_chars": sum(
                int(record.get("original_context_chars") or 0)
                - int(record.get("optimized_context_chars") or 0)
                for item in values
                for record in (item.get("inference_records") or [])
            ),
            "dependency_agent_switches": sum(
                int(item.get("dependency_agent_switches") or 0) for item in values
            ),
            "mean_assignment_locality": statistics.mean(
                [
                    float(item["mean_assignment_locality"])
                    for item in values
                    if item.get("mean_assignment_locality") is not None
                ]
            )
            if any(item.get("mean_assignment_locality") is not None for item in values)
            else None,
        }
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="atlas-stage-e2-round")
    parser.add_argument("suite", choices=("smoke", "context", "placement", "all"))
    parser.add_argument("--repetitions", type=int, default=2)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--call-timeout", type=float, default=45.0)
    args = parser.parse_args(argv)
    round_id = f"e2-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    round_root = RESULTS_ROOT / round_id
    round_root.mkdir(parents=True, exist_ok=False)
    tasks = DEFINITIONS[:2] if args.suite == "smoke" else DEFINITIONS
    context_configs = CONTEXT_CONFIGS if args.suite in ("smoke", "context", "all") else ()
    placement_configs = (
        (("capability_only", "P0"), ("context_locality", "P1"))
        if args.suite in ("smoke", "placement", "all")
        else ()
    )
    manifest: dict[str, Any] = {
        "round_id": round_id,
        "suite": args.suite,
        "git_commit": git_commit(REPO_ROOT),
        "git_dirty": git_dirty(REPO_ROOT),
        "started_at": datetime.now(UTC).isoformat(),
        "runs": [],
        "repetitions": args.repetitions,
    }
    started = time.perf_counter()
    for repetition in range(args.repetitions):
        for definition in tasks:
            for context in context_configs:
                result = _run_one(
                    task_id=definition.task_id,
                    context=context,
                    placement="bypass",
                    workflow="context",
                    repetition=repetition,
                    round_root=round_root,
                    timeout_seconds=args.timeout,
                    call_timeout=args.call_timeout,
                )
                manifest["runs"].append(result)
                print(
                    f"{round_id} context {definition.task_id} {context} r{repetition} "
                    f"correct={result.get('correct')}",
                    flush=True,
                )
                (round_root / "manifest.json").write_text(
                    json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
                )
        for task_id in PLACEMENT_DEFINITIONS:
            for placement, label in placement_configs:
                result = _run_one(
                    task_id=task_id,
                    context="C1",
                    placement=placement,
                    workflow="placement",
                    repetition=repetition,
                    round_root=round_root,
                    timeout_seconds=args.timeout,
                    call_timeout=args.call_timeout,
                )
                result["placement_label"] = label
                manifest["runs"].append(result)
                print(
                    f"{round_id} placement {task_id} {label} r{repetition} "
                    f"correct={result.get('correct')}",
                    flush=True,
                )
                (round_root / "manifest.json").write_text(
                    json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
                )
    manifest["summary"] = _aggregate(manifest["runs"])
    manifest["finished_at"] = datetime.now(UTC).isoformat()
    manifest["elapsed_seconds"] = round(time.perf_counter() - started, 3)
    (round_root / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(json.dumps(manifest["summary"], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
