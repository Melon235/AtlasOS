"""Stage G2b-0: read-only diagnosis of multi-worker token / context cost.

Reads existing Stage E1 / G1 / G2a artifacts and writes derived tables under
``experiments/results/stage-g2b-diagnosis/``. It never calls a model, never starts a sandbox and
never writes into an existing run directory.

Data fidelity is tracked explicitly per metric:

``OBSERVED``      provider-reported usage or real execution events (``result.json`` metrics,
                  ``worker_results.*.token_usage``, ``tool_calls``, stop reasons, oracle verdicts).
``RECONSTRUCTED`` derived deterministically from the persisted turn-boundary conversation
                  snapshot (``tool_conversations.json``), which is append-only per worker.
``ESTIMATED``     tokenizer approximation of reconstructed content (``tiktoken`` cl100k via the
                  repository tokenizer selection); the per-turn provider token count is not
                  persisted, so per-turn sizes are estimated and anchored on the observed run total.
``UNAVAILABLE``   not derivable from the stored artifacts.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from anycode.context.tokenizer import select_tokenizer

REPO_ROOT = Path(__file__).resolve().parents[2]
RESULTS_ROOT = REPO_ROOT / "experiments" / "results"
OUTPUT_ROOT = RESULTS_ROOT / "stage-g2b-diagnosis"

ROUNDS: dict[str, Path] = {
    "e1-formal": RESULTS_ROOT / "stage-e1" / "e1-20261109T102030Z",
    "e1-smoke": RESULTS_ROOT / "stage-e1" / "e1-20261109T101141Z",
    "g1": RESULTS_ROOT / "stage-g1" / "g1-20261110T103003Z",
    "g2a": RESULTS_ROOT / "stage-g2a" / "g2a-20261110T112744Z",
}

FOCUS_TASKS = ("e1-02-report", "e1-07-export", "e1-12-conflict")
FILE_TOOLS = ("file_read", "list_files", "file_write", "file_edit")
_ROOT = "/workspace/project"
_PATH_IN_BASH = re.compile(
    r"(?:/workspace/project/)?([A-Za-z0-9_./-]+\.(?:py|md|txt|json|yaml|yml|cfg|toml))"
)
_HANDOFF_MARKERS = (
    "Relevant output from prerequisite tasks:",
    "Upstream handoff",
)


# ----------------------------------------------------------------------------- io helpers


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def run_dirs(round_dir: Path) -> list[Path]:
    if not round_dir.is_dir():
        return []
    # runs/<benchmark_id>/<sample_id>/<run_dir>/result.json
    return sorted({path.parent for path in (round_dir / "runs").glob("*/*/*/result.json")})


def conversations(run_dir: Path) -> dict[str, list[dict[str, Any]]]:
    payload = _load_json(run_dir / "tool_conversations.json") or {}
    tasks = payload.get("tasks") or {}
    return {
        str(name): list(entry.get("messages") or [])
        for name, entry in tasks.items()
        if isinstance(entry, dict)
    }


# ----------------------------------------------------------------------------- inventory


def inventory() -> dict[str, Any]:
    report: dict[str, Any] = {"rounds": {}, "fidelity": {}}
    for name, path in ROUNDS.items():
        runs = run_dirs(path)
        counts: Counter[str] = Counter()
        for run in runs:
            counts["result_json"] += (run / "result.json").is_file()
            counts["run_json"] += (run / "run.json").is_file()
            counts["conversations"] += (run / "tool_conversations.json").is_file()
            counts["workers_json"] += (run / "workers.json").is_file()
            counts["trajectory"] += any(run.glob("trajectory/*/transcript.jsonl"))
            counts["oracle_log"] += (run / "oracle.log").is_file()
        sample = _load_json(runs[0] / "result.json") if runs else None
        worker = next(iter((sample or {}).get("worker_results", {}).values()), {}) if sample else {}
        report["rounds"][name] = {
            "path": str(path),
            "exists": path.is_dir(),
            "runs": len(runs),
            "artifacts": dict(counts),
            "result_fields": sorted(sample) if sample else [],
            "worker_result_fields": sorted(worker),
            "metrics_fields": sorted((sample or {}).get("metrics", {})),
            "has_per_turn_usage": False,
            "has_turn_snapshots": counts["conversations"] > 0,
        }
    report["fidelity"] = {
        "run_prompt_completion_cache_tokens": "OBSERVED (provider usage summed by the runner)",
        "worker_input_output_tokens": (
            "OBSERVED but cache-hit tokens are dropped by AnyCode merge_usage, so per-worker "
            "prompt totals are NOT recoverable"
        ),
        "per_turn_prompt_tokens": "UNAVAILABLE (no per-call usage persisted)",
        "turn_boundary_messages": "RECONSTRUCTED from tool_conversations.json (append-only)",
        "per_turn_token_sizes": "ESTIMATED (tiktoken) anchored on the OBSERVED run total",
        "tool_call_args_and_outputs": "OBSERVED (result.json tool_calls, output truncated to 4000)",
        "tool_result_sizes_in_prompt": "RECONSTRUCTED from the conversation snapshot",
        "stop_reasons": "OBSERVED (worker_results[*].stop_reason)",
        "budget_ledger": "OBSERVED (G2a runs only)",
        "handoff_tokens": "RECONSTRUCTED (dependency section inside the first user message)",
        "system_prompt_and_tool_schema_tokens": (
            "ESTIMATED (residual after anchoring; cross-checked with a direct tokenizer count)"
        ),
    }
    return report


# ----------------------------------------------------------------------------- prompt parts


def _block_text(block: dict[str, Any]) -> str:
    kind = block.get("type")
    if kind == "text":
        return str(block.get("text") or "")
    if kind == "tool_use":
        return f"{block.get('name')}{json.dumps(block.get('input'), sort_keys=True, default=str)}"
    if kind == "tool_result":
        content = block.get("content")
        if isinstance(content, str):
            return content
        return json.dumps(content, default=str)
    return json.dumps(block, default=str)


def split_first_message(text: str) -> tuple[str, str]:
    """Split the first user message into (task description, handoff section)."""

    for marker in _HANDOFF_MARKERS:
        index = text.find(marker)
        if index >= 0:
            return text[:index], text[index:]
    return text, ""


class PromptCounter:
    """Token estimation for reconstructed prompt parts, cached per distinct text."""

    def __init__(self, tokenizer: Any) -> None:
        self._tokenizer = tokenizer
        self._cache: dict[str, int] = {}

    def count(self, text: str) -> int:
        if not text:
            return 0
        cached = self._cache.get(text)
        if cached is None:
            cached = self._tokenizer.count_text(text)
            if len(self._cache) < 200_000:
                self._cache[text] = cached
        return cached

    def block(self, block: dict[str, Any]) -> int:
        return self.count(_block_text(block))


def turn_prompts(messages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Prompts actually sent, one per model call.

    ``ConversationSnapshotHook.before_turn`` stores the conversation *before* each model call, so
    the saved list is exactly the last prompt. Earlier prompts are its prefixes. The snapshot is
    append-only per worker (no compaction is enabled), which makes prefix reconstruction exact
    for prompt *content*; only the token counts are estimated.
    """

    prompts: list[list[dict[str, Any]]] = []
    for index, message in enumerate(messages):
        if message.get("role") == "assistant":
            prompts.append(messages[:index])
    if messages and messages[-1].get("role") != "assistant":
        prompts.append(messages)
    return prompts


