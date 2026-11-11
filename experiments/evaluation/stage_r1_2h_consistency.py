"""R1-2H consistency check: sandboxed oracle vs the scores saved by the Host oracle.

Reads historical run directories (read-only), re-scores the exported workspace with the isolated
oracle and compares the verdict with ``result.json``. Nothing is re-executed on the Host.

Run with ``python -m experiments.evaluation.stage_r1_2h_consistency --limit 8``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path
from typing import Any

from experiments.evaluation.models import BenchmarkTask
from experiments.evaluation.runner import REPO_ROOT, _build_provider
from experiments.evaluation.sandboxed_oracle import (
    OracleSandboxConfig,
    evaluate_oracle_isolated,
)
from experiments.evaluation.stage_r05_tasks import definition_by_id, variants
from experiments.evaluation.stage_r1_2_verify import HistoricalRun, discover

RESULTS_ROOT = REPO_ROOT / "experiments" / "results" / "stage-r1-2h"

MATCH = "MATCH"
TEST_ENVIRONMENT_DIFFERENCE = "TEST_ENVIRONMENT_DIFFERENCE"
DEPENDENCY_DIFFERENCE = "DEPENDENCY_DIFFERENCE"
ORACLE_EXECUTION_FAILURE = "ORACLE_EXECUTION_FAILURE"
ACTUAL_SCORING_DIFFERENCE = "ACTUAL_SCORING_DIFFERENCE"
UNKNOWN = "UNKNOWN"


def benchmark_task_for(sample_id: str) -> BenchmarkTask:
    """Rebuild the exact task variant a historical run used.

    ``variants()`` produces ``-a0`` (single worker) and ``-a1`` (multi worker); the suffix must be
    matched by identity, not by position, or the worker-contract checks silently disappear.
    """

    base = sample_id.rsplit("-a", 1)[0] if sample_id.rsplit("-a", 1)[-1].isdigit() else sample_id
    definition = definition_by_id(base)
    for task in variants(definition):
        if task.sample_id == sample_id:
            return task
    return variants(definition)[0]


def _baseline(task: BenchmarkTask) -> dict[str, str]:
    import hashlib

    return {
        path: hashlib.sha256(content.encode("utf-8")).hexdigest()
        for path, content in task.visible_files.items()
    }


def _saved_oracle(run_dir: Path) -> dict[str, Any]:
    payload = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
    return dict(payload.get("oracle") or {})


def classify_difference(
    *,
    saved: dict[str, Any],
    new_passed: bool,
    new_kind: str | None,
    new_environment_failure: bool,
) -> str:
    old_passed = bool(saved.get("passed"))
    if old_passed == new_passed and not new_environment_failure:
        return MATCH
    if new_environment_failure:
        return ORACLE_EXECUTION_FAILURE
    old_returncode = (saved.get("details") or {}).get("hidden_tests", {}).get("returncode")
    if old_returncode is not None and new_kind == "TEST_COLLECTION_FAILED":
        return DEPENDENCY_DIFFERENCE
    if old_passed != new_passed:
        return ACTUAL_SCORING_DIFFERENCE
    return UNKNOWN


async def compare(
    runs: list[HistoricalRun], *, image: str | None, output_root: Path
) -> dict[str, Any]:
    provider = _build_provider(_load_eval_config())
    records: list[dict[str, Any]] = []
    for run in runs:
        if run.workspace is None:
            records.append(
                {
                    "run": run.run_key,
                    "sample": run.sample_id,
                    "difference": "UNAVAILABLE",
                }
            )
            continue
        saved = _saved_oracle(run.run_dir)
        saved_result = json.loads((run.run_dir / "result.json").read_text(encoding="utf-8"))
        # The manifest records the sample family; the variant (``-a0`` coding / ``-a1`` multi
        # worker) is only in the run's own task metadata, and the worker-contract checks depend on
        # it.
        variant = str((saved_result.get("task") or {}).get("sample_id") or run.sample_id)
        task = benchmark_task_for(variant)
        started = time.perf_counter()
        outcome = await evaluate_oracle_isolated(
            task,
            workspace=run.workspace,
            baseline_hashes=_baseline(task),
            tool_calls=list(saved_result.get("tool_calls") or []),
            worker_results=dict(saved_result.get("worker_results") or {}),
            run_dir=_oracle_dir(output_root, run),
            provider=provider,
            config=OracleSandboxConfig(image=image),
            host_fixture_unchanged=True,
            run_id=f"r12h-{run.run_key}",
        )
        difference = classify_difference(
            saved=saved,
            new_passed=outcome.passed,
            new_kind=outcome.failure_kind,
            new_environment_failure=outcome.environment_failure,
        )
        records.append(
            {
                "run": run.run_key,
                "sample": run.sample_id,
                "round": run.round_id,
                "saved_passed": bool(saved.get("passed")),
                "saved_returncode": (saved.get("details") or {})
                .get("hidden_tests", {})
                .get("returncode"),
                "new_passed": outcome.passed,
                "new_kind": outcome.failure_kind,
                "new_environment_failure": outcome.environment_failure,
                "cleanup_failure": outcome.cleanup_failure,
                "difference": difference,
                "timings_ms": outcome.timings_ms,
                "wall_ms": round((time.perf_counter() - started) * 1000.0, 3),
                "digests": outcome.digests,
            }
        )
        print(
            f"{run.run_key:<40} saved={bool(saved.get('passed'))!s:<5} "
            f"new={outcome.passed!s:<5} {difference} {outcome.failure_kind}",
            flush=True,
        )
    return {"records": records, "summary": summarize(records)}


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for record in records:
        counts[record["difference"]] = counts.get(record["difference"], 0) + 1
    timings: dict[str, list[float]] = {}
    for record in records:
        for key, value in (record.get("timings_ms") or {}).items():
            timings.setdefault(key, []).append(float(value))
    return {
        "runs": len(records),
        "differences": counts,
        "agreement": counts.get(MATCH, 0) / len(records) if records else None,
        "timings_ms": {
            key: {
                "mean": round(sum(values) / len(values), 3),
                "max": round(max(values), 3),
                "samples": len(values),
            }
            for key, values in sorted(timings.items())
        },
    }


def _oracle_dir(output_root: Path, run: HistoricalRun) -> Path:
    """Oracle evidence for a historical run is written to the new analysis dir, never to the run."""

    target = output_root / "runs" / run.run_key
    target.mkdir(parents=True, exist_ok=True)
    return target


def _load_eval_config():  # noqa: ANN202 - thin loader
    from atlasos.config import load_config

    return load_config(REPO_ROOT / "experiments" / "configs" / "eval-benchmark.yaml")


async def main_async(argv: list[str] | None = None) -> dict[str, Any]:
    parser = argparse.ArgumentParser(description="R1-2H oracle consistency check")
    parser.add_argument("--stage", action="append", default=[])
    parser.add_argument("--rounds", default="")
    parser.add_argument("--samples", default="")
    parser.add_argument("--limit", type=int, default=8)
    args = parser.parse_args(argv)
    stages = args.stage or ["stage-r1-1"]
    rounds = [item for item in args.rounds.split(",") if item]
    samples = [item for item in args.samples.split(",") if item]
    runs: list[HistoricalRun] = []
    for stage in stages:
        runs.extend(
            discover(
                stage,
                required_samples=samples or None,
                rounds=rounds or None,
            )
        )
    runs = [run for run in runs if run.workspace is not None][: args.limit]
    config = _load_eval_config()
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    out = RESULTS_ROOT / f"r12h-{stamp}"
    out.mkdir(parents=True, exist_ok=True)
    payload = await compare(runs, image=config.sandbox.image, output_root=out)
    payload["generated_at"] = stamp
    payload["runs"] = [run.as_dict() for run in runs]
    (out / "consistency.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps(payload["summary"], indent=2, sort_keys=True))
    print(f"consistency written to {out}")
    return payload


def main(argv: list[str] | None = None) -> dict[str, Any]:
    return asyncio.run(main_async(argv))


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    main()
