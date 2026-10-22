"""Stage 6 aggregation: markdown tables, E5 regression check, and dependency-free SVG plots."""

from __future__ import annotations

import json
import statistics
from collections import defaultdict
from pathlib import Path
from xml.sax.saxutils import escape

from atlasos.experiments import ExperimentStore

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNS = REPO_ROOT / "experiments" / "runs"
RESULTS = REPO_ROOT / "experiments" / "results"
FIGURES = RESULTS / "figures"


def _rows(name: str) -> list[dict]:
    return ExperimentStore(RUNS / name).read_all()


def _fmt(value: float | None, digits: int = 2) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def e1_table(rows: list[dict]) -> str:
    lines = [
        "| renderer | completion order | rendered order | layout hash | invariant |",
        "|---|---|---|---|---|",
    ]
    by_renderer: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_renderer[row["renderer"]].append(row)
    for renderer in ("naive_execution_order", "stable_deterministic"):
        variants = by_renderer[renderer]
        hashes = {row["layout_hash"] for row in variants}
        for row in variants:
            lines.append(
                f"| {renderer} | {row['completion_order']} | "
                f"{', '.join(row['rendered_block_order'])} | "
                f"`{row['layout_hash'][:16]}...` | {len(hashes) == 1} |"
            )
    return "\n".join(lines)


def e2_offline_table(rows: list[dict]) -> str:
    lines = [
        "| scale | duplicate ratio | T1 char reduction | T0 latency ms | T1 latency ms |",
        "|---|---:|---:|---:|---:|",
    ]
    lookup = {(row["scale"], row["duplicate_ratio"], row["transform_name"]): row for row in rows}
    for row in rows:
        if row["transform_name"] != "contextpilot_dedup":
            continue
        key = (row["scale"], row["duplicate_ratio"], "identity")
        identity = lookup.get(key)
        lines.append(
            f"| {row['scale']} | {row['duplicate_ratio']:.3f} | "
            f"{row['reduction_ratio'] * 100:.1f}% | "
            f"{_fmt(identity['latency_mean_ms'] if identity else None)} | "
            f"{_fmt(row['latency_mean_ms'])} |"
        )
    return "\n".join(lines)


def _totals(rows: list[dict], *, warm_only: bool = False) -> dict[str, float | int]:
    selected = [row for row in rows if not warm_only or row["repetition"] >= 2]
    prompt = sum(row["prompt_tokens"] for row in selected)
    hit = sum(row["cache_hit_tokens"] or 0 for row in selected)
    miss = sum(row["cache_miss_tokens"] or 0 for row in selected)
    return {
        "prompt": prompt,
        "hit": hit,
        "miss": miss,
        "ratio": hit / prompt if prompt else 0.0,
        "latency": statistics.fmean([row["provider_latency_ms"] for row in selected]),
    }


