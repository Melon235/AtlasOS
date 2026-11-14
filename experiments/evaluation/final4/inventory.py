# ruff: noqa: E501
"""Inventory every real Final-4 run directory and reconcile tokens, cost and resources.

One entry per ``result.json``. Nothing is inferred: fields that a run did not record are reported as
``None`` and counted as missing, and the run id is used as the identity key so a replicated unit is
never counted as an additional unit.

    python -m experiments.evaluation.final4.inventory            # table
    python -m experiments.evaluation.final4.inventory --json    # machine-readable
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
RUNS_ROOT = REPO_ROOT / "experiments" / "runs" / "final4"
OUT = REPO_ROOT / "experiments" / "results" / "final4" / "run_inventory.json"

#: Provider price basis recorded in ``experiments/configs/eval-benchmark.yaml``.
PRICE_BASIS = "deepseek pricing table in the config; estimated_cost_usd is the run's own figure"


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _entry(result_path: Path) -> dict[str, Any]:
    data = _load(result_path)
    metrics = data.get("metrics") or {}
    oracle = data.get("oracle") or {}
    identity = data.get("identity") or {}
    environment = data.get("environment") or {}
    resolved = environment.get("context_resolved") or {}
    hidden = ((oracle.get("details") or {}).get("hidden_tests")) or {}
    timings = oracle.get("timings_ms") or {}
    workers = data.get("worker_results") or {}
    hit = metrics.get("cache_hit_tokens")
    miss = metrics.get("cache_miss_tokens")
    return {
        "run_dir": str(result_path.parent.relative_to(RUNS_ROOT)),
        "run_id": identity.get("run_id"),
        "repetition_id": identity.get("repetition_id"),
        "task_id": identity.get("sample_id"),
        "mode": data.get("mode"),
        "git_commit": identity.get("git_commit"),
        "config_digest": identity.get("config_digest"),
        "dataset_version": identity.get("dataset_version"),
        "started_at": identity.get("started_at"),
        "context_builder": resolved.get("builder"),
        "context_policy": resolved.get("policy"),
        "delegation_route_mode": environment.get("delegation_route_mode"),
        "worker_ids": sorted(workers),
        "worker_count": len(workers),
        "cache_hit_tokens": hit,
        "cache_miss_tokens": miss,
        "prompt_tokens_total": (hit + miss) if hit is not None and miss is not None else None,
        "provider_prompt_tokens_field": metrics.get("prompt_tokens"),
        "completion_tokens": metrics.get("completion_tokens"),
        "estimated_cost_usd": metrics.get("estimated_cost_usd"),
        "latency_ms": metrics.get("latency_ms"),
        "model_requests": metrics.get("provider_calls"),
        "model_turns": metrics.get("model_turns"),
        "tool_calls": metrics.get("tool_calls"),
        "tool_search_calls": metrics.get("tool_search_calls"),
        "dispatch_calls": metrics.get("dispatch_calls"),
        "oracle_passed": oracle.get("passed"),
        "oracle_execution_mode": oracle.get("execution_mode"),
        "oracle_failure_kind": oracle.get("failure_kind"),
        "oracle_sandbox_id": hidden.get("sandbox_id"),
        "oracle_sandbox_creation_ms": timings.get("creation_ms"),
        "oracle_sandbox_cleanup_ms": timings.get("cleanup_ms"),
        "oracle_cleanup_failure": oracle.get("cleanup_failure"),
        # ``metrics.sandbox_created`` is a *boolean* per run: ``run_sample`` calls
        # ``ProjectSandboxManager.ensure_binding`` exactly once and every worker of the run shares
        # that one workspace sandbox. The oracle runs in its own, separately created container.
        "task_sandbox_created": metrics.get("sandbox_created"),
        "task_sandbox_destroyed": metrics.get("sandbox_destroyed"),
        "sandbox_cleanup_error": (data.get("safety") or {}).get("cleanup_error"),
        "host_isolation_violations": metrics.get("host_isolation_violations"),
        "patch_digest": data.get("patch_digest"),
        "success": data.get("success"),
        "failure_class": data.get("failure_class"),
    }


def declared_artifacts_check(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Would revision 3's added oracle check change any verdict on an already executed run?

    ``BenchmarkTask.required_artifacts`` feeds ``oracle``'s ``required_artifacts_present``. Before
    revision 3 it was ``()``, so the check was vacuous. This re-evaluates the check against each run's
    *exported* workspace — the same bytes the oracle saw, read back from disk, not a re-run.
    """

    from experiments.evaluation.final4.validation_tasks import TASKS

    required = {task.task_id: set(task.deliverables) for task in TASKS}
    checked: list[dict[str, Any]] = []
    for row in rows:
        task_id = str(row["task_id"])
        wanted = required.get(task_id, set())
        export = RUNS_ROOT / str(row["run_dir"]) / "workspace"
        present = (
            {path.name for path in export.rglob("*") if path.is_file()}
            if export.is_dir()
            else set()
        )
        missing = sorted(wanted - present)
        checked.append(
            {
                "run_id": row["run_id"],
                "task_id": task_id,
                "declared_artifacts": sorted(wanted),
                "export_available": export.is_dir(),
                "missing": missing,
                "required_artifacts_present_if_declared": (not missing)
                if export.is_dir()
                else None,
            }
        )
    verdicts = [item["required_artifacts_present_if_declared"] for item in checked]
    return {
        "check": "oracle.required_artifacts_present re-evaluated over the exported workspaces",
        "runs_checked": len(checked),
        "export_missing": sum(1 for item in checked if not item["export_available"]),
        "would_pass": sum(1 for verdict in verdicts if verdict is True),
        "would_fail": sum(1 for verdict in verdicts if verdict is False),
        "verdict_neutral": all(verdict is True for verdict in verdicts) and bool(checked),
        "entries": checked,
    }


