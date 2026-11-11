"""Stage G3-3: C0 (original worker text) vs C1 (structured contract), from run artifacts.

Consumes one Stage G3-3 round (``experiments/results/stage-g3-3/<round>``): the round manifest and,
per run, ``result.json``, ``tool_conversations.json``, ``workspace/`` and ``provider_calls.jsonl``.
It never calls a model and never writes into a run directory.

Behavior metrics reuse the Stage G3-0 turn classifier (EXPLORE/IMPLEMENT/REPAIR/VERIFY) and the
oracle-probe pattern, so the two stages measure the same thing.

Outputs (``<round>/analysis/``): ``runs.csv``, ``workers.csv``, ``probes.csv``,
``deliverables.csv``, ``ownership.csv``, ``summary.json``.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from anycode.context.tokenizer import select_tokenizer
from experiments.evaluation.stage_e_tasks import definition_by_id
from experiments.evaluation.stage_g2b_diagnosis import PromptCounter, conversations
from experiments.evaluation.stage_g3_failure_audit import (
    EDIT_TOOLS,
    IMPLEMENT_TOOLS,
    VERIFY_PATTERNS,
    classify_turn,
    is_oracle_probe,
)
from experiments.evaluation.stage_g3_worker_contracts import build_contract, worker_task_text

REPO_ROOT = Path(__file__).resolve().parents[2]
RESULTS_ROOT = REPO_ROOT / "experiments" / "results" / "stage-g3-3"
CONTRACT_MARKER = "[WORKER TASK CONTRACT]"
STUB_MARKER = "NotImplementedError"
_SCRATCH = re.compile(r"(^|/)(tmp|scratch|\.cache)(/|$)")


def latest_round() -> Path:
    rounds = sorted(path for path in RESULTS_ROOT.glob("g33-*") if path.is_dir())
    if not rounds:
        raise SystemExit(f"no Stage G3-3 round under {RESULTS_ROOT}")
    return rounds[-1]


def _load(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


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
                    str(result.get("worker_contract_mode") or suffix),
                    Path(run_dir),
                )
            )
    arms.sort(key=lambda item: (item[0], item[1], item[2]))
    return arms


def step_for(sample_id: str, worker_name: str) -> Any:
    definition = definition_by_id(sample_id)
    step_id = worker_name.split(":")[-1].rsplit("-", 1)[-1]
    for step in definition.workers:
        if step.task_id == step_id:
            return step
    return None


def ownership_for(sample_id: str, worker_name: str) -> tuple[str, ...]:
    definition = definition_by_id(sample_id)
    step = step_for(sample_id, worker_name)
    if step is None:
        return ()
    return build_contract(
        step, steps=definition.workers, sample_id=definition.sample_id
    ).owned_files


@dataclass
class WorkerRow:
    run_id: str
    sample_id: str
    repetition: int
    mode: str
    worker: str
    step_id: str
    success: bool
    stop_code: str
    tool_turns: int
    explore_turns: int
    implement_turns: int
    verify_turns: int
    probe_turns: int
    first_implement_turn: int | None
    first_write_path: str | None
    owned_files: tuple[str, ...]
    ownership_violations: int
    claimed_verification: bool
    ran_verification: bool
    deliverable_state: str
    task_text_chars: int
    contract_present: bool


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
    prompt_tokens: int
    completion_tokens: int
    cache_hit_tokens: int | None
    cache_miss_tokens: int | None
    cache_hit_ratio: float | None
    model_turns: int
    tool_calls: int
    cost: float | None
    latency_ms: float
    budget_exceeded: bool
    admission_rejections: int
    contract_overhead_chars: int
    contract_overhead_tokens: int | None
    task_text_chars: int
    failed_checks: list[str] = field(default_factory=list)
    run_dir: str = ""


def _worker_turns(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One entry per tool call, with the turn index of the assistant message that issued it."""

    names: dict[str, str] = {}
    inputs: dict[str, dict[str, Any]] = {}
    turn_of: dict[str, int] = {}
    turn = 0
    for message in messages:
        if message.get("role") == "assistant":
            turn += 1
            for block in message.get("content") or []:
                if block.get("type") == "tool_use":
                    call_id = str(block.get("id"))
                    names[call_id] = str(block.get("name"))
                    inputs[call_id] = dict(block.get("input") or {})
                    turn_of[call_id] = turn
    calls: list[dict[str, Any]] = []
    for message in messages:
        if message.get("role") != "user":
            continue
        for block in message.get("content") or []:
            if block.get("type") != "tool_result":
                continue
            call_id = str(block.get("tool_use_id"))
            content = block.get("content")
            text = content if isinstance(content, str) else json.dumps(content, default=str)
            calls.append(
                {
                    "call_id": call_id,
                    "tool": names.get(call_id, "?"),
                    "input": inputs.get(call_id, {}),
                    "is_error": bool(block.get("is_error")),
                    "text": text,
                    "turn": turn_of.get(call_id, 0),
                }
            )
    return calls


