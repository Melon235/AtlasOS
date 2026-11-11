"""Stage G3-2: O0 (no ingress offload) vs O1 (ingress offload), read from run artifacts.

Consumes one Stage G3-2 round (``experiments/results/stage-g3-2/<round>``): the round manifest for
the arm mapping plus, per run, ``result.json``, ``provider_calls.jsonl``,
``tool_conversations.json`` and ``tool_calls``. It never calls a model and never writes into a run
directory.

Outputs (``<round>/analysis/``): ``runs.csv``, ``turns.csv``, ``offloads.csv``, ``summary.json``.
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from experiments.evaluation.stage_g2b_diagnosis import conversations

REPO_ROOT = Path(__file__).resolve().parents[2]
RESULTS_ROOT = REPO_ROOT / "experiments" / "results" / "stage-g3-2"
HISTORY_READ_TOOL = "history.read"
CONTROL_TOOLS = frozenset({"tool.search", "tool_search", "dispatch_action"})


def latest_round() -> Path:
    rounds = sorted(path for path in RESULTS_ROOT.glob("g32-*") if path.is_dir())
    if not rounds:
        raise SystemExit(f"no Stage G3-2 round under {RESULTS_ROOT}")
    return rounds[-1]


def _load(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _load_calls(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            records.append(payload)
    return records


def arm_runs(round_dir: Path) -> list[tuple[str, int, str, Path]]:
    manifest = _load(round_dir / "manifest.json") or {}
    arms: list[tuple[str, int, str, Path]] = []
    for pair in manifest.get("pairs") or []:
        for suffix, result in (pair.get("runs") or {}).items():
            run_dir = result.get("run_dir")
            if not run_dir or not Path(run_dir).is_dir():
                continue
            arms.append(
                (
                    str(pair.get("definition")),
                    int(pair.get("repetition") or 0),
                    str(result.get("tool_result_mode") or suffix),
                    Path(run_dir),
                )
            )
    arms.sort(key=lambda item: (item[0], item[1], item[2]))
    return arms


@dataclass
class RunRow:
    run_id: str
    sample_id: str
    repetition: int
    mode: str
    success: bool
    failure_class: str | None
    oracle_passed: bool | None
    workers: int
    worker_success: int
    side_effect_unknown_workers: int
    max_turns_workers: int
    prompt_tokens: int
    completion_tokens: int
    cache_hit_tokens: int | None
    cache_miss_tokens: int | None
    cache_hit_ratio: float | None
    cache_hit_ratio_after_first: float | None
    model_turns: int
    tool_calls: int
    cost: float | None
    latency_ms: float
    budget_exceeded: bool
    admission_rejections: int
    offloads: int
    archived_chars: int
    placeholder_chars: int
    archive_failures: int
    deduplicated: int
    error_results_offloaded: int
    skipped_already_offloaded: int
    history_read_calls: int
    history_read_scan_calls: int
    dispatch_read_calls: int
    history_archives_on_disk: int
    offloaded_tool_calls: int
    run_dir: str = ""
    turns: list[dict[str, Any]] = field(default_factory=list)


def _cache_ratio(calls: list[dict[str, Any]]) -> tuple[float | None, float | None]:
    def _ratio(rows: list[dict[str, Any]]) -> float | None:
        hit = sum(
            int(row["cache_hit_tokens"]) for row in rows if _is_int(row.get("cache_hit_tokens"))
        )
        miss = sum(
            int(row["cache_miss_tokens"]) for row in rows if _is_int(row.get("cache_miss_tokens"))
        )
        return hit / (hit + miss) if (hit + miss) > 0 else None

    if not calls:
        return None, None
    return _ratio(calls), _ratio(calls[1:])


def _is_int(value: Any) -> bool:
    return isinstance(value, int)


def _tool_use_names(messages: list[dict[str, Any]]) -> dict[str, str]:
    names: dict[str, str] = {}
    for message in messages:
        for block in message.get("content") or []:
            if block.get("type") == "tool_use":
                names[str(block.get("id"))] = str(block.get("name"))
    return names


def run_row(run_dir: Path, sample_id: str, repetition: int, mode: str) -> RunRow:
    result = _load(run_dir / "result.json") or {}
    identity = result.get("identity") or {}
    metrics = result.get("metrics") or {}
    # ``EvalMetrics.extra`` is flattened into the top-level metrics object in ``result.json``.
    extra = metrics.get("extra") or metrics
    oracle = result.get("oracle") or {}
    workers = result.get("worker_results") or {}
    tool_calls = result.get("tool_calls") or []
    ingress = extra.get("tool_result") or {}
    compaction = extra.get("compaction") or {}
    budget = extra.get("budget") or {}
    calls = _load_calls(run_dir / "provider_calls.jsonl")

    dispatch_read = sum(
        1
        for call in tool_calls
        if call.get("tool_name") == "dispatch_action"
        and str((call.get("input") or {}).get("tool_id")) == HISTORY_READ_TOOL
    )
    direct_read = sum(1 for call in tool_calls if call.get("tool_name") == HISTORY_READ_TOOL)
    offloaded_tool_calls = sum(
        1 for call in tool_calls if "[TOOL_RESULT_OFFLOADED]" in str(call.get("output") or "")
    )
    history_dir = run_dir / "history"
    archives = len(list(history_dir.glob("*.txt"))) if history_dir.is_dir() else 0
    ratio, ratio_after = _cache_ratio(calls)

    events: list[dict[str, Any]] = []
    for task_key, messages in conversations(run_dir).items():
        names = _tool_use_names(messages)
        turn = 0
        for message in messages:
            if message.get("role") == "assistant":
                turn += 1
                continue
            for block in message.get("content") or []:
                if block.get("type") != "tool_result":
                    continue
                tool = names.get(str(block.get("tool_use_id")), "?")
                content = block.get("content")
                text = content if isinstance(content, str) else json.dumps(content, default=str)
                events.append(
                    {
                        "task": task_key,
                        "turn": turn,
                        "tool": tool,
                        "result_chars": len(text),
                        "offloaded": "[TOOL_RESULT_OFFLOADED]" in text,
                    }
                )

    return RunRow(
        run_id=str(identity.get("run_id") or run_dir.name),
        sample_id=sample_id,
        repetition=repetition,
        mode=mode,
        success=bool(result.get("success")),
        failure_class=result.get("failure_class"),
        oracle_passed=oracle.get("passed"),
        workers=len(workers),
        worker_success=sum(1 for info in workers.values() if info.get("success")),
        side_effect_unknown_workers=sum(
            1
            for info in workers.values()
            if (info.get("stop_reason") or {}).get("code") == "side_effect_unknown"
        ),
        max_turns_workers=sum(
            1
            for info in workers.values()
            if (info.get("stop_reason") or {}).get("code") == "max_turns"
        ),
        prompt_tokens=int(metrics.get("prompt_tokens") or 0),
        completion_tokens=int(metrics.get("completion_tokens") or 0),
        cache_hit_tokens=metrics.get("cache_hit_tokens"),
        cache_miss_tokens=metrics.get("cache_miss_tokens"),
        cache_hit_ratio=ratio,
        cache_hit_ratio_after_first=ratio_after,
        model_turns=int(metrics.get("model_turns") or 0),
        tool_calls=int(metrics.get("tool_calls") or 0),
        cost=metrics.get("estimated_cost_usd"),
        latency_ms=float(metrics.get("latency_ms") or 0.0),
        budget_exceeded=str(result.get("failure_class") or "") == "BUDGET_EXCEEDED",
        admission_rejections=int(budget.get("admission_rejections") or 0),
        offloads=int(ingress.get("offloads") or 0),
        archived_chars=int(ingress.get("original_chars_archived") or 0),
        placeholder_chars=int(ingress.get("placeholder_chars") or 0),
        archive_failures=int(ingress.get("archive_failures") or 0),
        deduplicated=int(ingress.get("deduplicated") or 0),
        error_results_offloaded=int(ingress.get("error_results_offloaded") or 0),
        skipped_already_offloaded=int(ingress.get("skipped_already_offloaded") or 0),
        history_read_calls=direct_read + dispatch_read,
        history_read_scan_calls=int(compaction.get("read_calls") or 0),
        dispatch_read_calls=dispatch_read,
        history_archives_on_disk=archives,
        offloaded_tool_calls=offloaded_tool_calls,
        run_dir=str(run_dir),
        turns=events,
    )


def estimate_savings(round_dir: Path, rows: list[RunRow]) -> dict[str, Any]:
    """ESTIMATED counterfactual: prompt characters avoided by offloading.

    For every offloaded block, the content the model would have re-sent on each *later* turn of the
    same worker is replaced by the placeholder. Characters are converted with the repository's
    tokenizer so the number stays comparable with the measured prompt totals.
    """

    from anycode.context.tokenizer import select_tokenizer
    from experiments.evaluation.stage_g2b_diagnosis import PromptCounter

    counter = PromptCounter(select_tokenizer("tiktoken", model="deepseek-chat"))
    saved_chars = 0
    saved_tokens = 0
    per_run: list[dict[str, Any]] = []
    for sample, repetition, mode, run_dir in arm_runs(round_dir):
        if mode != "ingress_offload":
            continue
        run_saved_chars = 0
        run_saved_tokens = 0
        for _task_key, messages in conversations(run_dir).items():
            turns = [
                index for index, item in enumerate(messages) if item.get("role") == "assistant"
            ]
            total_turns = len(turns)
            turn_index_of: dict[int, int] = {}
            for ordinal, index in enumerate(turns, start=1):
                turn_index_of[index] = ordinal
            for index, message in enumerate(messages):
                for block in message.get("content") or []:
                    if block.get("type") != "tool_result":
                        continue
                    text = str(block.get("content") or "")
                    if "[TOOL_RESULT_OFFLOADED]" not in text:
                        continue
                    original = _original_size(text)
                    remaining = total_turns - _current_turn(index, turns)
                    if original is None or remaining <= 0:
                        continue
                    delta_chars = max(0, original - len(text))
                    run_saved_chars += delta_chars * remaining
                    run_saved_tokens += counter.count("x" * delta_chars) * remaining
        saved_chars += run_saved_chars
        saved_tokens += run_saved_tokens
        per_run.append(
            {
                "run_id": next(
                    (row.run_id for row in rows if str(row.run_dir) == str(run_dir)),
                    run_dir.name,
                ),
                "sample_id": sample,
                "repetition": repetition,
                "estimated_saved_chars": run_saved_chars,
                "estimated_saved_tokens": run_saved_tokens,
            }
        )
    return {
        "data_type": "ESTIMATED (placeholder vs original chars x remaining turns of the worker)",
        "estimated_saved_prompt_tokens": saved_tokens,
        "estimated_saved_prompt_chars": saved_chars,
        "per_run": per_run,
    }


def _original_size(placeholder: str) -> int | None:
    for line in placeholder.splitlines():
        if line.startswith("Original size: "):
            return int(line.split()[2])
    return None


def _current_turn(index: int, turns: list[int]) -> int:
    return sum(1 for turn in turns if turn < index)


def _mean(values: list[float | int | None]) -> float | None:
    clean = [float(value) for value in values if value is not None]
    return round(sum(clean) / len(clean), 6) if clean else None


def aggregate(rows: list[RunRow]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for mode in ("off", "ingress_offload"):
        selected = [row for row in rows if row.mode == mode]
        hits = [row.cache_hit_tokens for row in selected]
        misses = [row.cache_miss_tokens for row in selected]
        complete = all(value is not None for value in hits + misses)
        total_hit = sum(int(value) for value in hits if value is not None) if complete else None
        total_miss = sum(int(value) for value in misses if value is not None) if complete else None
        ratios = [row.cache_hit_ratio for row in selected if row.cache_hit_ratio is not None]
        post_first = [
            row.cache_hit_ratio_after_first
            for row in selected
            if row.cache_hit_ratio_after_first is not None
        ]
        turns = [row.model_turns for row in selected if row.model_turns]
        successful = [row for row in selected if row.success]
        summary[mode] = {
            "runs": len(selected),
            "oracle_pass": sum(1 for row in selected if row.oracle_passed),
            "task_success": sum(1 for row in selected if row.failure_class == "TASK_SUCCESS"),
            "prompt_tokens": sum(row.prompt_tokens for row in selected),
            "completion_tokens": sum(row.completion_tokens for row in selected),
            "cache_hit_tokens": total_hit,
            "cache_miss_tokens": total_miss,
            "cache_hit_ratio": (
                round(total_hit / (total_hit + total_miss), 4)
                if total_hit is not None and total_miss is not None and total_hit + total_miss
                else None
            ),
            "median_run_cache_hit_ratio": round(statistics.median(ratios), 4) if ratios else None,
            "median_run_cache_ratio_after_first": (
                round(statistics.median(post_first), 4) if post_first else None
            ),
            "cost_usd": round(sum(row.cost or 0.0 for row in selected), 6),
            "model_turns": sum(row.model_turns for row in selected),
            # ``metrics.tool_calls`` loses the records of workers killed by a budget rejection;
            # the transcript count is the reliable measure and is reported alongside it.
            "tool_calls": sum(row.tool_calls for row in selected),
            "tool_calls_from_transcript": sum(len(row.turns) for row in selected),
            "workers": sum(row.workers for row in selected),
            "worker_success": sum(row.worker_success for row in selected),
            "budget_exceeded": sum(1 for row in selected if row.budget_exceeded),
            "admission_rejections": sum(row.admission_rejections for row in selected),
            "side_effect_unknown_workers": sum(row.side_effect_unknown_workers for row in selected),
            "max_turns_workers": sum(row.max_turns_workers for row in selected),
            "offloaded_results": sum(row.offloads for row in selected),
            "offloaded_tool_calls": sum(row.offloaded_tool_calls for row in selected),
            "archived_chars": sum(row.archived_chars for row in selected),
            "placeholder_chars": sum(row.placeholder_chars for row in selected),
            "archive_failures": sum(row.archive_failures for row in selected),
            "duplicate_archives": sum(row.deduplicated for row in selected),
            "error_results_offloaded": sum(row.error_results_offloaded for row in selected),
            "history_read_calls": sum(row.history_read_calls for row in selected),
            "history_archives_on_disk": sum(row.history_archives_on_disk for row in selected),
            "avg_prompt_per_turn": (
                round(sum(row.prompt_tokens for row in selected) / sum(turns), 1) if turns else None
            ),
            "avg_cost_successful_run": _mean([row.cost for row in successful]),
            "avg_cost_all_runs": _mean([row.cost for row in selected]),
        }
    summary["pairs"] = _paired(rows)
    return summary


def _paired(rows: list[RunRow]) -> list[dict[str, Any]]:
    by_key: dict[tuple[str, int], dict[str, RunRow]] = {}
    for row in rows:
        by_key.setdefault((row.sample_id, row.repetition), {})[row.mode] = row
    pairs: list[dict[str, Any]] = []
    for (sample_id, repetition), arms in sorted(by_key.items()):
        off = arms.get("off")
        ingress = arms.get("ingress_offload")
        if off is None or ingress is None:
            continue
        pairs.append(
            {
                "sample_id": sample_id,
                "repetition": repetition,
                "off_run": off.run_id,
                "ingress_run": ingress.run_id,
                "off_oracle": off.oracle_passed,
                "ingress_oracle": ingress.oracle_passed,
                "prompt_delta": ingress.prompt_tokens - off.prompt_tokens,
                "completion_delta": ingress.completion_tokens - off.completion_tokens,
                "cost_delta": round((ingress.cost or 0.0) - (off.cost or 0.0), 6),
                "turns_delta": ingress.model_turns - off.model_turns,
                "tool_calls_delta": ingress.tool_calls - off.tool_calls,
                "cache_ratio_delta": (
                    round(ingress.cache_hit_ratio - off.cache_hit_ratio, 4)
                    if ingress.cache_hit_ratio is not None and off.cache_hit_ratio is not None
                    else None
                ),
            }
        )
    return pairs


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
    parser = argparse.ArgumentParser(prog="atlas-stage-g3-2-analysis")
    parser.add_argument("--round", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)

    round_dir: Path = args.round or latest_round()
    output: Path = args.output or (round_dir / "analysis")
    output.mkdir(parents=True, exist_ok=True)

    rows = [
        run_row(run_dir, sample, rep, mode) for sample, rep, mode, run_dir in arm_runs(round_dir)
    ]
    write_csv(
        output / "runs.csv",
        [{key: value for key, value in row.__dict__.items() if key != "turns"} for row in rows],
    )
    turn_rows: list[dict[str, Any]] = []
    for row in rows:
        for event in row.turns:
            turn_rows.append(
                {
                    "run_id": row.run_id,
                    "mode": row.mode,
                    "sample_id": row.sample_id,
                    "repetition": row.repetition,
                    **event,
                }
            )
    write_csv(output / "turns.csv", turn_rows)
    write_csv(
        output / "offloads.csv",
        [
            {
                "run_id": row.run_id,
                "mode": row.mode,
                "sample_id": row.sample_id,
                "repetition": row.repetition,
                "offloads": row.offloads,
                "archived_chars": row.archived_chars,
                "placeholder_chars": row.placeholder_chars,
                "archive_failures": row.archive_failures,
                "duplicate_archives": row.deduplicated,
                "error_results_offloaded": row.error_results_offloaded,
                "history_read_calls": row.history_read_calls,
                "history_archives_on_disk": row.history_archives_on_disk,
            }
            for row in rows
        ],
    )

    summary = aggregate(rows)
    summary["ingress_savings"] = estimate_savings(round_dir, rows)
    summary["round"] = str(round_dir)
    summary["tool_names_seen"] = dict(Counter(event["tool"] for row in rows for event in row.turns))
    (output / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8"
    )

    print(json.dumps(summary, indent=2, sort_keys=True))
    for row in rows:
        print(
            f"{row.sample_id} rep={row.repetition} {row.mode:15s} oracle={row.oracle_passed} "
            f"class={row.failure_class} prompt={row.prompt_tokens} hit={row.cache_hit_tokens} "
            f"miss={row.cache_miss_tokens} ratio={row.cache_hit_ratio} turns={row.model_turns} "
            f"offloads={row.offloads} reads={row.history_read_calls} cost={row.cost}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