def e2_live_table(rows: list[dict]) -> tuple[str, list[dict]]:
    by_variant: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_variant[row["variant"]].append(row)
    lines = [
        "| scale | duplicate ratio | T0 prompt | T1 prompt | prompt reduction | "
        "T0 warm miss | T1 warm miss | warm miss reduction | T0 weighted hit | "
        "T1 weighted hit |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    paired: list[dict] = []
    for scale in ("small", "medium", "large"):
        for duplicate in ("D0", "D2", "D3"):
            t0 = by_variant.get(f"T0-{scale}-{duplicate}", [])
            t1 = by_variant.get(f"T1-{scale}-{duplicate}", [])
            if not t0 or not t1:
                continue
            # Prompt totals use all five repetitions; cache comparisons use warm reps 2-5,
            # because the provider cache is warm-prone and the first call of a variant is
            # measurably colder than its steady state.
            a = _totals(t0, warm_only=True)
            b = _totals(t1, warm_only=True)
            a_all = _totals(t0)
            b_all = _totals(t1)
            prompt_reduction = (
                (a_all["prompt"] - b_all["prompt"]) / a_all["prompt"] if a_all["prompt"] else 0.0
            )
            miss_reduction = (a["miss"] - b["miss"]) / a["miss"] if a["miss"] else 0.0
            paired.append(
                {
                    "scale": scale,
                    "duplicate_label": duplicate,
                    "duplicate_ratio": t1[0]["duplicate_ratio"],
                    "prompt_reduction": prompt_reduction,
                    "miss_reduction": miss_reduction,
                    "t0": a,
                    "t1": b,
                }
            )
            lines.append(
                f"| {scale} | {t1[0]['duplicate_ratio']:.3f} | {a_all['prompt']:,} | "
                f"{b_all['prompt']:,} | {prompt_reduction * 100:.1f}% | "
                f"{a['miss']:,} | {b['miss']:,} | "
                f"{miss_reduction * 100:.1f}% | {a['ratio']:.4f} | {b['ratio']:.4f} |"
            )
    return "\n".join(lines), paired


def e3_table(rows: list[dict]) -> str:
    lines = ["| topology | chain length | policy | switches | handoff refs | mean locality |"]
    lines.append("|---|---:|---|---:|---:|---:|")
    for row in rows:
        if row["experiment"] != "E3":
            continue
        lines.append(
            f"| {row['variant'].split('-')[0]} | {row['chain_length']} | {row['policy']} | "
            f"{row['dependency_agent_switches']} | {row['total_logical_handoff_refs']} | "
            f"{row['mean_assignment_locality']:.3f} |"
        )
    return "\n".join(lines)


def e4_table(rows: list[dict]) -> str:
    by_variant: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_variant[row["variant"]].append(row)
    lines = [
        "| variant | policy | transform | handoff | switches | locality | prompt | "
        "cache miss | transform ms |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for variant in sorted(by_variant):
        group = by_variant[variant]
        first = group[0]
        prompt = statistics.fmean([row["prompt_tokens"] for row in group])
        warm_miss = statistics.fmean(
            [row["cache_miss_tokens"] or 0 for row in group if row["repetition"] >= 2]
        )
        transform = statistics.fmean([row["transform_latency_ms"] for row in group])
        lines.append(
            f"| {variant.split('-')[0]} | {first['placement_policy']} | "
            f"{first['context_transform']} | {first['total_logical_handoff_refs']} | "
            f"{first['dependency_agent_switches']} | {first['mean_assignment_locality']:.3f} | "
            f"{prompt:,.0f} | {warm_miss:,.0f} | {transform:.2f} |"
        )
    return "\n".join(lines)


def e5_records() -> tuple[str, list[dict]]:
    rows = _rows("stage5-formal.jsonl")
    latest: dict[str, dict] = {}
    for row in rows:
        if row.get("variant") in {"R0", "R1", "R2"}:
            latest[row["variant"]] = row
    expected = {
        "R1": {"research-a": 1, "research-b": 2, "review": 1},
        "R2": {"research-a": 1, "research-b": 1, "review": 1},
    }
    for variant, counts in expected.items():
        row = latest.get(variant)
        if row is None:
            raise RuntimeError(f"Stage 5 formal record for {variant} is missing")
        if row["execution_count_by_task"] != counts:
            actual = row["execution_count_by_task"]
            raise RuntimeError(f"recovery regression: {variant} counts {actual} != {counts}")
        if row["completed_tasks_reexecuted"] != 0:
            raise RuntimeError(f"recovery regression: {variant} re-executed completed tasks")
        if not row["recovery_success"]:
            raise RuntimeError(f"recovery regression: {variant} did not succeed")
    r0, r2 = latest["R0"], latest["R2"]
    for field in (
        "context_ref_equivalent",
        "context_layout_equivalent",
        "prompt_equivalent",
        "final_output_equivalent",
    ):
        if not r2[field]:
            raise RuntimeError(f"recovery regression: R2 {field} is false")
    if r0["context_refs"] != r2["context_refs"]:
        raise RuntimeError("recovery regression: R0/R2 context refs differ")

    lines = [
        "| scenario | completed reexecuted | in-flight reexecuted | rebuilt blocks | "
        "layout equivalent | final equivalent |",
        "|---|---:|---:|---:|---|---|",
    ]
    for variant in ("R0", "R1", "R2"):
        row = latest[variant]
        layout = row["context_layout_equivalent"]
        final = row["final_output_equivalent"]
        lines.append(
            f"| {variant} | {row['completed_tasks_reexecuted']} | "
            f"{row['inflight_tasks_reexecuted']} | {row['context_blocks_rebuilt']} | "
            f"{layout if layout is not None else '-'} | {final if final is not None else '-'} |"
        )
    summary = [
        {
            "experiment": "E5",
            "variant": variant,
            "run_id": latest[variant]["run_id"],
            "git_commit": latest[variant]["git_commit"],
            "git_dirty": latest[variant]["git_dirty"],
            "execution_count_by_task": latest[variant]["execution_count_by_task"],
            "completed_tasks_reexecuted": latest[variant]["completed_tasks_reexecuted"],
            "inflight_tasks_reexecuted": latest[variant]["inflight_tasks_reexecuted"],
            "context_blocks_rebuilt": latest[variant]["context_blocks_rebuilt"],
            "context_layout_equivalent": latest[variant]["context_layout_equivalent"],
            "final_output_equivalent": latest[variant]["final_output_equivalent"],
        }
        for variant in ("R0", "R1", "R2")
    ]
    return "\n".join(lines), summary


def _svg_line_chart(
    path: Path,
    *,
    title: str,
    x_labels: list[str],
    series: list[tuple[str, str, list[float]]],
    y_label: str,
    y_suffix: str = "",
) -> None:
    width, height = 680, 400
    left, right, top, bottom = 70, 20, 50, 60
    plot_w = width - left - right
    plot_h = height - top - bottom
    all_values = [value for _, _, values in series for value in values]
    y_min = min(0.0, min(all_values))
    y_max = max(0.0, max(all_values))
    span = y_max - y_min
    if span <= 0:
        span = 1.0
    y_min -= span * 0.08 if y_min < 0 else 0.0
    y_max += span * 0.12

    def x_pos(index: int) -> float:
        return left + plot_w * index / max(1, len(x_labels) - 1)

    def y_pos(value: float) -> float:
        return top + plot_h * (1.0 - (value - y_min) / (y_max - y_min))

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        f'<text x="{width / 2}" y="28" text-anchor="middle" font-family="sans-serif" '
        f'font-size="16">{escape(title)}</text>',
    ]
    for tick in range(6):
        value = y_min + (y_max - y_min) * tick / 5
        y = y_pos(value)
        parts.append(
            f'<line x1="{left}" y1="{y:.1f}" x2="{width - right}" y2="{y:.1f}" '
            'stroke="#e5e7eb" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{left - 8}" y="{y + 4:.1f}" text-anchor="end" '
            f'font-family="sans-serif" font-size="11">{value:.1f}{escape(y_suffix)}</text>'
        )
    if y_min < 0 < y_max:
        zero_y = y_pos(0.0)
        parts.append(
            f'<line x1="{left}" y1="{zero_y:.1f}" x2="{width - right}" y2="{zero_y:.1f}" '
            'stroke="#6b7280" stroke-width="1.5"/>'
        )
    parts.append(
        f'<line x1="{left}" y1="{top + plot_h}" x2="{width - right}" '
        f'y2="{top + plot_h}" stroke="#374151" stroke-width="1"/>'
    )
    for index, label in enumerate(x_labels):
        parts.append(
            f'<text x="{x_pos(index):.1f}" y="{top + plot_h + 22}" text-anchor="middle" '
            f'font-family="sans-serif" font-size="12">{escape(label)}</text>'
        )
    parts.append(
        f'<text x="{left + plot_w / 2}" y="{height - 12}" text-anchor="middle" '
        f'font-family="sans-serif" font-size="12">{escape(y_label)}</text>'
    )

    legend_y = top - 14
    legend_x = left
    for name, color, values in series:
        points = " ".join(
            f"{x_pos(index):.1f},{y_pos(value):.1f}" for index, value in enumerate(values)
        )
        parts.append(
            f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="2.5"/>'
        )
        for index, value in enumerate(values):
            parts.append(
                f'<circle cx="{x_pos(index):.1f}" cy="{y_pos(value):.1f}" r="4" fill="{color}"/>'
            )
            parts.append(
                f'<text x="{x_pos(index):.1f}" y="{y_pos(value) - 9:.1f}" '
                f'text-anchor="middle" font-family="sans-serif" font-size="10">'
                f"{value:.1f}{escape(y_suffix)}</text>"
            )
        parts.append(
            f'<line x1="{legend_x}" y1="{legend_y}" x2="{legend_x + 22}" y2="{legend_y}" '
            f'stroke="{color}" stroke-width="3"/>'
        )
        parts.append(
            f'<text x="{legend_x + 28}" y="{legend_y + 4}" font-family="sans-serif" '
            f'font-size="11">{escape(name)}</text>'
        )
        legend_x += 190
    parts.append("</svg>")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(parts), encoding="utf-8")


