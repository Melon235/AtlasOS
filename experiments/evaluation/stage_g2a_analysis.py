"""Aggregate Stage G2a budget results (B0 legacy vs B1 guarded).

Thin addendum on top of Stage E1: the sample definitions, fixtures, oracles, per-sample CLI,
metric aggregation (``_metrics`` / ``_run_result``) and failure attribution helpers are reused
unchanged. Only the budget counters and the B0/B1 pairing are new.
"""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path
from typing import Any

from experiments.evaluation.stage_e1_analysis import _metrics, _run_result
from experiments.evaluation.stage_e_evidence import collect_evidence
from experiments.evaluation.stage_g1_analysis import _producer_failures

REPO_ROOT = Path(__file__).resolve().parents[2]
RESULTS_ROOT = REPO_ROOT / "experiments" / "results" / "stage-g2a"


def _latest_manifest() -> Path:
    manifests = sorted(RESULTS_ROOT.glob("*/manifest.json"))
    if not manifests:
        raise SystemExit("no Stage G2a manifest found")
    return manifests[-1]


def _budget(run: dict[str, Any]) -> dict[str, Any]:
    detail = _run_result(run)
    return (detail.get("metrics") or {}).get("budget") or {}


def _limit(run: dict[str, Any]) -> int | None:
    """The configured limit; legacy runs have no budget block, so read the run settings."""

    budget = _budget(run)
    if budget.get("limit_tokens"):
        return int(budget["limit_tokens"])
    environment = _run_result(run).get("environment") or {}
    value = environment.get("token_budget")
    return int(value) if value else None


def _mode(run: dict[str, Any]) -> str:
    return str(
        _budget(run).get("mode") or _run_result(run).get("environment", {}).get("budget_mode")
    )


def _classify(run: dict[str, Any], evidence: dict[str, Any], budget: dict[str, Any]) -> str:
    if run.get("success"):
        return "TASK_SUCCESS"
    failure_class = str(run.get("failure_class") or "")
    if failure_class == "BUDGET_EXCEEDED":
        return "BUDGET_STOP" if int(budget.get("admission_rejections") or 0) else "BUDGET_OVERRUN"
    if failure_class in {"TIMEOUT", "ENVIRONMENT_FAILURE", "SANDBOX_FAILURE", "MODEL_FAILURE"}:
        return failure_class
    if _producer_failures(evidence):
        return "PRODUCER_FAILURE"
    if failure_class == "ORACLE_FAILURE":
        return "ORACLE_FAILURE"
    return failure_class or "UNKNOWN"


def _rollup(runs: list[dict[str, Any]]) -> dict[str, Any]:
    budgets = [_budget(run) for run in runs]
    actual = [
        int(run.get("prompt_tokens") or 0) + int(run.get("completion_tokens") or 0) for run in runs
    ]
    limits = [value for value in (_limit(run) for run in runs) if value]
    return {
        "runs": len(runs),
        "budget_mode": _mode(runs[0]) if runs else "legacy",
        "budget_limit": max(limits) if limits else None,
        "actual_tokens": sum(actual),
        "actual_tokens_max": max(actual) if actual else None,
        "runs_over_budget": sum(1 for value in actual if limits and value > max(limits)),
        "peak_reserved_tokens": max(
            (int(item.get("peak_reserved_tokens") or 0) for item in budgets), default=0
        ),
        "requests_started": sum(int(item.get("requests_started") or 0) for item in budgets),
        "requests_completed": sum(int(item.get("requests_completed") or 0) for item in budgets),
        "requests_written_off": sum(int(item.get("requests_written_off") or 0) for item in budgets),
        "admission_rejections": sum(int(item.get("admission_rejections") or 0) for item in budgets),
        "budget_overrun_tokens": sum(
            int(item.get("budget_overrun_tokens") or 0) for item in budgets
        ),
        "estimate_abs_error_tokens": sum(
            int(item.get("estimate_abs_error_tokens") or 0) for item in budgets
        ),
        "estimate_samples": sum(int(item.get("estimate_samples") or 0) for item in budgets),
        "learned_ratio_max": max(
            (float(item.get("learned_ratio") or 1.0) for item in budgets), default=1.0
        ),
        "stop_reasons": sorted(
            {str(item.get("stop_reason")) for item in budgets if item.get("stop_reason")}
        ),
    }


