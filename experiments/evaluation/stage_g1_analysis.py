"""Aggregate Stage G1 H0/H1 handoff results.

This is a thin Stage E1 addendum: sample definitions, fixtures, oracles, the per-sample CLI and
the metric aggregation helpers are reused unchanged. Only the pairing dimension differs (handoff
mode instead of worker count) and only handoff-specific counters are added on top.
"""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path
from typing import Any

from experiments.evaluation.stage_e1_analysis import _metrics, _run_result
from experiments.evaluation.stage_e_evidence import collect_evidence

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = REPO_ROOT / "experiments" / "results" / "stage-g1"
INFRA_FAILURES = frozenset(
    {
        "TIMEOUT",
        "ENVIRONMENT_FAILURE",
        "SANDBOX_FAILURE",
        "MODEL_FAILURE",
        "RUNTIME_FAILURE",
        "CLEANUP_FAILURE",
        "HOST_ISOLATION_VIOLATION",
        "FORBIDDEN_WORKSPACE_CHANGE",
    }
)


def _latest_manifest() -> Path:
    manifests = sorted((REPO_ROOT / "experiments" / "results" / "stage-g1").glob("*/manifest.json"))
    if not manifests:
        raise SystemExit("no Stage G1 manifest found")
    return manifests[-1]


def _handoff(run: dict[str, Any]) -> dict[str, Any]:
    detail = _run_result(run)
    return (detail.get("metrics") or {}).get("handoff") or {}


def _producer_failures(evidence: dict[str, Any]) -> list[str]:
    """Downstream consumers of an upstream worker that failed.

    A producer-side failure must never be attributed to handoff truncation.
    """

    failed = {
        str(step["step_task_id"])
        for step in evidence.get("steps") or []
        if step.get("agent_success") is False
    }
    consumers: list[str] = []
    for edge in evidence.get("edges") or []:
        if str(edge.get("from")) in failed:
            consumers.append(str(edge.get("to")))
    return sorted(set(consumers))


def _classify(run: dict[str, Any], evidence: dict[str, Any], handoff: dict[str, Any]) -> str:
    """Attribute one run without blaming truncation for producer-side failures."""

    if run.get("success"):
        return "TASK_SUCCESS"
    failure_class = str(run.get("failure_class") or "")
    if failure_class == "BUDGET_EXCEEDED":
        return "BUDGET_EXCEEDED"
    if failure_class in INFRA_FAILURES:
        return failure_class
    if _producer_failures(evidence):
        return "PRODUCER_FAILURE"
    if int(handoff.get("read_denied") or 0) > 0:
        return "HANDOFF_FAILURE"
    offloaded = int(handoff.get("offloaded_handoffs") or 0)
    if offloaded and int(handoff.get("read_calls") or 0) == 0:
        return "CONSUMER_DID_NOT_READ"
    if int(handoff.get("read_success") or 0) > 0:
        return "CONSUMER_READ_STILL_FAILED"
    if failure_class == "ORACLE_FAILURE":
        return "ORACLE_FAILURE"
    return failure_class or "UNKNOWN"


def _obtained_complete(handoff: dict[str, Any]) -> int:
    """Consumers that obtained every character: prompt preview plus pages returned by the tool.

    Runs recorded before the preview was folded into the runtime counter carry no
    ``preview_chars`` field, so the preview length is joined back from the projection blocks.
    """

    preview: dict[tuple[str, str], int] = {}
    for block in handoff.get("blocks") or []:
        if block.get("ref_id"):
            preview[(str(block.get("consumer")), str(block["ref_id"]))] = int(
                block.get("projected_chars") or 0
            )
    complete = 0
    for entry in handoff.get("consumers") or []:
        total = int(entry.get("total_chars") or 0)
        covered = int(entry.get("covered_chars") or 0)
        if "preview_chars" not in entry:
            key = (str(entry.get("consumer")), str(entry.get("ref_id")))
            covered += min(preview.get(key, 0), total)
        if total > 0 and covered >= total:
            complete += 1
    return complete


def _handoff_rollup(runs: list[dict[str, Any]]) -> dict[str, Any]:
    rows = [_handoff(run) for run in runs]
    return {
        "runs": len(rows),
        "result_chars": sum(int(row.get("result_chars") or 0) for row in rows),
        "inline_chars": sum(int(row.get("inline_chars") or 0) for row in rows),
        "offloaded_chars": sum(int(row.get("offloaded_chars") or 0) for row in rows),
        "inline_handoffs": sum(int(row.get("inline_handoffs") or 0) for row in rows),
        "offloaded_handoffs": sum(int(row.get("offloaded_handoffs") or 0) for row in rows),
        "refs_issued": sum(int(row.get("refs_issued") or 0) for row in rows),
        "read_calls": sum(int(row.get("read_calls") or 0) for row in rows),
        "read_success": sum(int(row.get("read_success") or 0) for row in rows),
        "read_failures": sum(int(row.get("read_failures") or 0) for row in rows),
        "read_denied": sum(int(row.get("read_denied") or 0) for row in rows),
        "consumers_read_complete": sum(_obtained_complete(row) for row in rows),
        "unmapped_consumers": sorted(
            {name for row in rows for name in (row.get("unmapped_consumers") or [])}
        ),
    }


