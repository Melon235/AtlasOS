"""Aggregate Stage E2 Track A results; never alters prompts or task execution."""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = REPO_ROOT / "experiments" / "results" / "evaluation" / "stage-e2"


def _latest_manifest() -> Path:
    rounds = sorted((REPO_ROOT / "experiments" / "results" / "stage-e2").glob("*/manifest.json"))
    if not rounds:
        raise SystemExit("no Stage E2 manifest found")
    return rounds[-1]


def _p50(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def _prompt_bytes(record: dict[str, Any], *, agent: str | None = None) -> int:
    total = 0
    for prompt in record.get("prompts") or []:
        if agent is not None and prompt.get("agent") != agent:
            continue
        for message in prompt.get("messages") or []:
            total += len(json.dumps(message, ensure_ascii=False).encode("utf-8"))
    return total


def _prompt_digest(record: dict[str, Any]) -> str:
    text = json.dumps(record.get("prompts") or [], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _summarize(values: list[dict[str, Any]]) -> dict[str, Any]:
    for item in values:
        record_path = Path(item.get("record_path") or "")
        if not item.get("prompts") and record_path.is_file():
            detail = json.loads(record_path.read_text(encoding="utf-8"))
            item["prompts"] = detail.get("prompts") or []
    correct = sum(1 for item in values if item.get("correct"))
    prompt = [int(item.get("prompt_tokens") or 0) for item in values]
    hit = [int(item.get("cache_hit_tokens") or 0) for item in values]
    miss = [int(item.get("cache_miss_tokens") or 0) for item in values]
    latencies = [float(item.get("latency_ms") or 0.0) for item in values]
    records = [record for item in values for record in (item.get("inference_records") or [])]
    dedup = [record for record in records if record.get("deduplicated")]
    return {
        "runs": len(values),
        "correct": correct,
        "success_rate": correct / len(values) if values else None,
        "prompt_tokens": sum(prompt),
        "prompt_tokens_mean": statistics.mean(prompt) if prompt else None,
        "completion_tokens": sum(int(item.get("completion_tokens") or 0) for item in values),
        "cache_hit_tokens": sum(hit),
        "cache_miss_tokens": sum(miss),
        "cache_hit_ratio": (sum(hit) / (sum(hit) + sum(miss))) if (sum(hit) + sum(miss)) else None,
        "estimated_cost": round(
            sum(float(item.get("estimated_cost") or 0.0) for item in values), 8
        ),
        "latency_p50_ms": _p50(latencies),
        "transform_latency_ms": sum(
            float(item.get("transform_latency_ms") or 0.0) for item in values
        ),
        "deduplicated_blocks": sum(
            int(record.get("original_block_count") or 0)
            - int(record.get("optimized_block_count") or 0)
            for record in records
        ),
        "deduplicated_calls": len(dedup),
        "block_reduction_chars": sum(
            int(record.get("original_context_chars") or 0)
            - int(record.get("optimized_context_chars") or 0)
            for record in records
        ),
        "all_prompt_bytes": sum(_prompt_bytes(item) for item in values),
        "handoff_prompt_bytes": sum(_prompt_bytes(item, agent="merger") for item in values),
        "prompt_digests": len({_prompt_digest(item) for item in values}),
        "dependency_agent_switches": sum(
            int(item.get("dependency_agent_switches") or 0) for item in values
        ),
        "mean_assignment_locality": statistics.mean(
            [
                float(item["mean_assignment_locality"])
                for item in values
                if item.get("mean_assignment_locality") is not None
            ]
        )
        if any(item.get("mean_assignment_locality") is not None for item in values)
        else None,
    }


def _write_charts(summary: dict[str, Any], output: Path) -> list[str]:
    figures = output / "figures"
    figures.mkdir(parents=True, exist_ok=True)
    paths: list[str] = []

    context_keys = [key for key in summary if key.startswith("context:")]
    labels = [key.split(":")[1] for key in context_keys]
    success = [summary[key]["success_rate"] or 0.0 for key in context_keys]
    fig, ax = plt.subplots(figsize=(6.5, 3.8))
    bars = ax.bar(labels, success, color=["#4c78a8", "#59a14f", "#f28e2b"])
    ax.set_ylim(0, 1.08)
    ax.set_ylabel("exact-result success")
    ax.set_title("E2 Context Plane correctness (8 tasks x 3 reps)")
    for bar, value in zip(bars, success, strict=True):
        ax.text(bar.get_x() + bar.get_width() / 2, value + 0.015, f"{value:.3f}", ha="center")
    ax.grid(axis="y", alpha=0.25)
    path = figures / "context_correctness.png"
    fig.savefig(path, dpi=160, bbox_inches="tight")
    plt.close(fig)
    paths.append(str(path.relative_to(output)))

    fig, ax = plt.subplots(figsize=(7.0, 3.8))
    misses = [summary[key]["cache_miss_tokens"] for key in context_keys]
    hits = [summary[key]["cache_hit_tokens"] for key in context_keys]
    ax.bar(labels, misses, label="cache miss", color="#e15759")
    ax.bar(labels, hits, bottom=misses, label="cache hit", color="#76b7b2")
    ax.set_ylabel("provider tokens (sum)")
    ax.set_title("Prompt/cache tokens by context configuration")
    ax.legend()
    path = figures / "context_tokens.png"
    fig.savefig(path, dpi=160, bbox_inches="tight")
    plt.close(fig)
    paths.append(str(path.relative_to(output)))

    placement_keys = [key for key in summary if key.startswith("placement:")]
    if placement_keys:
        labels = [key.split(":")[2] for key in placement_keys]
        switches = [summary[key]["dependency_agent_switches"] for key in placement_keys]
        fig, ax = plt.subplots(figsize=(6.5, 3.8))
        bars = ax.bar(labels, switches, color=["#4c78a8", "#59a14f"])
        ax.set_ylabel("dependency agent switches (sum)")
        ax.set_title("Placement: switch count (12 paired runs each)")
        for bar, value in zip(bars, switches, strict=True):
            ax.text(bar.get_x() + bar.get_width() / 2, value + 0.5, str(value), ha="center")
        path = figures / "placement_switches.png"
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
    groups: dict[str, list[dict[str, Any]]] = {}
    for run in manifest.get("runs") or []:
        key = f"{run['workflow']}:{run['context']}:{run['placement']}"
        groups.setdefault(key, []).append(run)
    summary = {key: _summarize(values) for key, values in sorted(groups.items())}
    failures = []
    for run in manifest.get("runs") or []:
        if run.get("correct"):
            continue
        expected = None
        record_path = Path(run.get("record_path") or "")
        if record_path.is_file():
            expected = json.loads(record_path.read_text(encoding="utf-8")).get("expected")
        failures.append(
            {
                "task_id": run["task_id"],
                "context": run["context"],
                "repetition": run["repetition"],
                "expected": expected,
                "final_output": run.get("final_output"),
            }
        )
    output = args.output
    output.mkdir(parents=True, exist_ok=True)
    (output / "metrics.json").write_text(
        json.dumps(
            {"source_manifest": str(manifest_path), "summary": summary, "failures": failures},
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    figures = _write_charts(summary, output)
    lines = [
        "# Stage E2 - Context Plane Track A",
        "",
        f"Source manifest: `{manifest_path}`",
        f"Git commit: `{manifest.get('git_commit')}`; dirty: `{manifest.get('git_dirty')}`",
        "",
        "## Aggregate",
        "",
        "| configuration | runs | correct | prompt tokens | cache hit | cache miss | hit ratio | cost USD | p50 latency ms | dedup calls | block chars removed | transform ms | switches | locality |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for key, item in summary.items():
        lines.append(
            f"| {key} | {item['runs']} | {item['correct']}/{item['runs']} | "
            f"{item['prompt_tokens']} | {item['cache_hit_tokens']} | "
            f"{item['cache_miss_tokens']} | {item['cache_hit_ratio'] or 0.0:.4f} | "
            f"{item['estimated_cost']:.6f} | {item['latency_p50_ms'] or 0.0:.1f} | "
            f"{item['deduplicated_calls']} | {item['block_reduction_chars']} | "
            f"{item['transform_latency_ms']:.2f} | {item['dependency_agent_switches']} | "
            f"{item['mean_assignment_locality']} |"
        )
    lines.extend(
        [
            "",
            "## Notes",
            "",
            "- C0 is upstream_default; C1 adds task-scoped selection + stable rendering + identity "
            "transform; C2 adds ContextPilot dedup. Only this factor changes.",
            "- Correctness is an exact-result token check, not an LLM judge.",
            "- C0 executes before C1/C2 for each task; provider cache is warm-order sensitive. Hit "
            "ratio differences are observations, not causal cache improvements.",
            "- Dedup calls and block-char removal are measured from real inference records; zero "
            "means the transform did not remove a block in these workloads.",
            "- P0/P1 use C1 context with capability_only vs context_locality placement; 4 tasks x 3 "
            "repetitions.",
            "",
        ]
    )
    (output / "metrics.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"summary": summary, "figures": figures, "failures": failures}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
