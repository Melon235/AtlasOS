# ruff: noqa: E501
"""Collect real final-4 run directories into the published result files.

Reads every ``experiments/runs/final4/**/result.json`` (git-ignored raw evidence) and writes the
sanitised summaries under ``experiments/results/final4/``. Nothing is fabricated: a unit that has no
run directory stays ``NOT_RUN`` with its original reason.

Configuration identification is explicit and auditable:

* ``mode == "multi_worker"`` → ``R1`` (multi over the frozen candidate DAG)
* single-worker with the R2 settings digest → ``R2`` (router in ``selective`` mode chose DIRECT)
* any other single-worker run → ``R0``
"""

from __future__ import annotations

import csv
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
RUNS_ROOT = REPO_ROOT / "experiments" / "runs" / "final4"
OUT = REPO_ROOT / "experiments" / "results" / "final4"

#: Settings digest of the R2 unit (delegation_route_mode="selective"); resolved from the runs below.
R2_DIGEST_PREFIX = "822a1797"


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _summarise(result_path: Path) -> dict[str, Any]:
    data = _load(result_path)
    metrics = data.get("metrics") or {}
    oracle = data.get("oracle") or {}
    budget = metrics.get("budget") or {}
    identity = data.get("identity") or {}
    workers = data.get("worker_results") or []
    timings = oracle.get("timings_ms") or {}
    digest = str(identity.get("config_digest", ""))
    mode = str(data.get("mode"))

    if mode == "multi_worker":
        configuration = "R1"
    elif digest.startswith(R2_DIGEST_PREFIX):
        configuration = "R2"
    else:
        configuration = "R0"

    used = None
    if budget.get("limit_tokens") and budget.get("remaining_tokens") is not None:
        used = int(budget["limit_tokens"]) - int(budget["remaining_tokens"])

    return {
        "unit": f"{data.get('sample_id')}-{configuration}",
        "task_id": data.get("sample_id"),
        "configuration": configuration,
        "mode": mode,
        "runtime_sha": "93cbd98f9407e7358e997878238520dcac15827a",
        "run_dir": str(result_path.parent),
        "status": "EXECUTED",
        "runtime_status": "completed" if data.get("success") else "failed",
        "agent_success": bool(data.get("agent_success")),
        "success": bool(data.get("success")),
        "failure_class": data.get("failure_class"),
        "failure_reason": data.get("failure_reason"),
        "oracle_status": oracle.get("failure_kind"),
        "oracle_pass": bool(oracle.get("passed")),
        "oracle_execution": oracle.get("execution_mode"),
        "oracle_checks": oracle.get("checks"),
        "worker_count": len(workers),
        "worker_ids": [w.get("task_id") if isinstance(w, dict) else str(w) for w in workers],
        "tool_calls": len(data.get("tool_calls") or []),
        "prompt_tokens": metrics.get("cache_miss_tokens"),
        "cache_hit_tokens": metrics.get("cache_hit_tokens"),
        "cache_miss_tokens": metrics.get("cache_miss_tokens"),
        "completion_tokens": metrics.get("completion_tokens"),
        "cache_metrics_complete": metrics.get("cache_metrics_complete"),
        "budget_limit_tokens": budget.get("limit_tokens"),
        "budget_used_tokens": used,
        "budget_mode": budget.get("mode"),
        "budget_rejections": budget.get("admission_rejections"),
        "budget_requests": budget.get("requests_completed"),
        "oracle_ms": timings.get("total_ms"),
        "sandbox_cleanup_ms": timings.get("cleanup_ms"),
        "config_digest": digest,
        "workspace_patch_digest": data.get("patch_digest"),
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    rows = [_summarise(path) for path in sorted(RUNS_ROOT.rglob("result.json"))]
    rows.sort(key=lambda row: (str(row["task_id"]), str(row["configuration"])))

    # The frozen matrix: 12 units. Anything without a run keeps its NOT_RUN reason.
    planned: list[dict[str, Any]] = []
    for task_id in ("validation-serial", "validation-parallel", "validation-coupled"):
        for configuration in ("N0", "R0", "R1", "R2"):
            unit = f"{task_id}-{configuration}"
            executed = next((row for row in rows if row["unit"] == unit), None)
            if executed is not None:
                planned.append(executed)
            else:
                reason = (
                    "NOT_RUN: native arm still runs on the host; neutral sandbox wiring for "
                    "baseline.py is not complete, so N0 was skipped rather than downgraded"
                    if configuration == "N0"
                    else "NOT_RUN: outside this stage's executed subset"
                )
                planned.append(
                    {
                        "unit": unit,
                        "task_id": task_id,
                        "configuration": configuration,
                        "status": "NOT_RUN",
                        "blocked_reason": reason,
                        "oracle_pass": None,
                    }
                )

    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "runtime_sha": "93cbd98f9407e7358e997878238520dcac15827a",
        "executed": len(rows),
        "planned": len(planned),
        "runs": planned,
    }
    (OUT / "pilot_results.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
    )
    fieldnames = sorted({key for row in planned for key in row})
    with (OUT / "pilot_results.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in planned:
            writer.writerow({key: row.get(key) for key in fieldnames})

    by_task: dict[str, dict[str, Any]] = {}
    for row in planned:
        by_task.setdefault(str(row["task_id"]), {})[str(row["configuration"])] = {
            "status": row["status"],
            "oracle_pass": row.get("oracle_pass"),
            "worker_count": row.get("worker_count"),
            "budget_used_tokens": row.get("budget_used_tokens"),
        }
    router = {
        "router_policy_version": "r0.1.0",
        "route_mode": "selective",
        "cells": by_task,
        "observations": {
            "r2_actual_path": [
                {
                    "task_id": row["task_id"],
                    "worker_count": row.get("worker_count"),
                    "oracle_pass": row.get("oracle_pass"),
                }
                for row in planned
                if row["configuration"] == "R2" and row["status"] == "EXECUTED"
            ],
            "r1_multi": [
                {
                    "task_id": row["task_id"],
                    "worker_count": row.get("worker_count"),
                    "oracle_pass": row.get("oracle_pass"),
                    "failure_class": row.get("failure_class"),
                }
                for row in planned
                if row["configuration"] == "R1" and row["status"] == "EXECUTED"
            ],
        },
        "regret_available": False,
        "note": "regret needs the full task x configuration matrix; only the executed subset is present",
    }
    (OUT / "router_comparison.json").write_text(
        json.dumps(router, indent=2, sort_keys=True), encoding="utf-8"
    )

    for row in planned:
        if row["status"] == "EXECUTED":
            print(
                row["unit"],
                "| workers",
                row["worker_count"],
                "| oracle",
                row["oracle_pass"],
                "|",
                row["failure_class"],
                "| tok",
                row["budget_used_tokens"],
            )
        else:
            print(row["unit"], "| NOT_RUN")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
