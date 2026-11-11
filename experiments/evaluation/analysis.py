"""Analyze real Inspect AI round results into machine-readable metrics and charts."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
CATEGORY_ORDER = (
    "bugfix",
    "feature",
    "tool_discovery",
    "multi_worker",
    "long_context",
    "recovery",
    "safety",
)
FAILURE_ORDER = (
    "TASK_SUCCESS",
    "ORACLE_FAILURE",
    "MODEL_FAILURE",
    "RUNTIME_FAILURE",
    "ENVIRONMENT_FAILURE",
    "SANDBOX_FAILURE",
    "TIMEOUT",
    "BUDGET_EXCEEDED",
    "HOST_ISOLATION_VIOLATION",
    "FORBIDDEN_WORKSPACE_CHANGE",
    "CLEANUP_FAILURE",
    "NOT_EVALUATED",
)
_COLORS = {
    "pass": "#2a9d8f",
    "fail": "#e76f51",
    "neutral": "#457b9d",
    "muted": "#8d99ae",
    "warn": "#e9c46a",
}


def _ratio(numerator: int, denominator: int) -> dict[str, Any]:
    return {
        "numerator": numerator,
        "denominator": denominator,
        "value": (numerator / denominator) if denominator else None,
    }


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    return float(np.percentile(np.asarray(values, dtype=float), percentile))


def _load_round(round_dir: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    manifest_path = round_dir / "manifest.json"
    manifest = (
        json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest_path.is_file()
        else {"round_id": round_dir.name}
    )
    results = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((round_dir / "runs").rglob("result.json"))
    ]
    return manifest, results


def _dispatch_ids(result: dict[str, Any]) -> list[str]:
    return [
        str((item.get("input") or {}).get("tool_id", ""))
        for item in result.get("tool_calls", [])
        if item.get("tool_name") == "dispatch_action"
    ]


def _task_run_view(result: dict[str, Any]) -> dict[str, Any]:
    sample_id = result.get("sample_id")
    task = result.get("task") or {}
    if not task and sample_id:
        from experiments.evaluation.tasks import task_by_id

        try:
            definition = task_by_id(str(sample_id))
            task = {
                **definition.public_metadata(),
                "tool_expectations": definition.tool_expectations,
            }
        except KeyError:
            task = {}
    oracle = result.get("oracle") or {}
    checks = oracle.get("checks") or {}
    metrics = result.get("metrics") or {}
    return {
        "sample_id": sample_id,
        "category": result.get("category"),
        "mode": result.get("mode"),
        "success": bool(result.get("success")),
        "failure_class": result.get("failure_class"),
        "failure_reason": result.get("failure_reason"),
        "agent_success": bool(result.get("agent_success")),
        "oracle_passed": bool(oracle.get("passed")),
        "oracle_checks": checks,
        "oracle_failure_reasons": oracle.get("failure_reasons") or [],
        "metrics": metrics,
        "safety": result.get("safety") or {},
        "tool_calls": result.get("tool_calls") or [],
        "dispatch_ids": _dispatch_ids(result),
        "worker_selection": metrics.get("worker_selection") or [],
        "worker_results": result.get("worker_results") or {},
        "identity": result.get("identity") or {},
        "environment": result.get("environment") or {},
        "final_output": result.get("final_output") or "",
        "task": task,
        "run_dir": result.get("run_dir"),
    }


def compute_metrics(results: list[dict[str, Any]]) -> dict[str, Any]:
    runs = [_task_run_view(result) for result in results]
    total = len(runs)
    success = sum(1 for run in runs if run["success"])
    evaluable = [
        run for run in runs if run["failure_class"] not in ("ENVIRONMENT_FAILURE", "NOT_EVALUATED")
    ]
    by_category: dict[str, dict[str, Any]] = {}
    for category in CATEGORY_ORDER:
        category_runs = [run for run in runs if run["category"] == category]
        if not category_runs:
            continue
        by_category[category] = {
            "runs": len(category_runs),
            "task_success": _ratio(
                sum(1 for run in category_runs if run["success"]), len(category_runs)
            ),
            "agent_success": _ratio(
                sum(1 for run in category_runs if run["agent_success"]), len(category_runs)
            ),
        }
    failure_counts = Counter(run["failure_class"] for run in runs)
    per_sample: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for run in runs:
        per_sample[str(run["sample_id"])].append(run)
    pass_at_1_by_sample = {
        sample_id: _ratio(sum(1 for run in sample_runs if run["success"]), len(sample_runs))
        for sample_id, sample_runs in sorted(per_sample.items())
    }
    tool_runs = [
        run
        for run in runs
        if run["category"] == "tool_discovery"
        and (run["task"].get("tool_expectations") or {}).get("required_dispatch_tool_ids")
    ]
    tool_recall_1 = 0
    tool_recall_3 = 0
    tool_selection = 0
    for run in tool_runs:
        required = list(run["task"]["tool_expectations"]["required_dispatch_tool_ids"])
        dispatched = run["dispatch_ids"]
        if dispatched and dispatched[0] in required:
            tool_recall_1 += 1
        if any(item in required for item in dispatched[:3]):
            tool_recall_3 += 1
        if all(item in dispatched for item in required):
            tool_selection += 1
    worker_runs = [run for run in runs if run["category"] == "multi_worker"]
    worker_complete = 0
    for run in worker_runs:
        workers = run["worker_results"]
        if workers and all(bool(value.get("success")) for value in workers.values()):
            worker_complete += 1
    safety_side_effects = sum(
        1
        for run in runs
        if not run["oracle_checks"].get("forbidden_paths_unchanged", True)
        or not run["oracle_checks"].get("host_fixture_unchanged", True)
        or run["metrics"].get("host_isolation_violations", 0)
    )
    latencies = [
        float(run["metrics"]["latency_ms"])
        for run in runs
        if run["metrics"].get("latency_ms") is not None
    ]
    provision_latencies = [
        float(run["metrics"]["provision_latency_ms"])
        for run in runs
        if run["metrics"].get("provision_latency_ms") is not None
    ]
    inference_latencies = [
        float(run["metrics"]["inference_latency_ms"])
        for run in runs
        if run["metrics"].get("inference_latency_ms") is not None
    ]
    prompt_tokens = sum(int(run["metrics"].get("prompt_tokens") or 0) for run in runs)
    completion_tokens = sum(int(run["metrics"].get("completion_tokens") or 0) for run in runs)
    cache_hit = sum(int(run["metrics"].get("cache_hit_tokens") or 0) for run in runs)
    cache_miss = sum(int(run["metrics"].get("cache_miss_tokens") or 0) for run in runs)
    cache_complete = sum(1 for run in runs if run["metrics"].get("cache_metrics_complete") is True)
    costs = [
        float(run["metrics"]["estimated_cost_usd"])
        for run in runs
        if run["metrics"].get("estimated_cost_usd") is not None
    ]
    model_turns = [int(run["metrics"].get("model_turns") or 0) for run in runs]
    tool_calls = [int(run["metrics"].get("tool_calls") or 0) for run in runs]
    resolved_models = sorted(
        {model for run in runs for model in (run["environment"].get("resolved_models") or [])}
    )
    return {
        "runs": total,
        "samples": len(per_sample),
        "task_success_rate": _ratio(success, total),
        "evaluable_task_success_rate": _ratio(
            sum(1 for run in evaluable if run["success"]), len(evaluable)
        ),
        "pass_at_1": _ratio(success, total),
        "pass_at_1_by_sample": pass_at_1_by_sample,
        "agent_success_rate": _ratio(sum(1 for run in runs if run["agent_success"]), total),
        "oracle_pass_rate": _ratio(sum(1 for run in runs if run["oracle_passed"]), total),
        "categories": by_category,
        "failure_classes": {
            name: {"count": failure_counts[name], "fraction": _ratio(failure_counts[name], total)}
            for name in FAILURE_ORDER
            if failure_counts[name]
        },
        "tool_behavior": {
            "required_tool_runs": len(tool_runs),
            "tool_recall_at_1": _ratio(tool_recall_1, len(tool_runs)),
            "tool_recall_at_3": _ratio(tool_recall_3, len(tool_runs)),
            "tool_selection_accuracy": _ratio(tool_selection, len(tool_runs)),
            "undisclosed_tool_rejections": sum(
                int(run["metrics"].get("undisclosed_tool_rejections") or 0) for run in runs
            ),
        },
        "worker_behavior": {
            "multi_worker_runs": len(worker_runs),
            "all_workers_successful": _ratio(worker_complete, len(worker_runs)),
        },
        "safety": {
            "unauthorized_side_effect_runs": _ratio(safety_side_effects, total),
            "host_isolation_violations": sum(
                int(run["metrics"].get("host_isolation_violations") or 0) for run in runs
            ),
            "approval_bypass_count": None,
            "policy_bypass_count": None,
        },
        "performance": {
            "e2e_latency_p50_ms": _percentile(latencies, 50),
            "e2e_latency_p95_ms": _percentile(latencies, 95),
            "e2e_latency_mean_ms": round(statistics.mean(latencies), 3) if latencies else None,
            "provision_latency_p50_ms": _percentile(provision_latencies, 50),
            "provision_latency_p95_ms": _percentile(provision_latencies, 95),
            "inference_latency_p50_ms": _percentile(inference_latencies, 50),
            "inference_latency_p95_ms": _percentile(inference_latencies, 95),
            "prompt_tokens_total": prompt_tokens,
            "completion_tokens_total": completion_tokens,
            "cache_hit_tokens_total": cache_hit,
            "cache_miss_tokens_total": cache_miss,
            "cache_hit_ratio": _ratio(cache_hit, cache_hit + cache_miss),
            "cache_metric_runs_complete": _ratio(cache_complete, total),
            "estimated_cost_usd_total": round(sum(costs), 8) if costs else None,
            "estimated_cost_runs": len(costs),
            "model_turns_mean": round(statistics.mean(model_turns), 3) if model_turns else None,
            "tool_calls_mean": round(statistics.mean(tool_calls), 3) if tool_calls else None,
            "context_transform_overhead": "NOT_EVALUATED",
            "sandbox_reattach_overhead": "NOT_EVALUATED (no coding-run reattachment)",
        },
        "resolved_models": resolved_models,
        "requested_model": (runs[0]["identity"].get("model") if runs else None),
        "raw_runs": runs,
    }


def _save(fig: plt.Figure, output: Path, name: str) -> list[str]:
    output.mkdir(parents=True, exist_ok=True)
    svg = output / f"{name}.svg"
    png = output / f"{name}.png"
    fig.savefig(svg, bbox_inches="tight")
    fig.savefig(png, dpi=160, bbox_inches="tight")
    plt.close(fig)
    return [str(svg), str(png)]


def _bar(value: float | None, title: str, ylabel: str, output: Path, name: str) -> list[str]:
    fig, ax = plt.subplots(figsize=(6.4, 4.0))
    value = float(value or 0.0)
    ax.bar([title], [value], color=_COLORS["pass"] if value >= 0.5 else _COLORS["fail"])
    ax.set_ylim(0, 1.05)
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    for index, item in enumerate([value]):
        ax.text(index, item + 0.02, f"{item:.3f}", ha="center")
    ax.grid(axis="y", alpha=0.25)
    return _save(fig, output, name)


def _category_chart(metrics: dict[str, Any], output: Path) -> list[str]:
    categories = list(metrics["categories"])
    values = [metrics["categories"][item]["task_success"]["value"] or 0.0 for item in categories]
    fig, ax = plt.subplots(figsize=(8.4, 4.2))
    bars = ax.bar(categories, values, color=_COLORS["neutral"])
    ax.set_ylim(0, 1.05)
    ax.set_ylabel("task success rate")
    ax.set_title("Task success by category")
    ax.tick_params(axis="x", rotation=20)
    for bar, value in zip(bars, values, strict=True):
        ax.text(bar.get_x() + bar.get_width() / 2, value + 0.02, f"{value:.2f}", ha="center")
    ax.grid(axis="y", alpha=0.25)
    return _save(fig, output, "category_success")


def _failure_chart(metrics: dict[str, Any], output: Path) -> list[str]:
    labels = list(metrics["failure_classes"])
    counts = [metrics["failure_classes"][item]["count"] for item in labels]
    fig, ax = plt.subplots(figsize=(9.0, 4.4))
    colors = [_COLORS["pass"] if label == "TASK_SUCCESS" else _COLORS["fail"] for label in labels]
    bars = ax.bar(labels, counts, color=colors)
    ax.set_ylabel("runs")
    ax.set_title("Failure classification")
    ax.tick_params(axis="x", rotation=25)
    for bar, count in zip(bars, counts, strict=True):
        ax.text(bar.get_x() + bar.get_width() / 2, count + 0.05, str(count), ha="center")
    ax.grid(axis="y", alpha=0.25)
    return _save(fig, output, "failure_classification")


def _token_cost_chart(
    metrics: dict[str, Any], runs: list[dict[str, Any]], output: Path
) -> list[str]:
    prompt = [int(run["metrics"].get("prompt_tokens") or 0) for run in runs]
    completion = [int(run["metrics"].get("completion_tokens") or 0) for run in runs]
    costs = [float(run["metrics"].get("estimated_cost_usd") or 0.0) for run in runs]
    fig, axes = plt.subplots(1, 3, figsize=(12.0, 3.8))
    axes[0].hist(prompt, bins=12, color=_COLORS["neutral"])
    axes[0].set_title("Prompt tokens")
    axes[1].hist(completion, bins=12, color=_COLORS["warn"])
    axes[1].set_title("Completion tokens")
    axes[2].hist(costs, bins=12, color=_COLORS["pass"])
    axes[2].set_title("Estimated cost (USD, peak upper bound)")
    for axis in axes:
        axis.grid(axis="y", alpha=0.25)
    return _save(fig, output, "token_cost_distribution")


def _latency_chart(metrics: dict[str, Any], output: Path) -> list[str]:
    labels = [
        "E2E p50",
        "E2E p95",
        "Provision p50",
        "Provision p95",
        "Inference p50",
        "Inference p95",
    ]
    values = [
        metrics["performance"]["e2e_latency_p50_ms"] or 0.0,
        metrics["performance"]["e2e_latency_p95_ms"] or 0.0,
        metrics["performance"]["provision_latency_p50_ms"] or 0.0,
        metrics["performance"]["provision_latency_p95_ms"] or 0.0,
        metrics["performance"]["inference_latency_p50_ms"] or 0.0,
        metrics["performance"]["inference_latency_p95_ms"] or 0.0,
    ]
    fig, ax = plt.subplots(figsize=(9.2, 4.2))
    bars = ax.bar(labels, values, color=_COLORS["neutral"])
    ax.set_ylabel("milliseconds")
    ax.set_title("Latency percentiles")
    ax.tick_params(axis="x", rotation=20)
    for bar, value in zip(bars, values, strict=True):
        ax.text(
            bar.get_x() + bar.get_width() / 2,
            value + max(values) * 0.02,
            f"{value:.0f}",
            ha="center",
        )
    ax.grid(axis="y", alpha=0.25)
    return _save(fig, output, "latency_p50_p95")


def _turns_tools_chart(runs: list[dict[str, Any]], output: Path) -> list[str]:
    turns = [int(run["metrics"].get("model_turns") or 0) for run in runs]
    calls = [int(run["metrics"].get("tool_calls") or 0) for run in runs]
    fig, axes = plt.subplots(1, 2, figsize=(9.6, 3.8))
    axes[0].hist(turns, bins=12, color=_COLORS["neutral"])
    axes[0].set_title("Model turns per run")
    axes[1].hist(calls, bins=12, color=_COLORS["warn"])
    axes[1].set_title("Tool calls per run")
    for axis in axes:
        axis.grid(axis="y", alpha=0.25)
    return _save(fig, output, "turns_toolcalls")


def _reliability_chart(reliability: dict[str, Any], output: Path) -> list[str]:
    cases = reliability.get("cases") or []
    if not cases:
        return []
    labels = [item["id"] for item in cases]
    values = [1.0 if item["passed"] else 0.0 for item in cases]
    fig, ax = plt.subplots(figsize=(max(8.0, len(labels) * 0.5), 4.6))
    colors = [_COLORS["pass"] if item else _COLORS["fail"] for item in values]
    ax.barh(labels, values, color=colors)
    ax.set_xlim(0, 1.1)
    ax.set_xlabel("1 = expected safe outcome observed")
    ax.set_title("Safety / recovery fault-injection matrix")
    ax.grid(axis="x", alpha=0.25)
    return _save(fig, output, "safety_recovery_matrix")


def _comparison_chart(
    metrics: dict[str, Any],
    comparison: dict[str, Any] | None,
    output: Path,
) -> list[str]:
    if not comparison:
        return []
    comparable = {
        label: item
        for label, item in comparison.items()
        if isinstance(item, dict) and "task_success_rate" in item
    }
    labels = [
        f"AtlasOS full (n={metrics['task_success_rate']['denominator']})",
        *(f"{label} (n={item.get('runs', '?')})" for label, item in comparable.items()),
    ]
    values = [metrics["task_success_rate"]["value"] or 0.0]
    values.extend(float(item.get("task_success_rate") or 0.0) for item in comparable.values())
    fig, ax = plt.subplots(figsize=(11.0, 4.4))
    bars = ax.bar(
        labels, values, color=[_COLORS["pass"], *([_COLORS["neutral"]] * (len(labels) - 1))]
    )
    ax.set_ylim(0, 1.05)
    ax.set_ylabel("task success rate")
    ax.set_title("AtlasOS vs baseline / ablations (scope in labels)")
    ax.tick_params(axis="x", rotation=15)
    for bar, value in zip(bars, values, strict=True):
        ax.text(bar.get_x() + bar.get_width() / 2, value + 0.02, f"{value:.2f}", ha="center")
    ax.grid(axis="y", alpha=0.25)
    return _save(fig, output, "atlas_vs_baseline")


def _write_metrics_markdown(metrics: dict[str, Any], path: Path) -> None:
    def row(label: str, item: dict[str, Any] | None) -> str:
        if not item or item.get("value") is None:
            return f"| {label} | NOT_EVALUATED | - | - |"
        return f"| {label} | {item['value']:.4f} | {item['numerator']} | {item['denominator']} |"

    lines = [
        "# AtlasOS Agent Benchmark Metrics",
        "",
        "All values are computed from the machine-readable `result.json` records in the round.",
        "",
        "## Task capability",
        "",
        "| Metric | Value | Numerator | Denominator |",
        "|---|---:|---:|---:|",
        row("Task success rate", metrics["task_success_rate"]),
        row("Evaluable task success rate", metrics["evaluable_task_success_rate"]),
        row("Pass@1 (marginal)", metrics["pass_at_1"]),
        row("Agent self-reported success", metrics["agent_success_rate"]),
        row("Independent oracle pass", metrics["oracle_pass_rate"]),
        "",
        "## Category success",
        "",
        "| Category | Runs | Success rate | Numerator | Denominator |",
        "|---|---:|---:|---:|---:|",
    ]
    for category, value in metrics["categories"].items():
        item = value["task_success"]
        lines.append(
            f"| {category} | {value['runs']} | {item['value']:.4f} | "
            f"{item['numerator']} | {item['denominator']} |"
        )
    lines.extend(
        [
            "",
            "## Tool / worker behavior",
            "",
            "| Metric | Value | Numerator | Denominator |",
            "|---|---:|---:|---:|",
            row("Tool Recall@1", metrics["tool_behavior"]["tool_recall_at_1"]),
            row("Tool Recall@3", metrics["tool_behavior"]["tool_recall_at_3"]),
            row("Tool selection accuracy", metrics["tool_behavior"]["tool_selection_accuracy"]),
            row("All workers successful", metrics["worker_behavior"]["all_workers_successful"]),
            "",
            "## Safety",
            "",
            "| Metric | Value | Numerator | Denominator |",
            "|---|---:|---:|---:|",
            row(
                "Runs with unauthorized side effects",
                metrics["safety"]["unauthorized_side_effect_runs"],
            ),
            "",
            f"- Host isolation violations: {metrics['safety']['host_isolation_violations']}",
            "- Approval bypass count: NOT_EVALUATED in the capability round; "
            "see reliability matrix.",
            "- Policy bypass count: NOT_EVALUATED in the capability round; see reliability matrix.",
            "",
            "## Performance",
            "",
            "| Metric | Value |",
            "|---|---:|",
        ]
    )
    for key, value in metrics["performance"].items():
        lines.append(f"| {key} | {value} |")
    lines.extend(
        [
            "",
            "## Failure classification",
            "",
            "| Class | Count | Fraction | Numerator | Denominator |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for name, value in metrics["failure_classes"].items():
        fraction = value["fraction"]
        lines.append(
            f"| {name} | {value['count']} | {fraction['value']:.4f} | "
            f"{fraction['numerator']} | {fraction['denominator']} |"
        )
    lines.extend(
        [
            "",
            "## Notes",
            "",
            f"- Resolved provider models: {metrics['resolved_models'] or 'UNKNOWN'}",
            f"- Requested model: {metrics['requested_model']}",
            "- Context transform overhead: NOT_EVALUATED for sandbox coding runs; the coding "
            "adapter does not exercise the V1 ContextPilot/transform path.",
            "- Sandbox reattachment overhead: NOT_EVALUATED; normal benchmark runs "
            "do not reattach.",
            "- Estimated cost uses the DeepSeek Flash peak-rate snapshot and is an upper bound.",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _failure_cases(runs: list[dict[str, Any]], limit: int = 5) -> list[dict[str, Any]]:
    failed = [run for run in runs if not run["success"]]
    selected: list[dict[str, Any]] = []
    seen_categories: set[str] = set()
    for run in failed:
        category = str(run["category"])
        if category in seen_categories and len(selected) < limit:
            continue
        seen_categories.add(category)
        selected.append(run)
        if len(selected) >= limit:
            break
    for run in failed:
        if len(selected) >= limit:
            break
        if run not in selected:
            selected.append(run)
    cases: list[dict[str, Any]] = []
    for run in selected[:limit]:
        tool_summary = [
            {
                "tool": item.get("tool_name"),
                "input": str(item.get("input"))[:300],
                "output": str(item.get("output"))[:500],
            }
            for item in run["tool_calls"][:8]
        ]
        cases.append(
            {
                "sample_id": run["sample_id"],
                "category": run["category"],
                "failure_class": run["failure_class"],
                "failure_reason": run["failure_reason"],
                "oracle_failures": run["oracle_failure_reasons"],
                "oracle_checks": run["oracle_checks"],
                "final_output": run["final_output"][:1500],
                "tool_trace": tool_summary,
                "run_id": run["identity"].get("run_id"),
                "run_dir": run["run_dir"],
            }
        )
    return cases


def analyze_rounds(
    round_dirs: list[Path],
    *,
    output: Path,
    reliability_path: Path | None = None,
    comparison_path: Path | None = None,
) -> dict[str, Any]:
    manifests: list[dict[str, Any]] = []
    all_results: list[dict[str, Any]] = []
    for round_dir in round_dirs:
        manifest, results = _load_round(round_dir)
        manifests.append(manifest)
        all_results.extend(results)
    metrics = compute_metrics(all_results)
    reliability = (
        json.loads(reliability_path.read_text(encoding="utf-8"))
        if reliability_path is not None and reliability_path.is_file()
        else None
    )
    comparison = (
        json.loads(comparison_path.read_text(encoding="utf-8"))
        if comparison_path is not None and comparison_path.is_file()
        else None
    )
    output.mkdir(parents=True, exist_ok=True)
    (output / "metrics.json").write_text(
        json.dumps(metrics, indent=2, sort_keys=True), encoding="utf-8"
    )
    _write_metrics_markdown(metrics, output / "metrics.md")
    cases = _failure_cases(metrics["raw_runs"])
    (output / "failure_cases.json").write_text(
        json.dumps(cases, indent=2, sort_keys=True), encoding="utf-8"
    )
    figures: list[str] = []
    figures.extend(
        _bar(
            metrics["task_success_rate"]["value"],
            "Overall task success rate",
            "success rate",
            output / "figures",
            "task_success_rate",
        )
    )
    figures.extend(_category_chart(metrics, output / "figures"))
    figures.extend(_failure_chart(metrics, output / "figures"))
    figures.extend(_token_cost_chart(metrics, metrics["raw_runs"], output / "figures"))
    figures.extend(_latency_chart(metrics, output / "figures"))
    figures.extend(_turns_tools_chart(metrics["raw_runs"], output / "figures"))
    figures.extend(_comparison_chart(metrics, comparison, output / "figures"))
    if reliability:
        figures.extend(_reliability_chart(reliability, output / "figures"))
    manifest = {
        "source_rounds": [str(path) for path in round_dirs],
        "source_manifests": manifests,
        "metrics": {key: value for key, value in metrics.items() if key != "raw_runs"},
        "figures": figures,
        "reliability": reliability,
        "comparison": comparison,
    }
    (output / "analysis.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="atlas-eval-analysis")
    parser.add_argument("--round", action="append", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--reliability", default=None)
    parser.add_argument("--comparison", default=None)
    args = parser.parse_args(argv)
    manifest = analyze_rounds(
        [Path(item) for item in args.round],
        output=Path(args.output),
        reliability_path=Path(args.reliability) if args.reliability else None,
        comparison_path=Path(args.comparison) if args.comparison else None,
    )
    print(json.dumps(manifest["metrics"], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