def write_plots(
    e2_offline: list[dict],
    e2_live_pairs: list[dict],
    e3_rows: list[dict],
) -> None:
    by_label: dict[str, dict[str, float]] = defaultdict(
        lambda: {"prompt0": 0.0, "prompt1": 0.0, "miss0": 0.0, "miss1": 0.0, "ratio": 0.0}
    )
    for pair in e2_live_pairs:
        bucket = by_label[pair["duplicate_label"]]
        bucket["prompt0"] += pair["t0"]["prompt"]
        bucket["prompt1"] += pair["t1"]["prompt"]
        bucket["miss0"] += pair["t0"]["miss"]
        bucket["miss1"] += pair["t1"]["miss"]
        bucket["ratio"] = pair["duplicate_ratio"]
    labels = [label for label in ("D0", "D2", "D3") if label in by_label]
    prompt_reductions = [
        (by_label[label]["prompt0"] - by_label[label]["prompt1"]) / by_label[label]["prompt0"] * 100
        for label in labels
    ]
    miss_reductions = [
        (by_label[label]["miss0"] - by_label[label]["miss1"]) / by_label[label]["miss0"] * 100
        for label in labels
    ]
    _svg_line_chart(
        FIGURES / "e2_duplicate_ratio_vs_reduction.svg",
        title="Stage 6 E2: exact duplicate ratio vs reduction (live, all scales)",
        x_labels=[f"{label} ({by_label[label]['ratio']:.2f})" for label in labels],
        series=[
            ("prompt reduction", "#2563eb", prompt_reductions),
            ("cache-miss reduction", "#dc2626", miss_reductions),
        ],
        y_label="reduction (%)",
        y_suffix="%",
    )

    by_length: dict[int, dict[str, dict]] = defaultdict(dict)
    for row in e3_rows:
        if row["experiment"] == "E3":
            by_length[row["chain_length"]][row["policy"]] = row
    lengths = sorted(by_length)
    _svg_line_chart(
        FIGURES / "e3_chain_length_vs_handoff.svg",
        title="Stage 6 E3: chain length vs logical handoff refs",
        x_labels=[f"P{length + 1} (len {length})" for length in lengths],
        series=[
            (
                "S1 capability_only",
                "#6b7280",
                [
                    by_length[length]["capability_only"]["total_logical_handoff_refs"]
                    for length in lengths
                ],
            ),
            (
                "S2 context_locality",
                "#059669",
                [
                    by_length[length]["context_locality"]["total_logical_handoff_refs"]
                    for length in lengths
                ],
            ),
        ],
        y_label="logical handoff refs",
    )

    latency_by_scale: dict[str, list[float]] = defaultdict(list)
    for row in e2_offline:
        if row["transform_name"] == "contextpilot_dedup":
            latency_by_scale[row["scale"]].append(row["latency_mean_ms"])
    scales = ["small", "medium", "large"]
    _svg_line_chart(
        FIGURES / "e2_transform_latency_vs_scale.svg",
        title="Stage 6 E2: ContextPilot dedup latency vs context scale",
        x_labels=scales,
        series=[
            (
                "mean transform latency",
                "#7c3aed",
                [statistics.fmean(latency_by_scale[scale]) for scale in scales],
            )
        ],
        y_label="milliseconds",
        y_suffix=" ms",
    )