def _write_path(tool: str, payload: dict[str, Any]) -> str | None:
    if tool not in IMPLEMENT_TOOLS | EDIT_TOOLS:
        return None
    path = str(payload.get("path") or "")
    if not path:
        return None
    return path.replace("/workspace/project/", "").lstrip("/")


def analyze_worker(
    *,
    run_id: str,
    sample_id: str,
    repetition: int,
    mode: str,
    name: str,
    info: dict[str, Any],
    messages: list[dict[str, Any]],
    workspace: Path,
) -> WorkerRow:
    calls = _worker_turns(messages)
    step = step_for(sample_id, name)
    step_id = step.task_id if step is not None else name.rsplit("-", 1)[-1]
    owned = ownership_for(sample_id, name)
    foreign = ownership_for_all(sample_id) - set(owned)

    turns: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for call in calls:
        turns[call["turn"]].append(call)

    phases: dict[int, str] = {}
    for turn, items in turns.items():
        labels = [
            classify_turn(str(item["tool"]), dict(item["input"]), prior_verify_failed=False)
            for item in items
        ]
        if any(label in {"IMPLEMENT", "REPAIR"} for label in labels):
            phases[turn] = "IMPLEMENT"
        elif "VERIFY" in labels:
            phases[turn] = "VERIFY"
        elif "EXPLORE" in labels:
            phases[turn] = "EXPLORE"
        else:
            phases[turn] = "OTHER"

    first_implement = next(
        (
            turn
            for turn in sorted(turns)
            if any(
                _write_path(str(item["tool"]), dict(item["input"]))
                and not _SCRATCH.search(_write_path(str(item["tool"]), dict(item["input"])) or "")
                for item in turns[turn]
            )
        ),
        None,
    )
    first_write = next(
        (
            _write_path(str(item["tool"]), dict(item["input"]))
            for turn in sorted(turns)
            for item in turns[turn]
            if _write_path(str(item["tool"]), dict(item["input"]))
        ),
        None,
    )

    violations = 0
    for call in calls:
        path = _write_path(str(call["tool"]), dict(call["input"]))
        if path and path in foreign:
            violations += 1

    probes = [call for call in calls if is_oracle_probe(str(call["tool"]), dict(call["input"]))]

    assistant_text = "\n".join(
        block.get("text") or ""
        for message in messages
        if message.get("role") == "assistant"
        for block in (message.get("content") or [])
        if block.get("type") == "text"
    ).lower()
    claim = bool(
        re.search(
            r"\b(tests?|checks?|smoke test|verification)\b.*\b(pass|passed|succeed|ok)\b",
            assistant_text,
        )
        or "all passed" in assistant_text
    )
    verify_calls = [
        call
        for call in calls
        if call["tool"] == "bash"
        and VERIFY_PATTERNS.search(str(call["input"].get("command") or ""))
    ]
    ran_verification = any(not call["is_error"] for call in verify_calls)

    deliverable = deliverable_state(workspace, owned, verify_calls)

    return WorkerRow(
        run_id=run_id,
        sample_id=sample_id,
        repetition=repetition,
        mode=mode,
        worker=name,
        step_id=step_id,
        success=bool(info.get("success")),
        stop_code=str((info.get("stop_reason") or {}).get("code") or "none"),
        tool_turns=len(turns),
        explore_turns=sum(1 for phase in phases.values() if phase == "EXPLORE"),
        implement_turns=sum(1 for phase in phases.values() if phase == "IMPLEMENT"),
        verify_turns=sum(1 for phase in phases.values() if phase == "VERIFY"),
        probe_turns=len({call["turn"] for call in probes}),
        first_implement_turn=first_implement,
        first_write_path=first_write,
        owned_files=owned,
        ownership_violations=violations,
        claimed_verification=claim,
        ran_verification=ran_verification,
        deliverable_state=deliverable,
        task_text_chars=len(_task_text(messages)),
        contract_present=CONTRACT_MARKER in _task_text(messages),
    )