def _paired(pairs: list[dict[str, Any]], key: str) -> dict[str, Any]:
    diffs = [
        float(pair["runs"]["b1"].get(key) or 0.0) - float(pair["runs"]["b0"].get(key) or 0.0)
        for pair in pairs
    ]
    if not diffs:
        return {"n": 0, "mean_difference": None, "min": None, "max": None}
    return {
        "n": len(diffs),
        "mean_difference": statistics.mean(diffs),
        "min": min(diffs),
        "max": max(diffs),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="atlas-stage-g2a-analysis")
    parser.add_argument("--manifest", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=RESULTS_ROOT)
    args = parser.parse_args(argv)
    manifest_path = args.manifest or _latest_manifest()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    pairs = manifest.get("pairs") or []
    variants = ("b0", "b1")
    grouped = {variant: [pair["runs"][variant] for pair in pairs] for variant in variants}

    per_run: list[dict[str, Any]] = []
    for pair in pairs:
        for variant in variants:
            run = pair["runs"][variant]
            run_dir = run.get("run_dir")
            evidence = collect_evidence(run_dir) if run_dir and Path(run_dir).is_dir() else {}
            detail = _run_result(run)
            budget = _budget(run)
            per_run.append(
                {
                    "run_id": run.get("run_id"),
                    "definition": pair["definition"],
                    "repetition": pair["repetition"],
                    "variant": variant,
                    "budget_mode": _mode(run),
                    "budget_limit": _limit(run),
                    "actual_prompt_tokens": run.get("prompt_tokens"),
                    "actual_completion_tokens": run.get("completion_tokens"),
                    "actual_tokens": int(run.get("prompt_tokens") or 0)
                    + int(run.get("completion_tokens") or 0),
                    "peak_reserved_tokens": budget.get("peak_reserved_tokens"),
                    "admission_rejections": budget.get("admission_rejections"),
                    "budget_overrun_tokens": budget.get("budget_overrun_tokens"),
                    "requests_started": budget.get("requests_started"),
                    "requests_completed": budget.get("requests_completed"),
                    "requests_written_off": budget.get("requests_written_off"),
                    "budget_stop_reason": budget.get("stop_reason"),
                    "final_status": detail.get("success")
                    and "success"
                    or detail.get("failure_class"),
                    "failure_reason": detail.get("failure_reason"),
                    "oracle_success": (detail.get("oracle") or {}).get("passed"),
                    "attribution": _classify(run, evidence, budget),
                    "tool_calls": run.get("tool_calls"),
                    "latency_ms": run.get("latency_ms"),
                    "cost": run.get("cost"),
                }
            )

    summary = {
        "source_manifest": str(manifest_path),
        "round_id": manifest.get("round_id"),
        "git_commit": manifest.get("git_commit"),
        "git_dirty": manifest.get("git_dirty"),
        "handoff_mode": manifest.get("handoff_mode"),
        "metrics": {variant.upper(): _metrics(grouped[variant]) for variant in variants},
        "budget": {variant.upper(): _rollup(grouped[variant]) for variant in variants},
        "paired": {
            key: _paired(pairs, key)
            for key in ("prompt_tokens", "completion_tokens", "tool_calls", "latency_ms", "cost")
        },
        "runs": per_run,
    }
    output = args.output / str(manifest.get("round_id") or "latest")
    output.mkdir(parents=True, exist_ok=True)
    (output / "metrics.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8"
    )

    lines = [
        "# Stage G2a - Run-scoped Token Budget",
        "",
        f"Source manifest: `{manifest_path}`",
        f"Git commit: `{summary['git_commit']}`; dirty: `{summary['git_dirty']}`",
        f"Handoff mode (both arms): `{summary['handoff_mode']}`",
        "",
        "## Execution",
        "",
        "| variant | runs | success | prompt | completion | total | tool calls "
        "| cache ratio | cost USD | p50 latency ms |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for variant in variants:
        item = summary["metrics"][variant.upper()]
        lines.append(
            f"| {variant} | {item['runs']} | {item['success']}/{item['runs']} | "
            f"{item['prompt_tokens']} | {item['completion_tokens']} | "
            f"{item['prompt_tokens'] + item['completion_tokens']} | {item['tool_calls']} | "
            f"{item['cache_hit_ratio'] or 0.0:.4f} | {item['cost']:.6f} | "
            f"{item['latency_p50_ms'] or 0.0:.1f} |"
        )
    lines.extend(
        [
            "",
            "## Budget",
            "",
            "| variant | mode | limit | actual | max run | over budget | peak reserved "
            "| started | completed | written off | rejections | overrun tokens "
            "| estimate error | learned ratio |",
            "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for variant in variants:
        item = summary["budget"][variant.upper()]
        lines.append(
            f"| {variant} | {item['budget_mode']} | {item['budget_limit']} | "
            f"{item['actual_tokens']} | {item['actual_tokens_max']} | "
            f"{item['runs_over_budget']}/{item['runs']} | {item['peak_reserved_tokens']} | "
            f"{item['requests_started']} | {item['requests_completed']} | "
            f"{item['requests_written_off']} | {item['admission_rejections']} | "
            f"{item['budget_overrun_tokens']} | {item['estimate_abs_error_tokens']} | "
            f"{item['learned_ratio_max']:.2f} |"
        )
    lines.extend(["", "## Paired difference (guarded - legacy)", ""])
    for key, value in summary["paired"].items():
        mean = value["mean_difference"]
        lines.append(
            f"- {key}: mean {mean if mean is None else round(mean, 3)} "
            f"(min {value['min']}, max {value['max']}, n={value['n']})"
        )
    lines.extend(
        [
            "",
            "## Runs",
            "",
            "| task | rep | variant | mode | success | total tokens | rejections | attribution |",
            "|---|---:|---|---|---|---:|---:|---|",
        ]
    )
    for row in per_run:
        lines.append(
            f"| {row['definition']} | {row['repetition']} | {row['variant']} | "
            f"{row['budget_mode']} | {'yes' if row['final_status'] == 'success' else 'no'} | "
            f"{row['actual_tokens']} | {row['admission_rejections'] or 0} | {row['attribution']} |"
        )
    (output / "metrics.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({k: summary[k] for k in ("metrics", "budget", "paired")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
