# ruff: noqa: E501
"""Collect corrected Final-4 runs (task set revision 2) into their own result files.

Differences from ``collect.py``, which published the 2026-11-14 pilot and is left unchanged:

* it writes ``pilot_results_v2.json`` / ``router_comparison_v2.json`` and never rewrites the legacy
  ``pilot_results.json`` / ``pilot_results.csv`` / ``router_comparison.json``;
* it classifies a run from what the run *recorded* (``environment.context_resolved`` and
  ``environment.delegation_route_mode``), not from a settings-digest prefix, and a run that recorded
  no resolved context configuration is labelled ``EXPLORATORY_LEGACY_PILOT`` instead of being folded
  into a cell;
* it publishes the Context Plane evidence per unit, including whether each declared dependency
  actually appeared in the rendered task prompt exactly once.

    python -m experiments.evaluation.final4.collect_v2
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
RUNS_ROOT = REPO_ROOT / "experiments" / "runs" / "final4"
OUT = REPO_ROOT / "experiments" / "results" / "final4"

TASK_IDS = ("validation-serial", "validation-parallel", "validation-coupled")
#: The corrected matrix. ``X1`` is not listed: it is ``<task>-R1`` by definition.
CONFIGURATIONS = ("N0", "X0", "R0", "R1", "R2")
RUNTIME_SHA = "93cbd98f9407e7358e997878238520dcac15827a"


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _optional(path: Path) -> dict[str, Any]:
    return _load(path) if path.is_file() else {}


def _repetition(directory: str) -> int:
    """Run directories are named ``r<repetition>-<run_id>``."""

    head = directory.split("-", 1)[0]
    return int(head[1:]) if head.startswith("r") and head[1:].isdigit() else 0


def _configuration(data: dict[str, Any]) -> str:
    """Classify from recorded evidence only; ``LEGACY`` when the run recorded nothing to classify."""

    environment = data.get("environment") or {}
    resolved = environment.get("context_resolved")
    if not isinstance(resolved, dict):
        return "LEGACY"
    route_mode = str(environment.get("delegation_route_mode", "off"))
    workers = data.get("worker_results") or {}
    if (
        resolved.get("expectation") == "upstream_default"
        or resolved.get("policy") == "upstream_default"
    ):
        return "X0" if len(workers) > 1 else "R0"
    if len(workers) > 1:
        return "R1"
    return "R2" if route_mode == "selective" else "R0"


def _context_summary(
    data: dict[str, Any], record: dict[str, Any], run_environment: dict[str, Any]
) -> dict[str, Any]:
    metrics = data.get("metrics") or {}
    context = (metrics.get("extra") or {}).get("context") or (record.get("evidence") or {})
    environment = data.get("environment") or {}
    resolved = (
        (environment.get("context_resolved") or {})
        or (record.get("resolved") or {})
        or (run_environment.get("context_resolved") or {})
    )
    tasks = context.get("tasks") or []
    return {
        "enabled": bool(context.get("enabled")) or resolved.get("builder") is not None,
        "builder": context.get("builder") or resolved.get("builder"),
        "builder_invocations": context.get("builder_invocations"),
        "deep_evidence": (
            "ARCHIVED (context.json)"
            if record
            else "MISSING: recorded before the runner persisted context.json; the run's own "
            "run.json still proves which builder was resolved and invoked"
        ),
        "prompt_builder_source": resolved.get("prompt_builder_source"),
        "policy": resolved.get("policy"),
        "renderer": resolved.get("renderer"),
        "transform": resolved.get("transform"),
        "config_path": resolved.get("config_path"),
        "handoff_mode": resolved.get("handoff_mode"),
        "tool_error_mode": resolved.get("tool_error_mode"),
        "store_block_ids": context.get("store_block_ids"),
        "tasks": [
            {
                "task_id": item.get("task_id"),
                "declared_dependencies": item.get("declared_dependencies"),
                "rendered_dependency_order": item.get("rendered_dependency_order"),
                "selected_block_ids": item.get("selected_block_ids"),
                "selected_block_versions": item.get("selected_block_versions"),
                "transform_name": item.get("transform_name"),
                "context_layout_hash": item.get("context_layout_hash"),
                "task_prompt_hash": item.get("task_prompt_hash"),
                "dependencies": item.get("dependencies"),
            }
            for item in tasks
        ],
        "dependencies": [
            {
                "task_id": item.get("task_id"),
                "dependency": fact.get("task_id"),
                "in_rendered_prompt": fact.get("in_rendered_prompt"),
                "injection_count": fact.get("injection_count"),
                "duplicate_injection": fact.get("duplicate_injection"),
            }
            for item in tasks
            for fact in (item.get("dependencies") or [])
        ],
        "provider_request_evidence": (
            "HASH_ONLY (provider_calls.jsonl)" if context.get("provider_requests") else "NONE"
        ),
    }


def _total_input(metrics: dict[str, Any]) -> int | None:
    hit = metrics.get("cache_hit_tokens")
    miss = metrics.get("cache_miss_tokens")
    if hit is None or miss is None:
        return None
    return int(hit) + int(miss)


def _summarise(result_path: Path) -> dict[str, Any]:
    data = _load(result_path)
    # ``metrics.extra`` is not serialised into ``result.json``; the durable Context Plane artifact is
    # ``context.json`` next to it (written by the runner since CORRECTED_EVAL_SHA).
    record = _optional(result_path.parent / "context.json")
    run_environment = (_optional(result_path.parent / "run.json") or {}).get("environment") or {}
    metrics = data.get("metrics") or {}
    oracle = data.get("oracle") or {}
    budget = metrics.get("budget") or {}
    identity = data.get("identity") or {}
    workers = data.get("worker_results") or {}
    timings = oracle.get("timings_ms") or {}
    configuration = _configuration(data)
    used = None
    if budget.get("limit_tokens") and budget.get("remaining_tokens") is not None:
        used = int(budget["limit_tokens"]) - int(budget["remaining_tokens"])
    return {
        "unit": f"{data.get('sample_id')}-{configuration}",
        "task_id": data.get("sample_id"),
        "configuration": configuration,
        "classification": (
            "EXPLORATORY_LEGACY_PILOT" if configuration == "LEGACY" else "CORRECTED_EVAL"
        ),
        "repetition": _repetition(result_path.parent.name),
        "mode": data.get("mode"),
        "runtime_sha": RUNTIME_SHA,
        "run_id": identity.get("run_id"),
        "run_dir": str(result_path.parent),
        "status": "EXECUTED",
        "success": bool(data.get("success")),
        "agent_success": bool(data.get("agent_success")),
        "failure_class": data.get("failure_class"),
        "failure_reason": data.get("failure_reason"),
        "oracle_status": oracle.get("failure_kind"),
        "oracle_pass": bool(oracle.get("passed")),
        "oracle_checks": oracle.get("checks"),
        "oracle_execution": oracle.get("execution_mode"),
        "worker_count": len(workers),
        "tool_calls": len(data.get("tool_calls") or []),
        # ``metrics.prompt_tokens`` is the provider's ``usage.prompt_tokens``. Its meaning is not
        # stable across dates (it equals the miss count in the 2026-11-14 runs and hit+miss in the
        # corrected runs), so the comparable fields are the hit/miss pair and their sum.
        "prompt_tokens_uncached": metrics.get("prompt_tokens"),
        "prompt_tokens_total": _total_input(metrics),
        "cache_hit_tokens": metrics.get("cache_hit_tokens"),
        "cache_miss_tokens": metrics.get("cache_miss_tokens"),
        "completion_tokens": metrics.get("completion_tokens"),
        "cache_metrics_complete": (metrics.get("extra") or {}).get("cache_metrics_complete"),
        "budget_limit_tokens": budget.get("limit_tokens"),
        "budget_used_tokens": used,
        "budget_rejections": budget.get("admission_rejections"),
        "budget_requests": budget.get("requests_completed"),
        "estimated_cost_usd": metrics.get("estimated_cost_usd"),
        "latency_ms": metrics.get("latency_ms"),
        "oracle_ms": timings.get("total_ms"),
        "sandbox_cleanup_ms": timings.get("cleanup_ms"),
        "sandbox_destroyed": metrics.get("sandbox_destroyed"),
        "config_digest": identity.get("config_digest"),
        "workspace_patch_digest": data.get("patch_digest"),
        "routing": (metrics.get("extra") or {}).get("routing"),
        "context": _context_summary(data, record, run_environment),
    }


def _router_replay() -> dict[str, Any]:
    """Deterministic re-evaluation of the frozen router on the frozen proposal.

    The router is a pure function of the proposal, the capability catalog and the policy, so the
    decision can be reproduced exactly. This is labelled as a replay, not as a live record: the live
    ``routing`` block was only persisted from CORRECTED_EVAL_SHA onwards.
    """

    from experiments.evaluation.final4 import runner_entry
    from experiments.evaluation.stage_r0_routing import plan_route, routing_policy

    from atlasos.workers import WorkerCatalog
    from atlasos.workers.routing import capabilities_from_catalog

    capabilities = capabilities_from_catalog(WorkerCatalog())
    replay: dict[str, Any] = {}
    for task_id in TASK_IDS:
        task = next(task for task in runner_entry.VALIDATION_TASKS if task.task_id == task_id)
        routed = plan_route(
            runner_entry.build_task(task, multi=True), capabilities, routing_policy("r0.1.0")
        )
        replay[task_id] = {
            "route": routed.decision.route.value,
            "reason_codes": list(routed.decision.reason_codes),
            "gates": [
                {
                    "gate": gate.gate,
                    "status": gate.status.value,
                    "reason_codes": list(gate.reason_codes),
                }
                for gate in routed.decision.gates
            ],
            "candidate_tasks": len(task.workers),
            "executed_worker_count_if_live": 1 if routed.is_direct else len(task.workers),
            "provenance": "DETERMINISTIC_REPLAY (pure function, same proposal and policy)",
        }
    return replay


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    rows = [_summarise(path) for path in sorted(RUNS_ROOT.rglob("result.json"))]
    corrected = [row for row in rows if row["classification"] == "CORRECTED_EVAL"]
    legacy = [row for row in rows if row["classification"] != "CORRECTED_EVAL"]
    corrected.sort(
        key=lambda row: (str(row["task_id"]), str(row["configuration"]), row["repetition"])
    )
    replications = [row for row in corrected if row["repetition"] > 0]

    planned: list[dict[str, Any]] = []
    for task_id in TASK_IDS:
        for configuration in CONFIGURATIONS:
            unit = f"{task_id}-{configuration}"
            executed = next(
                (row for row in corrected if row["unit"] == unit and row["repetition"] == 0),
                None,
            )
            if executed is not None:
                planned.append(executed)
                continue
            if configuration == "N0":
                reason = (
                    "NOT_RUN: the native arm still runs on the host; neutral sandbox wiring for "
                    "baseline.py is not complete, so N0 is reported as blocked rather than downgraded"
                )
            else:
                reason = "NOT_RUN: no corrected run directory for this unit"
            planned.append(
                {
                    "unit": unit,
                    "task_id": task_id,
                    "configuration": configuration,
                    "classification": "CORRECTED_EVAL",
                    "status": "NOT_RUN",
                    "blocked_reason": reason,
                    "oracle_pass": None,
                }
            )

    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "runtime_sha": RUNTIME_SHA,
        "task_set_revision": 2,
        "dataset_version": "final4-validation-2",
        "config_file": "experiments/configs/final4-context.yaml",
        "x1_equivalence": "X1 == <task>-R1 (same unit, not a separate run)",
        "executed": len(planned) - sum(1 for row in planned if row["status"] != "EXECUTED"),
        "planned": len(planned),
        "run_directories": len(corrected),
        "runs": planned,
        "replications": [
            {
                "unit": row["unit"],
                "repetition": row["repetition"],
                "run_id": row["run_id"],
                "oracle_pass": row["oracle_pass"],
                "worker_count": row["worker_count"],
                "budget_used_tokens": row["budget_used_tokens"],
                "cost_usd": row["estimated_cost_usd"],
                "context_builder": (row["context"] or {}).get("builder"),
                "context_invocations": (row["context"] or {}).get("builder_invocations"),
            }
            for row in replications
        ],
        "exploratory_legacy_pilot": [
            {
                "unit": row["unit"],
                "run_id": row["run_id"],
                "oracle_pass": row["oracle_pass"],
                "worker_count": row["worker_count"],
                "note": "see docs/final-4-legacy-pilot-reclassification.md",
            }
            for row in legacy
        ],
    }
    (OUT / "pilot_results_v2.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
    )

    cells: dict[str, dict[str, Any]] = {}
    for row in planned:
        cells.setdefault(str(row["task_id"]), {})[str(row["configuration"])] = {
            "status": row["status"],
            "worker_count": row.get("worker_count"),
            "oracle_pass": row.get("oracle_pass"),
            "context_builder": (row.get("context") or {}).get("builder"),
            "context_invocations": (row.get("context") or {}).get("builder_invocations"),
            "budget_used_tokens": row.get("budget_used_tokens"),
            "cost_usd": row.get("estimated_cost_usd"),
        }
    router = {
        "router_policy_version": "r0.1.0",
        "router_modified": False,
        "route_mode": "selective",
        "cells": cells,
        "x0_vs_x1": [
            {
                "task_id": task_id,
                "x0": cells.get(task_id, {}).get("X0", {}).get("status"),
                "x1": cells.get(task_id, {}).get("R1", {}).get("status"),
            }
            for task_id in TASK_IDS
        ],
        "router_replay": _router_replay(),
        "note": (
            "X1 is the Atlas Context Plane cell of the same candidate DAG, i.e. <task>-R1. "
            "The live R2 worker count (1) shows the router took DIRECT in every cell; the reason "
            "codes come from the deterministic replay because the live routing block was not yet "
            "persisted on this batch."
        ),
    }
    (OUT / "router_comparison_v2.json").write_text(
        json.dumps(router, indent=2, sort_keys=True), encoding="utf-8"
    )

    for row in planned:
        if row["status"] == "EXECUTED":
            context = row.get("context") or {}
            print(
                row["unit"],
                "| workers",
                row["worker_count"],
                "| oracle",
                row["oracle_pass"],
                "|",
                row["failure_class"],
                "| builder",
                context.get("builder"),
                "| invocations",
                context.get("builder_invocations"),
                "| tok",
                row["budget_used_tokens"],
            )
        else:
            print(row["unit"], "| NOT_RUN")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
