"""Aggregate Stage G2b-1 compaction results (C0 off vs C1 rolling_2).

Reuses the Stage E1 metric aggregation, the G1 failure attribution and the G2b-0 per-turn
reconstruction: this stage only adds compaction counters and the C0/C1 pairing.
"""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path
from typing import Any

from anycode.context.tokenizer import select_tokenizer
from experiments.evaluation.stage_e1_analysis import _metrics, _run_result
from experiments.evaluation.stage_e_evidence import collect_evidence
from experiments.evaluation.stage_g1_analysis import _producer_failures
from experiments.evaluation.stage_g2b_diagnosis import PromptCounter, per_turn

REPO_ROOT = Path(__file__).resolve().parents[2]
RESULTS_ROOT = REPO_ROOT / "experiments" / "results" / "stage-g2b1"
INFRA_FAILURES = frozenset(
    {"TIMEOUT", "ENVIRONMENT_FAILURE", "SANDBOX_FAILURE", "MODEL_FAILURE", "RUNTIME_FAILURE"}
)


def _latest_manifest() -> Path:
    manifests = sorted(RESULTS_ROOT.glob("*/manifest.json"))
    if not manifests:
        raise SystemExit("no Stage G2b-1 manifest found")
    return manifests[-1]


def _detail(run: dict[str, Any]) -> dict[str, Any]:
    return _run_result(run)


def _compaction(run: dict[str, Any]) -> dict[str, Any]:
    return (_detail(run).get("metrics") or {}).get("compaction") or {}


def _budget(run: dict[str, Any]) -> dict[str, Any]:
    return (_detail(run).get("metrics") or {}).get("budget") or {}


def _classify(run: dict[str, Any], evidence: dict[str, Any], budget: dict[str, Any]) -> str:
    if run.get("success"):
        return "TASK_SUCCESS"
    failure_class = str(run.get("failure_class") or "")
    if failure_class == "BUDGET_EXCEEDED":
        return "BUDGET_STOP" if int(budget.get("admission_rejections") or 0) else "BUDGET_OVERRUN"
    if failure_class in INFRA_FAILURES:
        return failure_class
    if _producer_failures(evidence):
        return "PRODUCER_FAILURE"
    if failure_class == "ORACLE_FAILURE":
        return "ORACLE_FAILURE"
    return failure_class or "UNKNOWN"


def _rollup(runs: list[dict[str, Any]]) -> dict[str, Any]:
    compactions = [_compaction(run) for run in runs]
    budgets = [_budget(run) for run in runs]
    return {
        "runs": len(runs),
        "compaction_mode": (compactions[0].get("mode") if compactions else None),
        "compaction_events": sum(int(item.get("compaction_events") or 0) for item in compactions),
        "results_archived": sum(int(item.get("results_archived") or 0) for item in compactions),
        "original_chars_archived": sum(
            int(item.get("original_chars_archived") or 0) for item in compactions
        ),
        "placeholder_chars": sum(int(item.get("placeholder_chars") or 0) for item in compactions),
        "archive_failures": sum(int(item.get("archive_failures") or 0) for item in compactions),
        "history_read_calls": sum(int(item.get("read_calls") or 0) for item in compactions),
        "history_read_success": sum(int(item.get("read_success") or 0) for item in compactions),
        "history_read_denied": sum(int(item.get("read_denied") or 0) for item in compactions),
        "history_bytes_stored": sum(
            int(item.get("history_bytes_stored") or 0) for item in compactions
        ),
        "admission_rejections": sum(int(item.get("admission_rejections") or 0) for item in budgets),
        "requests_started": sum(int(item.get("requests_started") or 0) for item in budgets),
        "requests_completed": sum(int(item.get("requests_completed") or 0) for item in budgets),
    }