def _paired_difference(pairs: list[dict[str, Any]], key: str) -> dict[str, Any]:
    diffs: list[float] = []
    for pair in pairs:
        h0 = float(pair["runs"]["h0"].get(key) or 0.0)
        h1 = float(pair["runs"]["h1"].get(key) or 0.0)
        diffs.append(h1 - h0)
    if not diffs:
        return {"n": 0, "mean_difference": None, "min": None, "max": None}
    return {
        "n": len(diffs),
        "mean_difference": statistics.mean(diffs),
        "min": min(diffs),
        "max": max(diffs),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="atlas-stage-g1-analysis")
    parser.add_argument("--manifest", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    manifest_path = args.manifest or _latest_manifest()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    pairs = manifest.get("pairs") or []
    h0 = [pair["runs"]["h0"] for pair in pairs]
    h1 = [pair["runs"]["h1"] for pair in pairs]
    per_run: list[dict[str, Any]] = []
    for pair in pairs:
        for suffix in ("h0", "h1"):
            run = pair["runs"][suffix]
            run_dir = run.get("run_dir")
            evidence = collect_evidence(run_dir) if run_dir and Path(run_dir).is_dir() else {}
            handoff = _handoff(run)
            per_run.append(
                {
                    "definition": pair["definition"],
                    "repetition": pair["repetition"],
                    "variant": suffix,
                    "handoff_mode": run.get("handoff_mode"),
                    "success": bool(run.get("success")),
                    "failure_class": run.get("failure_class"),
                    "failure_reason": _run_result(run).get("failure_reason"),
                    "attribution": _classify(run, evidence, handoff),
                    "prompt_tokens": run.get("prompt_tokens"),
                    "completion_tokens": run.get("completion_tokens"),
                    "tool_calls": run.get("tool_calls"),
                    "latency_ms": run.get("latency_ms"),
                    "cost": run.get("cost"),
                    "handoff": handoff,
                    "edges": evidence.get("edges") or [],
                }
            )
    summary = {
        "source_manifest": str(manifest_path),
        "round_id": manifest.get("round_id"),
        "git_commit": manifest.get("git_commit"),
        "git_dirty": manifest.get("git_dirty"),
        "metrics": {"H0": _metrics(h0), "H1": _metrics(h1)},
        "handoff": {"H0": _handoff_rollup(h0), "H1": _handoff_rollup(h1)},
        "paired": {
            key: _paired_difference(pairs, key)
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
        "# Stage G1 - Dual-Channel Handoff",
        "",
        f"Source manifest: `{manifest_path}`",
        f"Git commit: `{summary['git_commit']}`; dirty: `{summary['git_dirty']}`",
        "",
        "## Overall",
        "",
        "| variant | runs | success | prompt tokens | completion tokens | tool calls "
        "| cache ratio | cost USD | p50 latency ms |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for variant, item in summary["metrics"].items():
        lines.append(
            f"| {variant} | {item['runs']} | {item['success']}/{item['runs']} | "
            f"{item['prompt_tokens']} | {item['completion_tokens']} | {item['tool_calls']} | "
            f"{item['cache_hit_ratio'] or 0.0:.4f} | {item['cost']:.6f} | "
            f"{item['latency_p50_ms'] or 0.0:.1f} |"
        )
    lines.extend(
        [
            "",
            "## Handoff channels",
            "",
            "| variant | result chars | inline chars | offloaded chars | refs | read calls "
            "| read ok | read failed | consumers read complete |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for variant, item in summary["handoff"].items():
        lines.append(
            f"| {variant} | {item['result_chars']} | {item['inline_chars']} | "
            f"{item['offloaded_chars']} | {item['refs_issued']} | {item['read_calls']} | "
            f"{item['read_success']} | {item['read_failures']} | "
            f"{item['consumers_read_complete']} |"
        )
    lines.extend(["", "## Paired difference (H1 - H0)", ""])
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
            "| task | rep | variant | success | attribution |",
            "|---|---:|---|---|---|",
        ]
    )
    for row in per_run:
        lines.append(
            f"| {row['definition']} | {row['repetition']} | {row['variant']} | "
            f"{'yes' if row['success'] else 'no'} | {row['attribution']} |"
        )
    (output / "metrics.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({k: summary[k] for k in ("metrics", "handoff", "paired")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