def main() -> None:
    e1 = _rows("stage6-e1.jsonl")
    e2_offline = _rows("stage6-e2-offline.jsonl")
    e2_live = _rows("stage6-e2-live.jsonl")
    e3 = _rows("stage6-e3.jsonl")
    e4 = _rows("stage6-e4-live.jsonl")

    e2_live_table_text, pairs = e2_live_table(e2_live)
    e5_table_text, e5_summary = e5_records()

    REPORT = RESULTS / "stage6-tables.md"
    report = "\n\n".join(
        [
            "# Stage 6 Generated Tables",
            "## Table A - Stable layout",
            e1_table(e1),
            "## Table B - Dedup scaling (offline)",
            e2_offline_table(e2_offline),
            "## Table B live - Dedup scaling (DeepSeek)",
            e2_live_table_text,
            "## Table C - Placement scaling",
            e3_table(e3),
            "## Table D - Placement x dedup",
            e4_table(e4),
            "## Table E - Recovery",
            e5_table_text,
        ]
    )
    REPORT.write_text(report + "\n", encoding="utf-8")
    (RUNS / "stage6-e5.json").write_text(json.dumps(e5_summary, indent=2), encoding="utf-8")
    write_plots(e2_offline, pairs, e3)
    print(report)


if __name__ == "__main__":
    main()