def _paired(pairs: list[dict[str, Any]], key: str) -> dict[str, Any]:
    diffs = [
        float(pair["runs"]["c1"].get(key) or 0.0) - float(pair["runs"]["c0"].get(key) or 0.0)
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
    parser = argparse.ArgumentParser(prog="atlas-stage-g2b1-analysis")
    parser.add_argument("--manifest", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=RESULTS_ROOT)
    args = parser.parse_args(argv)
    manifest_path = args.manifest or _latest_manifest()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    pairs = manifest.get("pairs") or []
    variants = ("c0", "c1")
    grouped = {variant: [pair["runs"][variant] for pair in pairs] for variant in variants}
    counter = PromptCounter(select_tokenizer("tiktoken", model="deepseek-chat"))

    per_run: list[dict[str, Any]] = []
    turn_rows: list[dict[str, Any]] = []
    for pair in pairs:
        for variant in variants:
            run = pair["runs"][variant]
            run_dir = Path(str(run.get("run_dir")))
            detail = _detail(run)
            budget = _budget(run)
            compaction = _compaction(run)
            evidence = collect_evidence(str(run_dir)) if run_dir.is_dir() else {}
            task_turns: list[dict[str, Any]] = []
            if run_dir.is_dir():
                try:
                    task_turns, _summaries, _diag = per_turn(run_dir, detail, counter)
                except (KeyError, ValueError, FileNotFoundError):
                    task_turns = []
            turn_rows.extend(task_turns)
            prompt_per_turn = [
                float(row["anchored_prompt_tokens"])
                for row in task_turns
                if float(row.get("anchored_prompt_tokens") or 0) > 0
            ]
            per_run.append(
                {
                    "definition": pair["definition"],
                    "repetition": pair["repetition"],
                    "variant": variant,
                    "compaction_mode": compaction.get("mode"),
                    "success": bool(run.get("success")),
                    "failure_class": run.get("failure_class"),
                    "failure_reason": detail.get("failure_reason"),
                    "attribution": _classify(run, evidence, budget),
                    "prompt_tokens": run.get("prompt_tokens"),
                    "completion_tokens": run.get("completion_tokens"),
                    "cache_hit_tokens": run.get("cache_hit_tokens") or 0,
                    "cache_miss_tokens": run.get("cache_miss_tokens") or 0,
                    "model_turns": detail.get("metrics", {}).get("model_turns"),
                    "tool_calls": run.get("tool_calls"),
                    "cost_usd": run.get("cost"),
                    "latency_ms": run.get("latency_ms"),
                    "tokens_per_turn": (
                        round(
                            int(run.get("prompt_tokens") or 0)
                            / int(detail["metrics"]["model_turns"]),
                            1,
                        )
                        if detail.get("metrics", {}).get("model_turns")
                        else None
                    ),
                    "results_archived": compaction.get("results_archived"),
                    "history_read_calls": compaction.get("read_calls"),
                    "history_read_success": compaction.get("read_success"),
                    "admission_rejections": budget.get("admission_rejections"),
                    "mean_turn_prompt": round(statistics.mean(prompt_per_turn), 1)
                    if prompt_per_turn
                    else None,
                    "max_turn_prompt": round(max(prompt_per_turn), 1) if prompt_per_turn else None,
                    "last_turn_prompt": round(prompt_per_turn[-1], 1) if prompt_per_turn else None,
                    "stop_reasons": sorted(
                        {
                            str((worker.get("stop_reason") or {}).get("code"))
                            for worker in (detail.get("worker_results") or {}).values()
                        }
                    ),
                }
            )

    summary = {
        "source_manifest": str(manifest_path),
        "round_id": manifest.get("round_id"),
        "git_commit": manifest.get("git_commit"),
        "git_dirty": manifest.get("git_dirty"),
        "metrics": {variant.upper(): _metrics(grouped[variant]) for variant in variants},
        "compaction": {variant.upper(): _rollup(grouped[variant]) for variant in variants},
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
        "# Stage G2b-1 - Rolling Tool Result Compaction",
        "",
        f"Source manifest: `{manifest_path}`",
        f"Git commit: `{summary['git_commit']}`; dirty: `{summary['git_dirty']}`",
        "",
        "## Execution",
        "",
        "| arm | mode | runs | success | prompt | completion | turns | tools | cache ratio "
        "| cost USD | p50 latency ms |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for variant in variants:
        item = summary["metrics"][variant.upper()]
        mode = summary["compaction"][variant.upper()]["compaction_mode"]
        lines.append(
            f"| {variant} | {mode} | {item['runs']} | {item['success']}/{item['runs']} | "
            f"{item['prompt_tokens']} | {item['completion_tokens']} | {item.get('model_turns')} | "
            f"{item['tool_calls']} | {item['cache_hit_ratio'] or 0.0:.4f} | {item['cost']:.6f} | "
            f"{item['latency_p50_ms'] or 0.0:.1f} |"
        )
    lines.extend(
        [
            "",
            "## Compaction",
            "",
            "| arm | mode | events | archived | archived chars | placeholder chars "
            "| bytes stored | archive failures | read calls | read ok | read denied "
            "| budget rejections |",
            "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for variant in variants:
        item = summary["compaction"][variant.upper()]
        lines.append(
            f"| {variant} | {item['compaction_mode']} | {item['compaction_events']} | "
            f"{item['results_archived']} | {item['original_chars_archived']} | "
            f"{item['placeholder_chars']} | {item['history_bytes_stored']} | "
            f"{item['archive_failures']} | {item['history_read_calls']} | "
            f"{item['history_read_success']} | {item['history_read_denied']} | "
            f"{item['admission_rejections']} |"
        )
    lines.extend(["", "## Paired difference (rolling_2 - off)", ""])
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
            "| task | rep | arm | success | prompt | turns | tok/turn | mean turn prompt "
            "| max turn prompt | archived | reads | rejections | attribution |",
            "|---|---:|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
        ]
    )
    for row in per_run:
        lines.append(
            f"| {row['definition']} | {row['repetition']} | {row['variant']} | "
            f"{'yes' if row['success'] else 'no'} | {row['prompt_tokens']} | "
            f"{row['model_turns']} | {row['tokens_per_turn']} | {row['mean_turn_prompt']} | "
            f"{row['max_turn_prompt']} | {row['results_archived'] or 0} | "
            f"{row['history_read_calls'] or 0} | {row['admission_rejections'] or 0} | "
            f"{row['attribution']} |"
        )
    (output / "metrics.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({k: summary[k] for k in ("metrics", "compaction", "paired")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