def build() -> dict[str, Any]:
    rows = [_entry(path) for path in sorted(RUNS_ROOT.rglob("result.json"))]
    ids = [row["run_id"] for row in rows]
    duplicates = [run_id for run_id, count in Counter(ids).items() if count > 1]
    task_sandboxes = sum(1 for row in rows if row["task_sandbox_created"])
    task_destroyed = sum(1 for row in rows if row["task_sandbox_destroyed"])
    oracle_sandboxes = sum(1 for row in rows if row["oracle_sandbox_id"])
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "runtime_sha": "93cbd98f9407e7358e997878238520dcac15827a",
        "runs": len(rows),
        "distinct_run_ids": len(set(ids)),
        "duplicate_run_ids": duplicates,
        "totals": {
            "prompt_tokens_total": sum(int(row["prompt_tokens_total"] or 0) for row in rows),
            "cache_hit_tokens": sum(int(row["cache_hit_tokens"] or 0) for row in rows),
            "cache_miss_tokens": sum(int(row["cache_miss_tokens"] or 0) for row in rows),
            "completion_tokens": sum(int(row["completion_tokens"] or 0) for row in rows),
            "estimated_cost_usd": round(
                sum(float(row["estimated_cost_usd"] or 0) for row in rows), 8
            ),
        },
        "resources": {
            "containers_per_run": "1 shared workspace sandbox for all workers + 1 independent oracle sandbox",
            "task_sandboxes_created": task_sandboxes,
            "task_sandboxes_destroyed": task_destroyed,
            "oracle_sandboxes_created": oracle_sandboxes,
            "containers_total": task_sandboxes + oracle_sandboxes,
            "oracle_cleanup_failures": sum(
                1 for row in rows if row["oracle_cleanup_failure"] is not None
            ),
            "safety_cleanup_errors": sum(
                1 for row in rows if row["sandbox_cleanup_error"] is not None
            ),
            "host_isolation_violations": sum(
                int(row["host_isolation_violations"] or 0) for row in rows
            ),
        },
        "cost_basis": PRICE_BASIS,
        "declared_artifacts_check": declared_artifacts_check(rows),
        "entries": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true", help="print the full inventory")
    parser.add_argument("--write", action="store_true", help=f"write {OUT.name}")
    args = parser.parse_args()

    payload = build()
    if args.write:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    print(
        f"runs={payload['runs']} distinct={payload['distinct_run_ids']} "
        f"duplicates={payload['duplicate_run_ids']}"
    )
    header = "{:<46}{:>16}{:>8}{:>7}{:>7}{:>9}{:>7}{:>9}{:>8}{:>7}"
    print(
        header.format("run_dir", "run_id", "w", "hit", "miss", "out", "orc", "cost", "sb", "orcSb")
    )
    for row in payload["entries"]:
        print(
            header.format(
                str(row["run_dir"])[:45],
                str(row["run_id"]),
                str(row["worker_count"]),
                str(row["cache_hit_tokens"]),
                str(row["cache_miss_tokens"]),
                str(row["completion_tokens"]),
                "PASS" if row["oracle_passed"] else "FAIL",
                f"{row['estimated_cost_usd'] or 0:.5f}",
                f"{row['task_sandbox_destroyed']}/{row['task_sandbox_created']}",
                "yes" if row["oracle_sandbox_id"] else "no",
            )
        )
    totals = payload["totals"]
    print(
        f"tokens total={totals['prompt_tokens_total']} "
        f"(hit={totals['cache_hit_tokens']} miss={totals['cache_miss_tokens']}) "
        f"completion={totals['completion_tokens']} cost={totals['estimated_cost_usd']}"
    )
    print(json.dumps(payload["resources"], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
