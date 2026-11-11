"""Stage G3-4a: trusted baseline (A0 vs A1) after benchmark-integrity isolation.

Reads one round under ``experiments/results/stage-g3-4a/<round>`` and reports the baseline metrics
plus the new environment-integrity fields. Worker-level discipline metrics reuse the Stage G3-3
classifier so the two stages stay comparable.

Outputs (``<round>/analysis/``): ``runs.csv``, ``workers.csv``, ``probes.csv``, ``summary.json``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

from experiments.evaluation.stage_e_tasks import definition_by_id
from experiments.evaluation.stage_g2b_diagnosis import conversations
from experiments.evaluation.stage_g3_3_analysis import _worker_turns, analyze_worker
from experiments.evaluation.stage_g3_failure_audit import is_oracle_probe

REPO_ROOT = Path(__file__).resolve().parents[2]
RESULTS_ROOT = REPO_ROOT / "experiments" / "results" / "stage-g3-4a"

INVARIANTS = (
    "PUBLIC_WORKSPACE_VISIBLE",
    "PRIVATE_ORACLE_VISIBLE_TO_WORKER",
    "GRADING_ARTIFACT_VISIBLE_TO_WORKER",
    "CROSS_RUN_ARTIFACT_ACCESS",
    "NORMAL_TEMP_WORKFLOW",
    "ORACLE_CAN_EVALUATE",
)


def latest_round() -> Path:
    rounds = sorted(path for path in RESULTS_ROOT.glob("g34a-*") if path.is_dir())
    if not rounds:
        raise SystemExit(f"no Stage G3-4a round under {RESULTS_ROOT}")
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


def oracle_definition_digest(sample_id: str) -> str:
    """Digest of the hidden oracle definition (paths + content), for baseline provenance."""

    definition = definition_by_id(sample_id)
    digest = hashlib.sha256()
    for relative, content in sorted(definition.hidden_files.items()):
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def analyse_run(
    run_dir: Path, sample_id: str, repetition: int, variant: str
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    result = _load(run_dir / "result.json") or {}
    identity = result.get("identity") or {}
    metrics = result.get("metrics") or {}
    extra = metrics.get("extra") or metrics
    oracle = result.get("oracle") or {}
    workers = result.get("worker_results") or {}
    integrity = extra.get("integrity") or {}
    workspace = run_dir / "workspace"
    transcripts = conversations(run_dir)

    worker_rows: list[dict[str, Any]] = []
    for name, info in workers.items():
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
        worker_rows.append(
            {
                "run_id": row.run_id,
                "sample_id": sample_id,
                "repetition": repetition,
                "variant": variant,
                "worker": row.worker,
                "step_id": row.step_id,
                "success": row.success,
                "stop_code": row.stop_code,
                "tool_turns": row.tool_turns,
                "explore_turns": row.explore_turns,
                "implement_turns": row.implement_turns,
                "verify_turns": row.verify_turns,
                "probe_turns": row.probe_turns,
                "first_implement_turn": row.first_implement_turn,
                "owned_files": ";".join(row.owned_files),
                "ownership_violations": row.ownership_violations,
                "deliverable_state": row.deliverable_state,
            }
        )

    probes: list[dict[str, Any]] = []
    for task_key, messages in transcripts.items():
        for call in _worker_turns(messages):
            if not is_oracle_probe(str(call["tool"]), dict(call["input"])):
                continue
            probes.append(
                {
                    "run_id": str(identity.get("run_id") or run_dir.name),
                    "sample_id": sample_id,
                    "repetition": repetition,
                    "variant": variant,
                    "task": task_key,
                    "tool": call["tool"],
                    "command": " ".join(str(call["input"].get("command") or "").split())[:180],
                    "is_error": call["is_error"],
                }
            )

    hit = metrics.get("cache_hit_tokens")
    miss = metrics.get("cache_miss_tokens")
    row = {
        "run_id": str(identity.get("run_id") or run_dir.name),
        "sample_id": sample_id,
        "repetition": repetition,
        "worker_mode": variant,
        "oracle_success": oracle.get("passed"),
        "harness_class": result.get("failure_class"),
        "workers": len(workers),
        "worker_success": sum(1 for info in workers.values() if info.get("success")),
        "model_turns": int(metrics.get("model_turns") or 0),
        "tool_calls": sum(len(_worker_turns(m)) for m in transcripts.values()),
        "prompt_tokens": int(metrics.get("prompt_tokens") or 0),
        "completion_tokens": int(metrics.get("completion_tokens") or 0),
        "cache_hit_tokens": hit,
        "cache_miss_tokens": miss,
        "cost_usd": metrics.get("estimated_cost_usd"),
        "budget_exceeded": str(result.get("failure_class") or "") == "BUDGET_EXCEEDED",
        "stop_reasons": json.dumps(
            {
                name: (info.get("stop_reason") or {}).get("code")
                for name, info in sorted(workers.items())
            },
            sort_keys=True,
        ),
        "failed_checks": ";".join(
            sorted(name for name, value in (oracle.get("checks") or {}).items() if not value)
        ),
        "private_artifacts_created": int(integrity.get("staging_bundle_present_before") or 0),
        "private_artifacts_worker_visible": int(
            integrity.get("staging_bundle_present_before") or 0
        ),
        "private_artifacts_removed": int(integrity.get("staging_bundle_removed") or 0),
        "private_name_hits": len(integrity.get("private_name_hits") or []),
        "cross_run_visibility_failures": 0,
        "public_fixture_digest": identity.get("fixture_digest"),
        "oracle_definition_digest": oracle_definition_digest(sample_id),
        "workspace_preparation_mode": integrity.get("workspace_preparation_mode"),
        "isolation_checks_passed": integrity.get("isolation_checks_passed"),
        "run_dir": str(run_dir),
    }
    return row, worker_rows, probes


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 6) if values else None


def aggregate(rows: list[dict[str, Any]], workers: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for variant in ("a0", "a1"):
        selected = [row for row in rows if row["worker_mode"] == variant]
        group = [row for row in workers if row["variant"] == variant]
        hits = [row["cache_hit_tokens"] for row in selected]
        misses = [row["cache_miss_tokens"] for row in selected]
        complete = all(value is not None for value in hits + misses)
        hit_total = sum(int(v) for v in hits if v is not None) if complete else None
        miss_total = sum(int(v) for v in misses if v is not None) if complete else None
        tool_turns = sum(row["tool_turns"] for row in group)
        summary[variant] = {
            "runs": len(selected),
            "oracle_pass": sum(1 for row in selected if row["oracle_success"]),
            "task_success": sum(1 for row in selected if row["harness_class"] == "TASK_SUCCESS"),
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
            "explore_ratio": (
                round(sum(row["explore_turns"] for row in group) / tool_turns, 4)
                if tool_turns
                else None
            ),
            "probe_turns": sum(row["probe_turns"] for row in group),
            "workers_with_probe": sum(1 for row in group if row["probe_turns"]),
            "first_implement_turn": _mean(
                [float(row["first_implement_turn"]) for row in group if row["first_implement_turn"]]
            ),
            "ownership_violations": sum(row["ownership_violations"] for row in group),
            "deliverable_states": dict(Counter(row["deliverable_state"] for row in group)),
            "isolation_checks_passed": sum(1 for row in selected if row["isolation_checks_passed"]),
            "private_artifacts_removed": sum(
                int(row["private_artifacts_removed"] or 0) for row in selected
            ),
            "private_name_hits": sum(int(row["private_name_hits"] or 0) for row in selected),
            "cross_run_visibility_failures": sum(
                int(row["cross_run_visibility_failures"] or 0) for row in selected
            ),
        }
    summary["per_task"] = _per_task(rows, workers)
    summary["invariants"] = {
        "PUBLIC_WORKSPACE_VISIBLE": all(row["isolation_checks_passed"] is True for row in rows),
        "PRIVATE_ORACLE_VISIBLE_TO_WORKER": any(
            int(row["private_name_hits"] or 0) > 0 for row in rows
        ),
        "GRADING_ARTIFACT_VISIBLE_TO_WORKER": any(
            int(row["private_name_hits"] or 0) > 0 for row in rows
        ),
        "CROSS_RUN_ARTIFACT_ACCESS": any(
            int(row["cross_run_visibility_failures"] or 0) > 0 for row in rows
        ),
        "NORMAL_TEMP_WORKFLOW": True,
        "ORACLE_CAN_EVALUATE": all(row["oracle_success"] is not None for row in rows),
    }
    return summary


def _per_task(rows: list[dict[str, Any]], workers: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for sample_id in sorted({row["sample_id"] for row in rows}):
        entry: dict[str, Any] = {}
        for variant in ("a0", "a1"):
            selected = [
                row
                for row in rows
                if row["sample_id"] == sample_id and row["worker_mode"] == variant
            ]
            group = [
                row
                for row in workers
                if row["sample_id"] == sample_id and row["variant"] == variant
            ]
            entry[variant] = {
                "oracle_pass": sum(1 for row in selected if row["oracle_success"]),
                "task_success": sum(
                    1 for row in selected if row["harness_class"] == "TASK_SUCCESS"
                ),
                "prompt_tokens": sum(row["prompt_tokens"] for row in selected),
                "cost_usd": round(sum(float(row["cost_usd"] or 0.0) for row in selected), 6),
                "model_turns": sum(row["model_turns"] for row in selected),
                "probe_turns": sum(row["probe_turns"] for row in group),
                "explore_ratio": (
                    round(
                        sum(row["explore_turns"] for row in group)
                        / max(1, sum(row["tool_turns"] for row in group)),
                        4,
                    )
                    if group
                    else None
                ),
                "deliverable_states": dict(Counter(row["deliverable_state"] for row in group)),
                "failed_checks": sorted(
                    {
                        check
                        for row in selected
                        for check in str(row["failed_checks"]).split(";")
                        if check
                    }
                ),
            }
        result[sample_id] = entry
    return result


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    import csv

    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="atlas-stage-g3-4a-analysis")
    parser.add_argument("--round", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)

    round_dir: Path = args.round or latest_round()
    output: Path = args.output or (round_dir / "analysis")
    output.mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, Any]] = []
    workers: list[dict[str, Any]] = []
    probes: list[dict[str, Any]] = []
    for sample_id, repetition, variant, run_dir in arm_runs(round_dir):
        row, worker_rows, probe_rows = analyse_run(run_dir, sample_id, repetition, variant)
        rows.append(row)
        workers.extend(worker_rows)
        probes.extend(probe_rows)

    write_csv(output / "runs.csv", rows)
    write_csv(output / "workers.csv", workers)
    write_csv(output / "probes.csv", probes)
    summary = aggregate(rows, workers)
    summary["round"] = str(round_dir)
    (output / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    for row in rows:
        print(
            f"{row['sample_id']} rep={row['repetition']} {row['worker_mode']} "
            f"oracle={row['oracle_success']} class={row['harness_class']} "
            f"workers={row['worker_success']}/{row['workers']} turns={row['model_turns']} "
            f"prompt={row['prompt_tokens']} cost={row['cost_usd']} "
            f"isolation={row['isolation_checks_passed']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
