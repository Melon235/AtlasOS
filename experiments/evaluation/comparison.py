"""Build matched AtlasOS-vs-baseline and ablation comparison data from raw results."""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_results(root: Path) -> list[dict[str, Any]]:
    return [
        json.loads(path.read_text(encoding="utf-8")) for path in sorted(root.rglob("result.json"))
    ]


def _summary(rows: list[dict[str, Any]], *, scope: str) -> dict[str, Any]:
    if not rows:
        return {"scope": scope, "runs": 0, "task_success_rate": None}
    success = sum(1 for row in rows if row.get("success"))
    latencies = [
        float(row["metrics"]["latency_ms"])
        for row in rows
        if (row.get("metrics") or {}).get("latency_ms") is not None
    ]
    costs = [
        float(row["metrics"]["estimated_cost_usd"])
        for row in rows
        if (row.get("metrics") or {}).get("estimated_cost_usd") is not None
    ]
    return {
        "scope": scope,
        "runs": len(rows),
        "success": success,
        "task_success_rate": success / len(rows),
        "prompt_tokens": sum(
            int((row.get("metrics") or {}).get("prompt_tokens") or 0) for row in rows
        ),
        "completion_tokens": sum(
            int((row.get("metrics") or {}).get("completion_tokens") or 0) for row in rows
        ),
        "estimated_cost_usd": round(sum(costs), 8) if costs else None,
        "latency_p50_ms": statistics.median(latencies) if latencies else None,
        "latency_p95_ms": (
            sorted(latencies)[max(0, int(len(latencies) * 0.95) - 1)] if latencies else None
        ),
        "by_category": {
            category: {
                "runs": len(category_rows),
                "task_success_rate": (
                    sum(1 for row in category_rows if row.get("success")) / len(category_rows)
                ),
            }
            for category in sorted({str(row.get("category")) for row in rows})
            if (category_rows := [row for row in rows if str(row.get("category")) == category])
        },
    }


def build_comparison(
    *,
    formal_root: Path,
    baseline_root: Path,
    ablation_root: Path | None,
) -> dict[str, Any]:
    formal_rows = _load_results(formal_root / "runs")
    baseline_rows = _load_results(baseline_root / "baseline-runs")
    ablation_rows = _load_results(ablation_root / "runs") if ablation_root else []
    matched_formal = [row for row in formal_rows if row.get("category") != "tool_discovery"]
    matched_rep0 = [
        row for row in matched_formal if (row.get("identity") or {}).get("repetition_id") == 0
    ]
    tool_formal = [row for row in formal_rows if row.get("category") == "tool_discovery"]
    comparison: dict[str, Any] = {
        "AtlasOS full": _summary(formal_rows, scope="30 tasks, 3 repetitions"),
        "AtlasOS matched non-tool": _summary(
            matched_formal, scope="25 non-tool tasks, 3 repetitions"
        ),
        "AtlasOS matched rep0": _summary(matched_rep0, scope="25 non-tool tasks, repetition 0"),
        "AtlasOS tool discovery": _summary(tool_formal, scope="5 tool-discovery tasks"),
        "AnyCode baseline": _summary(baseline_rows, scope="25 non-tool tasks"),
        "tool_discovery_baseline": (
            "NOT_EVALUATED (AnyCode 0.9.0 has no M1 progressive MCP surface)"
        ),
    }
    if ablation_rows:
        ablation_non_tool = [
            row for row in ablation_rows if row.get("category") != "tool_discovery"
        ]
        ablation_ids = {row.get("sample_id") for row in ablation_non_tool}
        atlas_ablation_subset = [row for row in formal_rows if row.get("sample_id") in ablation_ids]
        comparison["No progressive discovery"] = _summary(
            ablation_rows, scope="10-task cross-category subset, all tools native"
        )
        comparison["No progressive discovery non-tool"] = _summary(
            ablation_non_tool, scope="8 non-tool subset tasks, all tools native"
        )
        comparison["AtlasOS ablation subset non-tool"] = _summary(
            atlas_ablation_subset,
            scope="same 8 non-tool tasks, full AtlasOS",
        )
        comparison["No progressive discovery tool discovery"] = (
            "NOT_EVALUATED: the progressive-discovery oracle requires tool.search; direct "
            "native MCP invocation succeeded but is not scored as the same contract"
        )
    return comparison


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="atlas-eval-comparison")
    parser.add_argument("--formal", required=True)
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--ablation", default=None)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    comparison = build_comparison(
        formal_root=Path(args.formal),
        baseline_root=Path(args.baseline),
        ablation_root=Path(args.ablation) if args.ablation else None,
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(comparison, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(comparison, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