def turn_layers(prompt: list[dict[str, Any]], counter: PromptCounter) -> dict[str, int]:
    """ESTIMATED token split of one reconstructed prompt."""

    layers: dict[str, int] = {
        "task": 0,
        "handoff": 0,
        "assistant": 0,
        "tool_results": 0,
        "other": 0,
    }
    for index, message in enumerate(prompt):
        role = message.get("role")
        for block in message.get("content") or []:
            kind = block.get("type")
            if index == 0 and role == "user" and kind == "text":
                task, handoff = split_first_message(str(block.get("text") or ""))
                layers["task"] += counter.count(task)
                layers["handoff"] += counter.count(handoff)
            elif kind == "tool_result":
                layers["tool_results"] += counter.block(block)
            elif role == "assistant":
                layers["assistant"] += counter.block(block)
            else:
                layers["other"] += counter.block(block)
    return layers


# ----------------------------------------------------------------------------- per turn


def per_turn(
    run: Path, result: dict[str, Any], counter: PromptCounter
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    observed_prompt = int(result["metrics"].get("prompt_tokens") or 0)
    observed_turns = int(result["metrics"].get("model_turns") or 0)
    rows: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []
    reconstructed: list[tuple[dict[str, Any], int, dict[str, int]]] = []
    for task_key, messages in conversations(run).items():
        prompts = turn_prompts(messages)
        for turn_index, prompt in enumerate(prompts, start=1):
            layers = turn_layers(prompt, counter)
            total = sum(layers.values())
            reconstructed.append(
                (
                    {
                        "run_id": result["identity"]["run_id"],
                        "sample_id": result["sample_id"],
                        "variant": "a0" if result["sample_id"].endswith("-a0") else "a1",
                        "task_key": task_key,
                        "turn_index": turn_index,
                    },
                    total,
                    layers,
                )
            )
    total_reconstructed = sum(item[1] for item in reconstructed)
    turns = len(reconstructed)
    overhead_per_turn = (
        (observed_prompt - total_reconstructed) / turns if turns and observed_prompt else 0.0
    )
    cumulative: dict[tuple[str, str], float] = defaultdict(float)
    for meta, total, layers in reconstructed:
        key = (meta["run_id"], meta["task_key"])
        cumulative[key] += total + overhead_per_turn
        anchored = total + overhead_per_turn
        rows.append(
            {
                **meta,
                "reconstructed_tokens": round(total, 1),
                "anchored_prompt_tokens": round(anchored, 1),
                "cumulative_anchored_tokens": round(cumulative[key], 1),
                "task_tokens": layers["task"],
                "handoff_tokens": layers["handoff"],
                "assistant_tokens": layers["assistant"],
                "tool_result_tokens": layers["tool_results"],
                "other_tokens": layers["other"],
                "fixed_overhead_tokens": round(overhead_per_turn, 1),
            }
        )
    by_task = defaultdict(list)
    for meta, total, _layers in reconstructed:
        by_task[(meta["run_id"], meta["task_key"])].append(total)
    for (run_id, task_key), values in sorted(by_task.items()):
        summaries.append(
            {
                "run_id": run_id,
                "task_key": task_key,
                "turns": len(values),
                "first_turn_tokens": round(values[0] + overhead_per_turn, 1),
                "last_turn_tokens": round(values[-1] + overhead_per_turn, 1),
                "max_turn_tokens": round(max(values) + overhead_per_turn, 1),
                "growth_ratio": round(values[-1] / values[0], 3) if values[0] else None,
            }
        )
    diagnostics = [
        {
            "run_id": result["identity"]["run_id"],
            "sample_id": result["sample_id"],
            "variant": "a0" if result["sample_id"].endswith("-a0") else "a1",
            "observed_prompt_tokens": observed_prompt,
            "observed_model_turns": observed_turns,
            "reconstructed_turns": turns,
            "reconstructed_prompt_tokens": round(total_reconstructed, 1),
            "overhead_per_turn": round(overhead_per_turn, 1),
            "overhead_share": round(overhead_per_turn * turns / observed_prompt, 4)
            if observed_prompt
            else None,
            "reconstruction_error": round(
                total_reconstructed + overhead_per_turn * turns - observed_prompt, 1
            )
            if turns
            else None,
        }
    ]
    return rows, summaries, diagnostics


# ----------------------------------------------------------------------------- repeats


def _normalize_path(value: str) -> str:
    text = value.strip()
    if text.startswith(_ROOT):
        text = text[len(_ROOT) :]
    return text.lstrip("./") or "."


def _tool_target(call: dict[str, Any]) -> tuple[str, str] | None:
    name = str(call.get("tool_name"))
    payload = call.get("input") or {}
    if name in ("file_read", "file_write", "file_edit", "list_files"):
        path = payload.get("path") or payload.get("file_path")
        return (name, _normalize_path(str(path))) if path else None
    if name == "bash":
        command = str(payload.get("command") or "")
        return ("bash", " ".join(command.split())) if command else None
    return None


def _bash_paths(call: dict[str, Any]) -> set[str]:
    command = str((call.get("input") or {}).get("command") or "")
    return {
        _normalize_path(match)
        for match in _PATH_IN_BASH.findall(command)
        if not match.startswith(".git/")
    }


def repeats(run: Path, result: dict[str, Any]) -> list[dict[str, Any]]:
    """Repeat-investigation counters built only from stored tool call arguments."""

    del run
    calls = result.get("tool_calls") or []
    per_worker: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for call in calls:
        per_worker[str(call.get("agent"))].append(call)

    writes: dict[str, int] = defaultdict(int)
    for call in calls:
        target = _tool_target(call)
        if call.get("tool_name") in ("file_write", "file_edit") and target:
            writes[target[1]] += 1

    target_workers: dict[tuple[str, str], set[str]] = defaultdict(set)
    for call in calls:
        target = _tool_target(call)
        if target:
            target_workers[target].add(str(call.get("agent")))

    rows: list[dict[str, Any]] = []
    for worker, worker_calls in per_worker.items():
        targets: Counter[tuple[str, str]] = Counter()
        for call in worker_calls:
            target = _tool_target(call)
            if target:
                targets[target] += 1
        for (tool, target), count in targets.items():
            if count < 2:
                continue
            rows.append(
                {
                    "run_id": result["identity"]["run_id"],
                    "sample_id": result["sample_id"],
                    "kind": "EXACT_REPEAT",
                    "tool": tool,
                    "target": target,
                    "workers": worker,
                    "count": count,
                    "extra_calls": count - 1,
                    "write_before_repeat": writes.get(target, 0) > 0,
                }
            )
    for (tool, target), workers in target_workers.items():
        if len(workers) < 2:
            continue
        rows.append(
            {
                "run_id": result["identity"]["run_id"],
                "sample_id": result["sample_id"],
                "kind": "CROSS_WORKER_REPEAT",
                "tool": tool,
                "target": target,
                "workers": ",".join(sorted(workers)),
                "count": len(workers),
                "extra_calls": len(workers) - 1,
                "write_before_repeat": writes.get(target, 0) > 0,
            }
        )
    bash_overlap: dict[str, set[str]] = defaultdict(set)
    for call in calls:
        if call.get("tool_name") != "bash":
            continue
        for path in _bash_paths(call):
            bash_overlap[path].add(str(call.get("agent")))
    shared = {path: workers for path, workers in bash_overlap.items() if len(workers) > 1}
    rows.append(
        {
            "run_id": result["identity"]["run_id"],
            "sample_id": result["sample_id"],
            "kind": "BASH_PATH_OVERLAP",
            "tool": "bash",
            "target": ",".join(sorted(shared))[:400],
            "workers": str(len({w for workers in shared.values() for w in workers})),
            "count": len(shared),
            "extra_calls": 0,
            "write_before_repeat": True,
        }
    )
    return rows


# ----------------------------------------------------------------------------- aggregates


def composition(turns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per-variant prompt composition from the anchored turn reconstruction."""

    buckets: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for row in turns:
        variant = str(row["variant"])
        for key in (
            "fixed_overhead_tokens",
            "task_tokens",
            "handoff_tokens",
            "assistant_tokens",
            "tool_result_tokens",
            "other_tokens",
            "anchored_prompt_tokens",
        ):
            buckets[variant][key] += float(row[key] or 0)
        buckets[variant]["turns"] += 1
    rows = []
    for variant, item in sorted(buckets.items()):
        total = item["anchored_prompt_tokens"] or 1.0
        rows.append(
            {
                "variant": variant,
                "turns": int(item["turns"]),
                "prompt_tokens": round(total, 1),
                "fixed_tokens": round(item["fixed_overhead_tokens"], 1),
                "fixed_share": round(item["fixed_overhead_tokens"] / total, 4),
                "task_tokens": round(item["task_tokens"], 1),
                "task_share": round(item["task_tokens"] / total, 4),
                "handoff_tokens": round(item["handoff_tokens"], 1),
                "handoff_share": round(item["handoff_tokens"] / total, 4),
                "assistant_tokens": round(item["assistant_tokens"], 1),
                "assistant_share": round(item["assistant_tokens"] / total, 4),
                "tool_result_tokens": round(item["tool_result_tokens"], 1),
                "tool_result_share": round(item["tool_result_tokens"] / total, 4),
                "other_tokens": round(item["other_tokens"], 1),
            }
        )
    return rows


def resend_factor(
    runs: list[tuple[Path, dict[str, Any]]],
    turns: list[dict[str, Any]],
    counter: PromptCounter,
) -> list[dict[str, Any]]:
    """Unique tool-result content vs the same content re-sent across turns."""

    unique: dict[str, float] = defaultdict(float)
    counts: dict[str, int] = defaultdict(int)
    for run, result in runs:
        variant = "a0" if result["sample_id"].endswith("-a0") else "a1"
        for _, messages in conversations(run).items():
            for message in messages:
                for block in message.get("content") or []:
                    if block.get("type") == "tool_result":
                        unique[variant] += counter.block(block)
                        counts[variant] += 1
    accumulated: dict[str, float] = defaultdict(float)
    for row in turns:
        accumulated[str(row["variant"])] += float(row["tool_result_tokens"] or 0)
    rows = []
    for variant in sorted(accumulated):
        total = unique.get(variant, 0.0)
        rows.append(
            {
                "variant": variant,
                "tool_results": counts.get(variant, 0),
                "unique_tool_result_tokens": round(total, 1),
                "billed_tool_result_tokens": round(accumulated[variant], 1),
                "resend_multiplier": round(accumulated[variant] / total, 3) if total else None,
            }
        )
    return rows


def stop_outcomes(
    turns: list[dict[str, Any]], workers: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Join worker stop reasons with oracle verdicts and per-worker prompt tokens."""

    stop_by_worker: dict[tuple[str, str], str] = {}
    for row in workers:
        worker_id = str(row["worker"])
        task_key = worker_id.split(":")[-1].rsplit("-", 1)[-1]
        stop_by_worker[(str(row["run_id"]), task_key)] = str(row["stop_reason"])

    tokens: dict[tuple[str, str, str], float] = defaultdict(float)
    counts: dict[tuple[str, str, str], int] = defaultdict(int)
    for row in turns:
        key = (str(row["variant"]), str(row["run_id"]), str(row["task_key"]))
        tokens[key] += float(row["anchored_prompt_tokens"] or 0)
        counts[key] += 1

    per_stop: dict[tuple[str, str], dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for (variant, run_id, task_key), value in tokens.items():
        stop = stop_by_worker.get((run_id, task_key), "?")
        per_stop[(variant, stop)]["tokens"] += value
        per_stop[(variant, stop)]["turns"] += counts[(variant, run_id, task_key)]
    rows = []
    for (variant, stop), item in sorted(per_stop.items()):
        rows.append(
            {
                "variant": variant,
                "stop_reason": stop,
                "turns": int(item["turns"]),
                "prompt_tokens": round(item["tokens"], 1),
                "tokens_per_turn": round(item["tokens"] / item["turns"], 1)
                if item["turns"]
                else None,
            }
        )
    oracle_rows = []
    for variant in ("a0", "a1"):
        subset = [row for row in workers if row["variant"] == variant]
        for stop in sorted({str(row["stop_reason"]) for row in subset}):
            matching = [row for row in subset if str(row["stop_reason"]) == stop]
            passed = [row for row in matching if row["oracle_passed"] in (True, "True")]
            failed = [row for row in matching if row["oracle_passed"] in (False, "False")]
            oracle_rows.append(
                {
                    "variant": variant,
                    "stop_reason": stop,
                    "workers": len(matching),
                    "oracle_passed": len(passed),
                    "oracle_failed": len(failed),
                }
            )
    return rows, oracle_rows


def _rounds(
    messages: list[dict[str, Any]], counter: PromptCounter
) -> tuple[dict[int, int], dict[int, int]]:
    """Map message index -> tool-call round, and round -> tool-result tokens."""

    index_round: dict[int, int] = {}
    round_tokens: dict[int, int] = defaultdict(int)
    current = -1
    for index, message in enumerate(messages):
        if message.get("role") == "assistant":
            current += 1
            index_round[index] = current
            continue
        index_round[index] = max(current, 0)
        for block in message.get("content") or []:
            if block.get("type") == "tool_result":
                round_tokens[max(current, 0)] += counter.block(block)
    return index_round, round_tokens


def compaction_simulation(
    runs: list[tuple[Path, dict[str, Any]]],
    counter: PromptCounter,
    overhead: dict[str, float],
    keep_rounds: tuple[int, ...] = (1, 2, 3, 4),
) -> list[dict[str, Any]]:
    """Counterfactual: keep only the newest N tool-result rounds verbatim.

    ESTIMATED and strictly counterfactual: it assumes the worker would not re-read anything that
    was elided, which no stored artifact can prove. Its ``keep_all`` row is the self-check against
    the reconstructed totals.
    """

    rows = []
    for keep in (*keep_rounds, 10_000):
        totals: dict[str, float] = defaultdict(float)
        turns: dict[str, int] = defaultdict(int)
        for run, result in runs:
            variant = "a0" if result["sample_id"].endswith("-a0") else "a1"
            fixed = overhead.get(variant, 1_950.0)
            for _, messages in conversations(run).items():
                _, round_tokens = _rounds(messages, counter)
                for prompt_index, prompt in enumerate(turn_prompts(messages)):
                    layers = turn_layers(prompt, counter)
                    last_round = prompt_index - 1
                    kept = sum(
                        round_tokens.get(round_index, 0)
                        for round_index in range(max(0, last_round - keep + 1), last_round + 1)
                    )
                    base = (
                        layers["task"] + layers["handoff"] + layers["assistant"] + layers["other"]
                    )
                    totals[variant] += base + kept + fixed
                    turns[variant] += 1
        rows.append(
            {
                "keep_tool_rounds": keep,
                "a0_tokens": round(totals["a0"], 1),
                "a1_tokens": round(totals["a1"], 1),
                "a1_share_of_observed": round(
                    totals["a1"] / max(1.0, E1_EXPECTED["a1_prompt_tokens"]), 4
                ),
                "amplification": round(totals["a1"] / totals["a0"], 3) if totals["a0"] else None,
                "a0_turns": turns["a0"],
                "a1_turns": turns["a1"],
            }
        )
    return rows


def fixed_prefix_measure(counter: PromptCounter) -> dict[str, Any]:
    """Direct tokenizer count of the rendered worker profile and the native tool schema.

    This is a read-only measurement used to cross-check the derived per-turn residual; it does not
    exercise a sandbox or a model.
    """

    from atlasos.tools import AtlasToolRuntime
    from atlasos.workers.catalog import WorkerCatalog
    from atlasos.workers.models import WorkerTaskContext
    from atlasos.workers.renderer import render_worker_context

    profile = WorkerCatalog().resolve("coding_worker")
    task = WorkerTaskContext(
        task_id="eval-000000000000-parser",
        objective="Implement the assigned module in /workspace/project.",
        background=None,
    )
    system_prompt = render_worker_context(profile, task)
    runtime = AtlasToolRuntime("stage-g2b-diagnosis")
    allowed = set(runtime.agent_tools)
    definitions = [
        definition for definition in runtime.registry.to_tool_defs() if definition.name in allowed
    ]
    schema = json.dumps(
        [
            {
                "name": definition.name,
                "description": definition.description,
                "input_schema": definition.input_schema,
            }
            for definition in definitions
        ],
        default=str,
        sort_keys=True,
    )
    system_tokens = counter.count(system_prompt)
    schema_tokens = counter.count(schema)
    return {
        "profile_id": profile.profile_id,
        "system_prompt_tokens": system_tokens,
        "tool_count": len(definitions),
        "tool_schema_tokens": schema_tokens,
        "fixed_total_tokens": system_tokens + schema_tokens,
        "data_type": "ESTIMATED (tiktoken cl100k)",
    }


# ----------------------------------------------------------------------------- drivers


def e1_runs() -> list[tuple[Path, dict[str, Any]]]:
    pairs = []
    for run in run_dirs(ROUNDS["e1-formal"]):
        result = _load_json(run / "result.json")
        if isinstance(result, dict):
            pairs.append((run, result))
    return pairs


def amplification(runs: list[tuple[Path, dict[str, Any]]]) -> dict[str, Any]:
    totals: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for _, result in runs:
        variant = "a0" if result["sample_id"].endswith("-a0") else "a1"
        metrics = result["metrics"]
        totals[variant]["runs"] += 1
        totals[variant]["success"] += int(bool(result["success"]))
        totals[variant]["prompt"] += metrics.get("prompt_tokens") or 0
        totals[variant]["completion"] += metrics.get("completion_tokens") or 0
        totals[variant]["turns"] += metrics.get("model_turns") or 0
        totals[variant]["tools"] += metrics.get("tool_calls") or 0
        totals[variant]["hit"] += metrics.get("cache_hit_tokens") or 0
        totals[variant]["miss"] += metrics.get("cache_miss_tokens") or 0
    a0, a1 = totals["a0"], totals["a1"]
    prompt_amp = a1["prompt"] / a0["prompt"] if a0["prompt"] else None
    turn_amp = a1["turns"] / a0["turns"] if a0["turns"] else None
    per_turn_amp = (
        ((a1["prompt"] / a1["turns"]) / (a0["prompt"] / a0["turns"]))
        if a0["turns"] and a1["turns"] and a0["prompt"] and a1["prompt"]
        else None
    )
    return {
        "totals": {variant: dict(values) for variant, values in totals.items()},
        "prompt_amplification": round(prompt_amp, 3) if prompt_amp else None,
        "turn_amplification": round(turn_amp, 3) if turn_amp else None,
        "tokens_per_turn_amplification": round(per_turn_amp, 3) if per_turn_amp else None,
        "identity_check": round(turn_amp * per_turn_amp, 3) if turn_amp and per_turn_amp else None,
        "prompt_per_turn": {
            variant: round(values["prompt"] / values["turns"], 1)
            for variant, values in totals.items()
            if values["turns"]
        },
    }


def worker_rows(runs: list[tuple[Path, dict[str, Any]]]) -> list[dict[str, Any]]:
    rows = []
    for _, result in runs:
        for name, worker in (result.get("worker_results") or {}).items():
            usage = worker.get("token_usage") or {}
            rows.append(
                {
                    "sample_id": result["sample_id"],
                    "run_id": result["identity"]["run_id"],
                    "variant": "a0" if result["sample_id"].endswith("-a0") else "a1",
                    "worker": name,
                    "success": worker.get("success"),
                    "stop_reason": (worker.get("stop_reason") or {}).get("code"),
                    "tool_calls": worker.get("tool_calls"),
                    "input_tokens_no_cache": usage.get("input_tokens"),
                    "output_tokens": usage.get("output_tokens"),
                    "cache_read_reported": usage.get("cache_read_input_tokens"),
                    "run_prompt_tokens": result["metrics"].get("prompt_tokens"),
                    "oracle_passed": (result.get("oracle") or {}).get("passed"),
                }
            )
    return rows


def run_rows(runs: list[tuple[Path, dict[str, Any]]]) -> list[dict[str, Any]]:
    rows = []
    for _, result in runs:
        metrics = result["metrics"]
        prompt = metrics.get("prompt_tokens") or 0
        turns = metrics.get("model_turns") or 0
        rows.append(
            {
                "sample_id": result["sample_id"],
                "run_id": result["identity"]["run_id"],
                "variant": "a0" if result["sample_id"].endswith("-a0") else "a1",
                "workers": len(result.get("worker_results") or {}),
                "success": result["success"],
                "oracle_passed": (result.get("oracle") or {}).get("passed"),
                "failure_class": result.get("failure_class"),
                "prompt_tokens": prompt,
                "completion_tokens": metrics.get("completion_tokens"),
                "cache_hit_tokens": metrics.get("cache_hit_tokens"),
                "cache_miss_tokens": metrics.get("cache_miss_tokens"),
                "model_turns": turns,
                "tool_calls": metrics.get("tool_calls"),
                "tokens_per_turn": round(prompt / turns, 1) if turns else None,
                "latency_ms": metrics.get("latency_ms"),
                "cost_usd": metrics.get("estimated_cost_usd"),
            }
        )
    return rows


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields = list(rows[0])
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


#: Stage E1 figures published before this diagnosis; used as a consistency check.
E1_EXPECTED = {
    "a0_prompt_tokens": 757_828,
    "a1_prompt_tokens": 4_992_381,
    "a0_model_turns": 257,
    "a1_model_turns": 987,
    "a0_success": 28,
    "a1_success": 3,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="atlas-stage-g2b-diagnosis")
    parser.add_argument("--output", type=Path, default=OUTPUT_ROOT)
    args = parser.parse_args(argv)
    output: Path = args.output
    output.mkdir(parents=True, exist_ok=True)

    counter = PromptCounter(select_tokenizer("tiktoken", model="deepseek-chat"))
    runs = e1_runs()
    inv = inventory()
    amp = amplification(runs)

    turns: list[dict[str, Any]] = []
    turn_summaries: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    for run, result in runs:
        rows, summaries, diag = per_turn(run, result, counter)
        turns.extend(rows)
        turn_summaries.extend(summaries)
        diagnostics.extend(diag)

    repeat_rows: list[dict[str, Any]] = []
    for run, result in runs:
        repeat_rows.extend(repeats(run, result))

    workers = worker_rows(runs)
    compose = composition(turns)
    resend = resend_factor(runs, turns, counter)
    stop_tokens, stop_oracle = stop_outcomes(turns, workers)
    fixed = fixed_prefix_measure(counter)
    overhead = {
        variant: (
            sum(
                row["overhead_per_turn"] * row["reconstructed_turns"]
                for row in diagnostics
                if row["variant"] == variant
            )
            / max(
                1,
                sum(row["reconstructed_turns"] for row in diagnostics if row["variant"] == variant),
            )
        )
        for variant in ("a0", "a1")
    }
    compaction = compaction_simulation(runs, counter, overhead)

    totals = amp["totals"]
    checks = {
        "a0_prompt_tokens": totals["a0"]["prompt"] == E1_EXPECTED["a0_prompt_tokens"],
        "a1_prompt_tokens": totals["a1"]["prompt"] == E1_EXPECTED["a1_prompt_tokens"],
        "a0_model_turns": totals["a0"]["turns"] == E1_EXPECTED["a0_model_turns"],
        "a1_model_turns": totals["a1"]["turns"] == E1_EXPECTED["a1_model_turns"],
        "a0_success": totals["a0"]["success"] == E1_EXPECTED["a0_success"],
        "a1_success": totals["a1"]["success"] == E1_EXPECTED["a1_success"],
    }
    turn_counts = {
        variant: sum(1 for row in diagnostics if row["variant"] == variant)
        for variant in ("a0", "a1")
    }
    reconciliation = {
        "observed_model_turns": {
            "a0": totals["a0"]["turns"],
            "a1": totals["a1"]["turns"],
        },
        "reconstructed_turns": {
            "a0": sum(
                int(row["reconstructed_turns"]) for row in diagnostics if row["variant"] == "a0"
            ),
            "a1": sum(
                int(row["reconstructed_turns"]) for row in diagnostics if row["variant"] == "a1"
            ),
        },
        "runs_checked": turn_counts,
        "turn_mismatches": [
            row["run_id"]
            for row in diagnostics
            if row["reconstructed_turns"] != row["observed_model_turns"]
        ],
        "overhead_per_turn": {
            variant: {
                "min": round(min(values), 1),
                "p50": round(sorted(values)[len(values) // 2], 1),
                "max": round(max(values), 1),
            }
            for variant, values in (
                (
                    "a0",
                    [row["overhead_per_turn"] for row in diagnostics if row["variant"] == "a0"],
                ),
                (
                    "a1",
                    [row["overhead_per_turn"] for row in diagnostics if row["variant"] == "a1"],
                ),
            )
            if values
        },
    }
    payload = {
        "inventory": inv,
        "amplification": amp,
        "expected_check": {
            "expected": E1_EXPECTED,
            "passed": checks,
            "all_passed": all(checks.values()),
        },
        "turn_reconciliation": reconciliation,
        "composition": compose,
        "tool_result_resend": resend,
        "stop_reason_tokens": stop_tokens,
        "stop_reason_oracle": stop_oracle,
        "fixed_prefix": fixed,
        "compaction_simulation": compaction,
        "turn_diagnostics": diagnostics,
        "turn_summaries": turn_summaries,
    }
    (output / "summary.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
    )
    write_csv(output / "runs.csv", run_rows(runs))
    write_csv(output / "workers.csv", workers)
    write_csv(output / "turns.csv", turns)
    write_csv(output / "turn_summaries.csv", turn_summaries)
    write_csv(output / "repeats.csv", repeat_rows)
    write_csv(output / "composition.csv", compose)
    write_csv(output / "resend.csv", resend)
    write_csv(output / "stop_tokens.csv", stop_tokens)
    write_csv(output / "stop_oracle.csv", stop_oracle)
    write_csv(output / "compaction.csv", compaction)

    print(json.dumps({k: payload[k] for k in ("amplification", "expected_check")}, indent=2))
    print(
        "reconciliation:",
        json.dumps(
            {
                k: reconciliation[k]
                for k in ("observed_model_turns", "reconstructed_turns", "turn_mismatches")
            },
            indent=2,
        ),
    )
    print("composition:", json.dumps(compose, indent=2))
    print("resend:", json.dumps(resend, indent=2))
    print("fixed prefix:", json.dumps(fixed, indent=2))
    print(
        "turns:",
        len(turns),
        "turn summaries:",
        len(turn_summaries),
        "repeat rows:",
        len(repeat_rows),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