def _task_text(messages: list[dict[str, Any]]) -> str:
    for message in messages:
        if message.get("role") != "user":
            continue
        parts = [
            block.get("text") or ""
            for block in (message.get("content") or [])
            if block.get("type") == "text"
        ]
        if parts:
            return "\n".join(parts)
    return ""


def ownership_for_all(sample_id: str) -> set[str]:
    definition = definition_by_id(sample_id)
    paths: set[str] = set()
    for step in definition.workers:
        paths.update(
            build_contract(
                step, steps=definition.workers, sample_id=definition.sample_id
            ).owned_files
        )
    return paths


def deliverable_state(
    workspace: Path, owned: tuple[str, ...], verify_calls: list[dict[str, Any]]
) -> str:
    if not owned:
        return "unverifiable"
    present = [path for path in owned if (workspace / path).is_file()]
    if not present:
        return "missing"
    if len(present) < len(owned):
        return "present_incomplete"
    stubs = [
        path
        for path in owned
        if STUB_MARKER in (workspace / path).read_text(encoding="utf-8", errors="replace")
    ]
    if stubs:
        return "present_incomplete"
    if any(not call["is_error"] for call in verify_calls):
        return "present_verified"
    return "present_unverified"


def run_row(
    run_dir: Path, sample_id: str, repetition: int, mode: str
) -> tuple[RunRow, list[WorkerRow]]:
    result = _load(run_dir / "result.json") or {}
    identity = result.get("identity") or {}
    metrics = result.get("metrics") or {}
    extra = metrics.get("extra") or metrics
    oracle = result.get("oracle") or {}
    workers = result.get("worker_results") or {}
    contract = extra.get("worker_contract") or {}
    workspace = run_dir / "workspace"
    transcripts = conversations(run_dir)

    rows: list[WorkerRow] = []
    for name, info in workers.items():
        key = name.split(":")[-1].rsplit("-", 1)[-1]
        rows.append(
            analyze_worker(
                run_id=str(identity.get("run_id") or run_dir.name),
                sample_id=sample_id,
                repetition=repetition,
                mode=mode,
                name=str(name),
                info=info,
                messages=transcripts.get(key) or [],
                workspace=workspace,
            )
        )

    failed = sorted(name for name, value in (oracle.get("checks") or {}).items() if not value)
    cost = metrics.get("estimated_cost_usd")
    hit = metrics.get("cache_hit_tokens")
    miss = metrics.get("cache_miss_tokens")
    row = RunRow(
        run_id=str(identity.get("run_id") or run_dir.name),
        sample_id=sample_id,
        repetition=repetition,
        mode=mode,
        success=bool(result.get("success")),
        failure_class=result.get("failure_class"),
        oracle_passed=oracle.get("passed"),
        workers=len(workers),
        worker_success=sum(1 for info in workers.values() if info.get("success")),
        prompt_tokens=int(metrics.get("prompt_tokens") or 0),
        completion_tokens=int(metrics.get("completion_tokens") or 0),
        cache_hit_tokens=hit,
        cache_miss_tokens=miss,
        cache_hit_ratio=(hit / (hit + miss))
        if hit is not None and miss is not None and hit + miss
        else None,
        model_turns=int(metrics.get("model_turns") or 0),
        tool_calls=sum(
            len(_worker_turns(m))
            for m in [transcripts.get(n.split(":")[-1].rsplit("-", 1)[-1]) or [] for n in workers]
        ),
        cost=float(cost) if cost is not None else None,
        latency_ms=float(metrics.get("latency_ms") or 0.0),
        budget_exceeded=str(result.get("failure_class") or "") == "BUDGET_EXCEEDED",
        admission_rejections=int((extra.get("budget") or {}).get("admission_rejections") or 0),
        contract_overhead_chars=int(contract.get("overhead_chars") or 0),
        contract_overhead_tokens=None,
        task_text_chars=sum(item.task_text_chars for item in rows),
        failed_checks=failed,
        run_dir=str(run_dir),
    )
    return row, rows


