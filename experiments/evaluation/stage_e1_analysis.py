"""Aggregate Stage E1 paired A0/A1 results and read-only collaboration evidence."""

from __future__ import annotations

import argparse
import json
import math
import statistics
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from experiments.evaluation.stage_e_evidence import collect_evidence
from experiments.evaluation.stage_e_tasks import DEFINITIONS

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = REPO_ROOT / "experiments" / "results" / "evaluation" / "stage-e1"
FAMILY = {item.sample_id: item.family for item in DEFINITIONS}


def _latest_manifest() -> Path:
    manifests = sorted((REPO_ROOT / "experiments" / "results" / "stage-e1").glob("*/manifest.json"))
    if not manifests:
        raise SystemExit("no Stage E1 manifest found")
    return manifests[-1]


def _run_result(run: dict[str, Any]) -> dict[str, Any]:
    run_dir = Path(run.get("run_dir") or "")
    path = run_dir / "result.json"
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def _p50(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def _metrics(runs: list[dict[str, Any]]) -> dict[str, Any]:
    for item in runs:
        if "_model_turns" not in item:
            detail = _run_result(item)
            metrics = detail.get("metrics") or {}
            item["_model_turns"] = int(metrics.get("model_turns") or 0)
            item["_cache_hit"] = int(metrics.get("cache_hit_tokens") or 0)
            item["_cache_miss"] = int(metrics.get("cache_miss_tokens") or 0)
    latency = [float(item.get("latency_ms") or 0.0) for item in runs]
    prompt = [int(item.get("prompt_tokens") or 0) for item in runs]
    completion = [int(item.get("completion_tokens") or 0) for item in runs]
    cost = [float(item.get("cost") or 0.0) for item in runs]
    tools = [int(item.get("tool_calls") or 0) for item in runs]
    turns = [int(item.get("_model_turns") or 0) for item in runs]
    cache_hit = [int(item.get("_cache_hit") or 0) for item in runs]
    cache_miss = [int(item.get("_cache_miss") or 0) for item in runs]
    return {
        "runs": len(runs),
        "success": sum(1 for item in runs if item.get("success")),
        "prompt_tokens": sum(prompt),
        "model_turns": sum(turns),
        "cache_hit_tokens": sum(cache_hit),
        "cache_miss_tokens": sum(cache_miss),
        "completion_tokens": sum(completion),
        "cost": round(sum(cost), 8),
        "latency_p50_ms": _p50(latency),
        "tool_calls": sum(tools),
        "prompt_tokens_mean": statistics.mean(prompt) if prompt else None,
        "completion_tokens_mean": statistics.mean(completion) if completion else None,
        "cost_mean": statistics.mean(cost) if cost else None,
        "latency_mean_ms": statistics.mean(latency) if latency else None,
        "tool_calls_mean": statistics.mean(tools) if tools else None,
        "model_turns_mean": statistics.mean(turns) if turns else None,
        "cache_hit_ratio": (sum(cache_hit) / (sum(cache_hit) + sum(cache_miss)))
        if (sum(cache_hit) + sum(cache_miss))
        else None,
    }


def _paired(pairs: list[dict[str, Any]]) -> dict[str, Any]:
    diffs = [
        int(bool(pair["runs"]["a1"].get("success"))) - int(bool(pair["runs"]["a0"].get("success")))
        for pair in pairs
    ]
    if not diffs:
        return {"n": 0, "mean_difference": None, "ci95": None}
    mean = statistics.mean(diffs)
    if len(diffs) > 1:
        stdev = statistics.stdev(diffs)
        ci = 1.96 * stdev / math.sqrt(len(diffs))
    else:
        ci = None
    return {
        "n": len(diffs),
        "mean_difference": mean,
        "ci95_low": mean - ci if ci is not None else None,
        "ci95_high": mean + ci if ci is not None else None,
        "a1_only": sum(1 for value in diffs if value > 0),
        "a0_only": sum(1 for value in diffs if value < 0),
        "both": sum(1 for value in diffs if value == 0),
    }


def _collect_evidence(runs: list[dict[str, Any]]) -> dict[str, Any]:
    edges: list[dict[str, Any]] = []
    steps: list[dict[str, Any]] = []
    profiles: set[str] = set()
    dag_nodes: list[int] = []
    critical_paths: list[int] = []
    all_success_task_failed = 0
    stop_codes: dict[str, int] = {}
    for run in runs:
        run_dir = run.get("run_dir")
        if not run_dir or not Path(run_dir).is_dir():
            continue
        evidence = collect_evidence(run_dir)
        edges.extend(evidence["edges"])
        steps.extend(evidence["steps"])
        step_states = [step.get("agent_success") for step in evidence["steps"]]
        if step_states and all(state is True for state in step_states) and not run.get("success"):
            all_success_task_failed += 1
        detail_path = Path(run_dir) / "result.json"
        if detail_path.is_file():
            detail = json.loads(detail_path.read_text(encoding="utf-8"))
            for worker in (detail.get("worker_results") or {}).values():
                code = str((worker.get("stop_reason") or {}).get("code") or "unknown")
                stop_codes[code] = stop_codes.get(code, 0) + 1
        dag_nodes.append(int(evidence["dag_nodes"]))
        critical_paths.append(int(evidence["critical_path_nodes"]))
        for step in evidence["steps"]:
            if step.get("profile_id"):
                profiles.add(str(step["profile_id"]))
    step_by_id = {str(step.get("step_task_id")): step for step in steps}
    return {
        "edges": len(edges),
        "handoff_complete_edges": sum(1 for edge in edges if edge.get("handoff_complete")),
        "handoff_consumed_edges": sum(1 for edge in edges if edge.get("consumed")),
        "handoff_bytes": sum(int(edge.get("handoff_bytes") or 0) for edge in edges),
        "worker_steps": len(steps),
        "worker_failures": sum(1 for step in steps if step.get("agent_success") is False),
        "worker_successes": sum(1 for step in steps if step.get("agent_success") is True),
        "all_workers_success_task_failed": all_success_task_failed,
        "worker_stop_codes": stop_codes,
        "upstream_success_downstream_failure": sum(
            1
            for edge in edges
            if step_by_id.get(edge["from"], {}).get("agent_success") is True
            and step_by_id.get(edge["to"], {}).get("agent_success") is False
        ),
        "dag_nodes_mean": statistics.mean(dag_nodes) if dag_nodes else None,
        "critical_path_nodes_mean": statistics.mean(critical_paths) if critical_paths else None,
        "profiles": sorted(profiles),
        "edges_detail": edges,
    }


def _write_charts(metrics: dict[str, Any], families: dict[str, Any], output: Path) -> list[str]:
    figures = output / "figures"
    figures.mkdir(parents=True, exist_ok=True)
    paths: list[str] = []
    fig, ax = plt.subplots(figsize=(6.4, 3.8))
    labels = ["A0 single worker", "A1 multi-worker"]
    rates = [
        metrics["A0"]["success"] / metrics["A0"]["runs"],
        metrics["A1"]["success"] / metrics["A1"]["runs"],
    ]
    bars = ax.bar(labels, rates, color=["#4c78a8", "#f28e2b"])
    ax.set_ylim(0, 1.08)
    ax.set_ylabel("task success")
    ax.set_title("Stage E1 paired task success")
    for bar, value in zip(bars, rates, strict=True):
        ax.text(bar.get_x() + bar.get_width() / 2, value + 0.015, f"{value:.3f}", ha="center")
    ax.grid(axis="y", alpha=0.25)
    path = figures / "paired_success.png"
    fig.savefig(path, dpi=160, bbox_inches="tight")
    plt.close(fig)
    paths.append(str(path.relative_to(output)))

    families_names = sorted(families)
    a0 = [families[name]["A0"]["success"] / families[name]["A0"]["runs"] for name in families_names]
    a1 = [families[name]["A1"]["success"] / families[name]["A1"]["runs"] for name in families_names]
    fig, ax = plt.subplots(figsize=(8.0, 3.8))
    x = range(len(families_names))
    ax.bar([value - 0.2 for value in x], a0, width=0.4, label="A0", color="#4c78a8")
    ax.bar([value + 0.2 for value in x], a1, width=0.4, label="A1", color="#f28e2b")
    ax.set_xticks(list(x), families_names, rotation=15)
    ax.set_ylim(0, 1.08)
    ax.set_ylabel("task success")
    ax.set_title("Stage E1 success by task family")
    ax.legend()
    path = figures / "family_success.png"
    fig.savefig(path, dpi=160, bbox_inches="tight")
    plt.close(fig)
    paths.append(str(path.relative_to(output)))

    fig, ax = plt.subplots(figsize=(6.4, 3.8))
    labels = ["A0 prompt", "A1 prompt", "A0 completion", "A1 completion"]
    values = [
        metrics["A0"]["prompt_tokens_mean"],
        metrics["A1"]["prompt_tokens_mean"],
        metrics["A0"]["completion_tokens_mean"],
        metrics["A1"]["completion_tokens_mean"],
    ]
    bars = ax.bar(labels, values, color=["#4c78a8", "#f28e2b", "#4c78a8", "#f28e2b"])
    ax.set_ylabel("tokens per run (mean)")
    ax.set_title("Stage E1 token cost")
    for bar, value in zip(bars, values, strict=True):
        ax.text(bar.get_x() + bar.get_width() / 2, value + 20, f"{value:.0f}", ha="center")
    path = figures / "paired_tokens.png"
    fig.savefig(path, dpi=160, bbox_inches="tight")
    plt.close(fig)
    paths.append(str(path.relative_to(output)))
    return paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    manifest_path = args.manifest or _latest_manifest()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    pairs = manifest.get("pairs") or []
    a0 = [pair["runs"]["a0"] for pair in pairs]
    a1 = [pair["runs"]["a1"] for pair in pairs]
    families: dict[str, dict[str, Any]] = {}
    for pair in pairs:
        family = FAMILY.get(pair["definition"], "unknown")
        families.setdefault(family, {"A0": [], "A1": []})
        families[family]["A0"].append(pair["runs"]["a0"])
        families[family]["A1"].append(pair["runs"]["a1"])
    family_metrics = {
        family: {variant: _metrics(values) for variant, values in variants.items()}
        for family, variants in families.items()
    }
    failure_cases: list[dict[str, Any]] = []
    for pair in pairs:
        for variant in ("a0", "a1"):
            run = pair["runs"][variant]
            if run.get("success"):
                continue
            detail = _run_result(run)
            failure_cases.append(
                {
                    "definition": pair["definition"],
                    "family": FAMILY.get(pair["definition"]),
                    "variant": variant,
                    "repetition": pair["repetition"],
                    "failure_class": run.get("failure_class") or detail.get("failure_class"),
                    "reason": detail.get("failure_reason"),
                    "oracle": detail.get("oracle"),
                }
            )
    evidence = _collect_evidence(a1)
    metrics = {"A0": _metrics(a0), "A1": _metrics(a1)}
    infra = [
        {
            "definition": pair["definition"],
            "variant": variant,
            "repetition": pair["repetition"],
        }
        for pair in pairs
        for variant in ("a0", "a1")
        if pair["runs"][variant].get("success") is None
        and pair["runs"][variant].get("failure_class") is None
    ]
    summary = {
        "source_manifest": str(manifest_path),
        "git_commit": manifest.get("git_commit"),
        "git_dirty": manifest.get("git_dirty"),
        "metrics": metrics,
        "infra_interruptions": infra,
        "a0_adjusted_success": metrics["A0"]["success"],
        "a0_adjusted_runs": metrics["A0"]["runs"]
        - len([item for item in infra if item["variant"] == "a0"]),
        "paired": _paired(pairs),
        "families": family_metrics,
        "evidence": {key: value for key, value in evidence.items() if key != "edges_detail"},
        "failures": failure_cases,
    }
    output = args.output
    output.mkdir(parents=True, exist_ok=True)
    (output / "metrics.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8"
    )
    figures = _write_charts(metrics, family_metrics, output)
    lines = [
        "# Stage E1 - Paired Multi-Worker Evaluation",
        "",
        f"Source manifest: `{manifest_path}`",
        f"Git commit: `{manifest.get('git_commit')}`; dirty: `{manifest.get('git_dirty')}`",
        "",
        "## Overall",
        "",
        "| variant | runs | success | prompt tokens | completion tokens | turns | cache ratio "
        "| cost USD | p50 latency ms | tool calls |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for variant, item in metrics.items():
        lines.append(
            f"| {variant} | {item['runs']} | {item['success']}/{item['runs']} | "
            f"{item['prompt_tokens']} | {item['completion_tokens']} | {item['model_turns']} | "
            f"{item['cache_hit_ratio'] or 0.0:.4f} | {item['cost']:.6f} | "
            f"{item['latency_p50_ms'] or 0.0:.1f} | {item['tool_calls']} |"
        )
    lines.extend(
        [
            "",
            "## Paired difference (A1 - A0)",
            "",
            f"- pairs: {summary['paired']['n']}",
            f"- mean difference: {summary['paired']['mean_difference']}",
            f"- 95% normal-approximation CI: "
            f"[{summary['paired']['ci95_low']}, {summary['paired']['ci95_high']}]",
            f"- A1-only successes: {summary['paired']['a1_only']}; A0-only: "
            f"{summary['paired']['a0_only']}; both same: {summary['paired']['both']}",
            "",
            "Small paired samples; the CI is descriptive, not a significance claim.",
            "",
            f"- infrastructure interruptions (excluded from adjusted A0 denominator): "
            f"{len([item for item in infra if item['variant'] == 'a0'])}; "
            f"A0 adjusted: {summary['a0_adjusted_success']}/{summary['a0_adjusted_runs']}",
            "",
            "## Collaboration evidence",
            "",
            f"- A1 worker steps observed: {evidence['worker_steps']}",
            f"- worker successes / failures: "
            f"{evidence['worker_successes']} / {evidence['worker_failures']}",
            f"- dependency edges: {evidence['edges']}",
            f"- handoff complete edges: {evidence['handoff_complete_edges']}",
            f"- handoff consumed edges: {evidence['handoff_consumed_edges']}",
            f"- transmitted handoff bytes (actual prefix): {evidence['handoff_bytes']}",
            f"- worker profiles: {', '.join(evidence['profiles']) or 'none'}",
            f"- mean DAG nodes / critical path: {evidence['dag_nodes_mean']} / "
            f"{evidence['critical_path_nodes_mean']}",
            f"- all workers reported success but task failed: "
            f"{evidence['all_workers_success_task_failed']} runs",
            f"- upstream success -> downstream failure edges: "
            f"{evidence['upstream_success_downstream_failure']}",
            f"- worker stop codes: {evidence['worker_stop_codes']}",
            "",
            "## Families",
            "",
            "| family | A0 | A1 |",
            "|---|---:|---:|",
        ]
    )
    for family, values in sorted(family_metrics.items()):
        lines.append(
            f"| {family} | {values['A0']['success']}/{values['A0']['runs']} | "
            f"{values['A1']['success']}/{values['A1']['runs']} |"
        )
    lines.extend(["", "## Failure classes", ""])
    classes: dict[str, int] = {}
    for failure in failure_cases:
        key = f"{failure['variant']}:{failure['failure_class']}"
        classes[key] = classes.get(key, 0) + 1
    for key, count in sorted(classes.items()):
        lines.append(f"- {key}: {count}")
    lines.extend(["", "Figures: " + ", ".join(f"`{path}`" for path in figures), ""])
    (output / "metrics.md").write_text("\n".join(lines), encoding="utf-8")
    print(
        json.dumps(
            {
                "metrics": metrics,
                "paired": summary["paired"],
                "evidence": summary["evidence"],
                "figures": figures,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
