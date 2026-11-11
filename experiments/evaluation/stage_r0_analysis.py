"""Stage R0: shadow audit of the delegation router plus A0/A1/A2 run analysis.

Two read-only commands:

``audit``
    Route every Stage E1 candidate plan offline (no model, no sandbox, no worker) and write
    ``shadow_audit.csv`` / ``shadow_audit.json``. It also re-runs the router with a different
    ``max_parallel_workers`` to show policy sensitivity without changing the frozen policy.

``runs``
    Summarise one Stage R0 round (``experiments/results/stage-r0/<round>``) into ``runs.csv``,
    ``workers.csv`` and ``summary.json``.

Both commands never write into a run directory and never call a provider.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any

from experiments.evaluation.stage_e_tasks import DEFINITIONS, variants
from experiments.evaluation.stage_g2b_diagnosis import conversations
from experiments.evaluation.stage_g3_3_analysis import _worker_turns, analyze_worker
from experiments.evaluation.stage_g3_failure_audit import is_oracle_probe
from experiments.evaluation.stage_r0_routing import build_proposal, routing_policy

from atlasos.workers import WorkerCatalog
from atlasos.workers.routing import (
    EvidenceKind,
    capabilities_from_catalog,
    decision_to_dict,
    max_wave_width,
    route_delegation,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
RESULTS_ROOT = REPO_ROOT / "experiments" / "results" / "stage-r0"
AUDIT_ROOT = RESULTS_ROOT / "audit"

SENSITIVITY_MAX_PARALLEL = 3


def _a1_task(sample_id: str) -> Any:
    for definition in DEFINITIONS:
        if definition.sample_id == sample_id:
            return variants(definition)[1]
    raise KeyError(sample_id)


def audit_rows() -> list[dict[str, Any]]:
    catalog = capabilities_from_catalog(WorkerCatalog())
    policy = routing_policy()
    wide = routing_policy()
    wide = type(wide)(
        version=f"{wide.version}+mp{SENSITIVITY_MAX_PARALLEL}",
        max_parallel_workers=SENSITIVITY_MAX_PARALLEL,
        allow_shared_mutation=wide.allow_shared_mutation,
        allow_unknown_risk=wide.allow_unknown_risk,
        require_goal_coverage=wide.require_goal_coverage,
        require_context_evidence=wide.require_context_evidence,
        min_tasks_for_delegation=wide.min_tasks_for_delegation,
        allow_single_read_only_delegation=wide.allow_single_read_only_delegation,
    )

    rows: list[dict[str, Any]] = []
    for definition in DEFINITIONS:
        task = variants(definition)[1]
        proposal = build_proposal(task, task.workers, proposal_id=f"{task.sample_id}:candidate")
        decision = route_delegation(proposal, catalog, policy)
        alternative = route_delegation(proposal, catalog, wide)

        write_tasks = sorted(
            {
                item.subject_task_ids[0]
                for item in proposal.evidence
                if item.kind is EvidenceKind.WRITE_PATHS
            }
        )
        interface_tasks = sorted(
            {
                item.subject_task_ids[0]
                for item in proposal.evidence
                if item.kind is EvidenceKind.INTERFACE_SYMBOLS
            }
        )
        context_tasks = sorted(
            {
                item.subject_task_ids[0]
                for item in proposal.evidence
                if item.kind is EvidenceKind.CONTEXT and item.status.value == "proven"
            }
        )
        edges = [
            f"{dependency}->{step.task_id}"
            for step in task.workers
            for dependency in step.depends_on
        ]
        rows.append(
            {
                "sample_id": definition.sample_id,
                "candidate_task_count": len(task.workers),
                "candidate_dag_edges": ";".join(edges),
                "parallel_wave_width": max_wave_width(
                    [
                        type(
                            "T",
                            (),
                            {
                                "task_id": step.task_id,
                                "depends_on": tuple(step.depends_on),
                            },
                        )()
                        for step in task.workers
                    ]
                ),
                "route_kind": decision.route.value,
                "reason_codes": ";".join(decision.reason_codes),
                "blocked_gates": ";".join(decision.blocked_gates),
                "evidence_status": ";".join(
                    sorted({item.status.value for item in proposal.evidence})
                ),
                "context_sufficiency": (
                    "proven" if len(context_tasks) == len(task.workers) else "partial"
                ),
                "tasks_with_write_evidence": len(write_tasks),
                "tasks_with_interface_evidence": len(interface_tasks),
                "policy_version": decision.policy_version,
                "policy_digest": decision.policy_digest,
                "route_at_max_parallel_3": alternative.route.value,
                "reason_codes_at_max_parallel_3": ";".join(alternative.reason_codes),
                "decision": decision_to_dict(decision),
            }
        )
    return rows


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def audit(output: Path) -> int:
    rows = audit_rows()
    serialisable = [{key: value for key, value in row.items() if key != "decision"} for row in rows]
    write_csv(output / "shadow_audit.csv", serialisable)
    (output / "shadow_audit.json").write_text(
        json.dumps(
            {
                "rows": rows,
                "route_distribution": dict(Counter(row["route_kind"] for row in rows)),
                "at_max_parallel_3": dict(Counter(row["route_at_max_parallel_3"] for row in rows)),
                "reason_distribution": dict(
                    Counter(
                        code for row in rows for code in str(row["reason_codes"]).split(";") if code
                    )
                ),
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "route_distribution": dict(Counter(row["route_kind"] for row in rows)),
                "at_max_parallel_3": dict(Counter(row["route_at_max_parallel_3"] for row in rows)),
                "reason_distribution": dict(
                    Counter(
                        code for row in rows for code in str(row["reason_codes"]).split(";") if code
                    )
                ),
            },
            indent=2,
            sort_keys=True,
        )
    )
    for row in rows:
        print(
            f"{row['sample_id']:16s} width={row['parallel_wave_width']} "
            f"route={row['route_kind']:16s} reasons={row['reason_codes']} "
            f"wide3={row['route_at_max_parallel_3']}"
        )
    return 0


# --------------------------------------------------------------------------- run analysis


def latest_round() -> Path:
    rounds = sorted(path for path in RESULTS_ROOT.glob("r0-*") if path.is_dir())
    if not rounds:
        raise SystemExit(f"no Stage R0 round under {RESULTS_ROOT}")
    return rounds[-1]


def _load(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def arm_runs(round_dir: Path) -> list[tuple[str, int, str, Path]]:
    manifest = _load(round_dir / "manifest.json") or {}
    arms: list[tuple[str, int, str, Path]] = []
    for pair in manifest.get("pairs") or []:
        for variant, result in (pair.get("runs") or {}).items():
            run_dir = result.get("run_dir")
            if not run_dir or not Path(run_dir).is_dir():
                continue
            arms.append(
                (
                    str(pair.get("definition")),
                    int(pair.get("repetition") or 0),
                    str(variant),
                    Path(run_dir),
                )
            )
    arms.sort(key=lambda item: (item[0], item[1], item[2]))
    return arms


def run_rows(
    round_dir: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    workers: list[dict[str, Any]] = []
    probes: list[dict[str, Any]] = []
    for sample_id, repetition, variant, run_dir in arm_runs(round_dir):
        result = _load(run_dir / "result.json") or {}
        identity = result.get("identity") or {}
        metrics = result.get("metrics") or {}
        extra = metrics.get("extra") or metrics
        oracle = result.get("oracle") or {}
        worker_results = result.get("worker_results") or {}
        routing = extra.get("routing") or {}
        decision = routing.get("decision") or {}
        transcripts = conversations(run_dir)
        workspace = run_dir / "workspace"

        for name, info in worker_results.items():
            key = name.split(":")[-1].rsplit("-", 1)[-1]
            row = analyze_worker(
                run_id=str(identity.get("run_id") or run_dir.name),
                sample_id=sample_id,
                repetition=repetition,
                mode=variant,
                name=str(name),
                info=info,
                messages=transcripts.get(key) or [],
                workspace=workspace,
            )
            workers.append(
                {
                    "run_id": row.run_id,
                    "sample_id": sample_id,
                    "repetition": repetition,
                    "arm": variant,
                    "worker": row.worker,
                    "success": row.success,
                    "stop_code": row.stop_code,
                    "tool_turns": row.tool_turns,
                    "explore_turns": row.explore_turns,
                    "probe_turns": row.probe_turns,
                    "first_implement_turn": row.first_implement_turn,
                    "deliverable_state": row.deliverable_state,
                }
            )

        for task_key, messages in transcripts.items():
            for call in _worker_turns(messages):
                if not is_oracle_probe(str(call["tool"]), dict(call["input"])):
                    continue
                probes.append(
                    {
                        "run_id": str(identity.get("run_id") or run_dir.name),
                        "sample_id": sample_id,
                        "repetition": repetition,
                        "arm": variant,
                        "task": task_key,
                        "command": " ".join(str(call["input"].get("command") or "").split())[:180],
                    }
                )

        hit = metrics.get("cache_hit_tokens")
        miss = metrics.get("cache_miss_tokens")
        rows.append(
            {
                "run_id": str(identity.get("run_id") or run_dir.name),
                "sample_id": sample_id,
                "repetition": repetition,
                "arm": variant,
                "oracle_success": oracle.get("passed"),
                "task_success": result.get("failure_class") == "TASK_SUCCESS",
                "harness_class": result.get("failure_class"),
                "workers": len(worker_results),
                "worker_success": sum(1 for info in worker_results.values() if info.get("success")),
                "model_turns": int(metrics.get("model_turns") or 0),
                "tool_calls": sum(len(_worker_turns(m)) for m in transcripts.values()),
                "prompt_tokens": int(metrics.get("prompt_tokens") or 0),
                "completion_tokens": int(metrics.get("completion_tokens") or 0),
                "cache_hit_tokens": hit,
                "cache_miss_tokens": miss,
                "cost_usd": metrics.get("estimated_cost_usd"),
                "latency_ms": float(metrics.get("latency_ms") or 0.0),
                "budget_exceeded": result.get("failure_class") == "BUDGET_EXCEEDED",
                "routed_route": decision.get("route"),
                "routed_reasons": ";".join(decision.get("reason_codes") or []),
                "execution_mode": routing.get("execution_mode"),
                "executed_worker_count": routing.get("executed_worker_count"),
                "route_digest": routing.get("route_digest"),
                "failed_checks": ";".join(
                    sorted(
                        name for name, value in (oracle.get("checks") or {}).items() if not value
                    )
                ),
                "run_dir": str(run_dir),
            }
        )
    return rows, workers, probes


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 6) if values else None


def summarise(rows: list[dict[str, Any]], workers: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for arm in ("a0", "a1", "a2"):
        selected = [row for row in rows if row["arm"] == arm]
        if not selected:
            continue
        group = [row for row in workers if row["arm"] == arm]
        hits = [row["cache_hit_tokens"] for row in selected]
        misses = [row["cache_miss_tokens"] for row in selected]
        complete = all(value is not None for value in hits + misses)
        hit_total = sum(int(v) for v in hits if v is not None) if complete else None
        miss_total = sum(int(v) for v in misses if v is not None) if complete else None
        summary[arm] = {
            "runs": len(selected),
            "oracle_pass": sum(1 for row in selected if row["oracle_success"]),
            "task_success": sum(1 for row in selected if row["task_success"]),
            "worker_success": sum(1 for row in group if row["success"]),
            "workers": len(group),
            "prompt_tokens": sum(row["prompt_tokens"] for row in selected),
            "completion_tokens": sum(row["completion_tokens"] for row in selected),
            "cache_hit_tokens": hit_total,
            "cache_miss_tokens": miss_total,
            "cache_hit_ratio": (
                round(hit_total / (hit_total + miss_total), 4)
                if hit_total is not None and miss_total is not None and hit_total + miss_total
                else None
            ),
            "cost_usd": round(sum(float(row["cost_usd"] or 0.0) for row in selected), 6),
            "avg_cost_per_run": _mean([float(row["cost_usd"] or 0.0) for row in selected]),
            "model_turns": sum(row["model_turns"] for row in selected),
            "tool_calls": sum(row["tool_calls"] for row in selected),
            "budget_exceeded": sum(1 for row in selected if row["budget_exceeded"]),
            "routes": dict(Counter(str(row["routed_route"]) for row in selected)),
            "execution_modes": dict(Counter(str(row["execution_mode"]) for row in selected)),
            "probe_turns": sum(row["probe_turns"] for row in group),
            "deliverable_states": dict(Counter(row["deliverable_state"] for row in group)),
        }
    summary["per_task"] = {}
    for sample_id in sorted({row["sample_id"] for row in rows}):
        entry: dict[str, Any] = {}
        for arm in ("a0", "a1", "a2"):
            selected = [row for row in rows if row["sample_id"] == sample_id and row["arm"] == arm]
            if not selected:
                continue
            entry[arm] = {
                "oracle_pass": sum(1 for row in selected if row["oracle_success"]),
                "runs": len(selected),
                "prompt_tokens": sum(row["prompt_tokens"] for row in selected),
                "cost_usd": round(sum(float(row["cost_usd"] or 0.0) for row in selected), 6),
                "model_turns": sum(row["model_turns"] for row in selected),
                "route": sorted({str(row["routed_route"]) for row in selected}),
                "failed_checks": sorted(
                    {
                        check
                        for row in selected
                        for check in str(row["failed_checks"]).split(";")
                        if check
                    }
                ),
            }
        summary["per_task"][sample_id] = entry
    return summary


def runs(output: Path, round_dir: Path) -> int:
    rows, workers, probes = run_rows(round_dir)
    write_csv(output / "runs.csv", rows)
    write_csv(output / "workers.csv", workers)
    write_csv(output / "probes.csv", probes)
    summary = summarise(rows, workers)
    summary["round"] = str(round_dir)
    (output / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    for row in rows:
        print(
            f"{row['sample_id']} rep={row['repetition']} {row['arm']} "
            f"oracle={row['oracle_success']} class={row['harness_class']} "
            f"route={row['routed_route']} exec={row['execution_mode']} "
            f"workers={row['workers']} turns={row['model_turns']} cost={row['cost_usd']}"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="atlas-stage-r0-analysis")
    sub = parser.add_subparsers(dest="command", required=True)
    audit_parser = sub.add_parser("audit")
    audit_parser.add_argument("--output", type=Path, default=AUDIT_ROOT)
    runs_parser = sub.add_parser("runs")
    runs_parser.add_argument("--round", type=Path, default=None)
    runs_parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)

    if args.command == "audit":
        args.output.mkdir(parents=True, exist_ok=True)
        return audit(args.output)
    round_dir: Path = args.round or latest_round()
    output: Path = args.output or (round_dir / "analysis")
    output.mkdir(parents=True, exist_ok=True)
    return runs(output, round_dir)


if __name__ == "__main__":
    raise SystemExit(main())