def aggregate(rows: list[RunRow], workers: list[WorkerRow]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for mode in ("legacy", "structured"):
        selected = [row for row in rows if row.mode == mode]
        group = [row for row in workers if row.mode == mode]
        hits = [row.cache_hit_tokens for row in selected]
        misses = [row.cache_miss_tokens for row in selected]
        complete = all(value is not None for value in hits + misses)
        total_hit = sum(int(v) for v in hits if v is not None) if complete else None
        total_miss = sum(int(v) for v in misses if v is not None) if complete else None
        with_impl = [row.first_implement_turn for row in group if row.first_implement_turn]
        summary[mode] = {
            "runs": len(selected),
            "oracle_pass": sum(1 for row in selected if row.oracle_passed),
            "task_success": sum(1 for row in selected if row.failure_class == "TASK_SUCCESS"),
            "worker_success": sum(row.success for row in group),
            "workers": len(group),
            "prompt_tokens": sum(row.prompt_tokens for row in selected),
            "completion_tokens": sum(row.completion_tokens for row in selected),
            "cache_hit_tokens": total_hit,
            "cache_miss_tokens": total_miss,
            "cache_hit_ratio": (
                round(total_hit / (total_hit + total_miss), 4)
                if total_hit is not None and total_miss is not None and total_hit + total_miss
                else None
            ),
            "cost_usd": round(sum(row.cost or 0.0 for row in selected), 6),
            "model_turns": sum(row.model_turns for row in selected),
            "tool_calls": sum(row.tool_calls for row in selected),
            "budget_exceeded": sum(1 for row in selected if row.budget_exceeded),
            "admission_rejections": sum(row.admission_rejections for row in selected),
            "avg_prompt_per_turn": (
                round(
                    sum(row.prompt_tokens for row in selected)
                    / sum(row.model_turns for row in selected),
                    1,
                )
                if sum(row.model_turns for row in selected)
                else None
            ),
            "contract_overhead_chars": sum(row.contract_overhead_chars for row in selected),
            "tool_turns": sum(row.tool_turns for row in group),
            "explore_turns": sum(row.explore_turns for row in group),
            "implement_turns": sum(row.implement_turns for row in group),
            "verify_turns": sum(row.verify_turns for row in group),
            "explore_ratio": (
                round(
                    sum(row.explore_turns for row in group) / sum(row.tool_turns for row in group),
                    4,
                )
                if sum(row.tool_turns for row in group)
                else None
            ),
            "probe_turns": sum(row.probe_turns for row in group),
            "workers_with_probe": sum(1 for row in group if row.probe_turns),
            "avg_first_implement_turn": (
                round(sum(with_impl) / len(with_impl), 2) if with_impl else None
            ),
            "workers_implementing": len(with_impl),
            "ownership_violations": sum(row.ownership_violations for row in group),
            "deliverable_states": dict(Counter(row.deliverable_state for row in group)),
            "workers_claiming_verification": sum(1 for row in group if row.claimed_verification),
            "workers_not_running_verification": sum(
                1 for row in group if row.claimed_verification and not row.ran_verification
            ),
            "workers_running_verification": sum(1 for row in group if row.ran_verification),
            "workers_not_implementing": sum(1 for row in group if row.first_implement_turn is None),
            "contract_present": sum(1 for row in group if row.contract_present),
        }
    return summary


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
    parser = argparse.ArgumentParser(prog="atlas-stage-g3-3-analysis")
    parser.add_argument("--round", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)

    round_dir: Path = args.round or latest_round()
    output: Path = args.output or (round_dir / "analysis")
    output.mkdir(parents=True, exist_ok=True)

    run_rows: list[RunRow] = []
    worker_rows: list[WorkerRow] = []
    for sample_id, repetition, mode, run_dir in arm_runs(round_dir):
        row, workers = run_row(run_dir, sample_id, repetition, mode)
        run_rows.append(row)
        worker_rows.extend(workers)

    counter = PromptCounter(select_tokenizer("tiktoken", model="deepseek-chat"))
    token_cache: dict[str, int] = {}
    for row in run_rows:
        if row.sample_id not in token_cache:
            token_cache[row.sample_id] = contract_tokens(row.sample_id, counter)
        row.contract_overhead_tokens = token_cache[row.sample_id]

    write_csv(output / "runs.csv", [row.__dict__ for row in run_rows])
    write_csv(output / "workers.csv", [row.__dict__ for row in worker_rows])

    probes: list[dict[str, Any]] = []
    for sample_id, repetition, mode, run_dir in arm_runs(round_dir):
        for task_key, messages in conversations(run_dir).items():
            for call in _worker_turns(messages):
                if not is_oracle_probe(str(call["tool"]), dict(call["input"])):
                    continue
                probes.append(
                    {
                        "run_id": run_dir.name,
                        "sample_id": sample_id,
                        "repetition": repetition,
                        "mode": mode,
                        "task": task_key,
                        "turn": call["turn"],
                        "tool": call["tool"],
                        "command": " ".join(
                            str(call["input"].get("command") or call["input"]).split()
                        )[:200],
                        "is_error": call["is_error"],
                    }
                )
    write_csv(output / "probes.csv", probes)

    write_csv(
        output / "deliverables.csv",
        [
            {
                "run_id": row.run_id,
                "sample_id": row.sample_id,
                "repetition": row.repetition,
                "mode": row.mode,
                "worker": row.worker,
                "owned_files": ";".join(row.owned_files),
                "deliverable_state": row.deliverable_state,
                "first_implement_turn": row.first_implement_turn,
                "first_write_path": row.first_write_path,
                "ran_verification": row.ran_verification,
                "claimed_verification": row.claimed_verification,
            }
            for row in worker_rows
        ],
    )
    write_csv(
        output / "ownership.csv",
        [
            {
                "run_id": row.run_id,
                "sample_id": row.sample_id,
                "mode": row.mode,
                "worker": row.worker,
                "owned_files": ";".join(row.owned_files),
                "violations": row.ownership_violations,
            }
            for row in worker_rows
            if row.ownership_violations
        ],
    )

    summary = aggregate(run_rows, worker_rows)
    summary["round"] = str(round_dir)
    summary["contract_overhead_tokens"] = sum(row.contract_overhead_tokens or 0 for row in run_rows)
    summary["per_task"] = _per_task(run_rows, worker_rows)
    (output / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8"
    )

    print(json.dumps(summary, indent=2, sort_keys=True))
    for row in run_rows:
        print(
            f"{row.sample_id} rep={row.repetition} {row.mode:10s} oracle={row.oracle_passed} "
            f"class={row.failure_class} prompt={row.prompt_tokens} turns={row.model_turns} "
            f"cost={row.cost} overhead={row.contract_overhead_chars} failed={row.failed_checks}"
        )
    return 0


def contract_tokens(sample_id: str, counter: PromptCounter) -> int:
    """Exact token cost of the structured contract for one run (all workers of the sample)."""

    definition = definition_by_id(sample_id)
    total = 0
    for step in definition.workers:
        legacy = worker_task_text(
            step, steps=definition.workers, sample_id=definition.sample_id, mode="legacy"
        )
        structured = worker_task_text(
            step, steps=definition.workers, sample_id=definition.sample_id, mode="structured"
        )
        total += counter.count(structured[len(legacy) :])
    return total


def _per_task(rows: list[RunRow], workers: list[WorkerRow]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for sample_id in sorted({row.sample_id for row in rows}):
        entry: dict[str, Any] = {}
        for mode in ("legacy", "structured"):
            selected = [row for row in rows if row.sample_id == sample_id and row.mode == mode]
            group = [row for row in workers if row.sample_id == sample_id and row.mode == mode]
            entry[mode] = {
                "oracle_pass": sum(1 for row in selected if row.oracle_passed),
                "task_success": sum(1 for row in selected if row.failure_class == "TASK_SUCCESS"),
                "prompt_tokens": sum(row.prompt_tokens for row in selected),
                "cost_usd": round(sum(row.cost or 0.0 for row in selected), 6),
                "model_turns": sum(row.model_turns for row in selected),
                "explore_ratio": (
                    round(
                        sum(row.explore_turns for row in group)
                        / sum(row.tool_turns for row in group),
                        4,
                    )
                    if sum(row.tool_turns for row in group)
                    else None
                ),
                "probe_turns": sum(row.probe_turns for row in group),
                "ownership_violations": sum(row.ownership_violations for row in group),
                "deliverable_states": dict(Counter(row.deliverable_state for row in group)),
                "first_implement_turn": (
                    round(
                        sum(row.first_implement_turn for row in group if row.first_implement_turn)
                        / max(1, sum(1 for row in group if row.first_implement_turn)),
                        2,
                    )
                    if any(row.first_implement_turn for row in group)
                    else None
                ),
                "worker_success": sum(1 for row in group if row.success),
                "workers": len(group),
            }
        result[sample_id] = entry
    return result


if __name__ == "__main__":
    raise SystemExit(main())
