"""Stage G3-1: legacy vs corrected tool-error semantics, read from real run artifacts.

Consumes one Stage G3-1 round (``experiments/results/stage-g3-1/<round>``): the round manifest for
the arm mapping plus each run's ``result.json``, ``tool_conversations.json`` and
``trajectory/*/transcript.jsonl``. It never calls a model, never starts a sandbox and never writes
into a run directory.

Outputs (``<round>/analysis/``): ``runs.csv``, ``workers.csv``, ``mechanism.csv``, ``summary.json``.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from experiments.evaluation.stage_g2b_diagnosis import conversations

REPO_ROOT = Path(__file__).resolve().parents[2]
RESULTS_ROOT = REPO_ROOT / "experiments" / "results" / "stage-g3-1"

_EXIT_RE = re.compile(r"exit code (\d+)")
_SILENT_EXIT_RE = re.compile(r"\(exited with code (\d+)")
_BASH_ERROR_TOOL = "bash"
REPAIR_TOOLS = frozenset({"file_write", "file_edit"})


def latest_round() -> Path:
    rounds = sorted(path for path in RESULTS_ROOT.glob("g31-*") if path.is_dir())
    if not rounds:
        raise SystemExit(f"no Stage G3-1 round under {RESULTS_ROOT}")
    return rounds[-1]


def exit_code_of(text: str) -> int | None:
    match = _EXIT_RE.search(text) or _SILENT_EXIT_RE.search(text)
    return int(match.group(1)) if match else None


@dataclass
class WorkerRow:
    run_id: str
    sample_id: str
    repetition: int
    mode: str
    worker: str
    success: bool
    stop_code: str
    stop_message: str
    tool_calls: int
    assistant_turns: int
    completed_bash_errors: int
    continued_after_error: bool
    repaired_after_error: bool
    bash_calls: int
    bash_nonzero_exits: int


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
    workers_success: int
    side_effect_unknown_workers: int
    completed_error_terminations: int
    max_turns_workers: int
    bash_calls: int
    bash_nonzero_exits: int
    prompt_tokens: int
    completion_tokens: int
    model_turns: int
    tool_calls: int
    cost: float
    latency_ms: float
    budget_exceeded: bool
    budget: dict[str, Any] = field(default_factory=dict)
    run_dir: str = ""


def _load(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def arm_runs(round_dir: Path) -> list[tuple[str, int, str, Path]]:
    """(sample_id, repetition, mode, run_dir) from the round manifest."""

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
                    str(result.get("tool_error_mode") or suffix),
                    Path(run_dir),
                )
            )
    arms.sort(key=lambda item: (item[0], item[1], item[2]))
    return arms


def worker_turns(messages: list[dict[str, Any]]) -> list[tuple[int, str, list[dict[str, Any]]]]:
    """(message index, tool name, input) for every tool_use block in the transcript."""

    calls: list[tuple[int, str, list[dict[str, Any]]]] = []
    for index, message in enumerate(messages):
        if message.get("role") != "assistant":
            continue
        for block in message.get("content") or []:
            if block.get("type") == "tool_use":
                calls.append((index, str(block.get("name")), [dict(block.get("input") or {})]))
    return calls


def error_position(messages: list[dict[str, Any]]) -> int | None:
    """Index of the message holding the first completed non-zero bash result."""

    names: dict[str, str] = {}
    for index, message in enumerate(messages):
        for block in message.get("content") or []:
            if block.get("type") == "tool_use":
                names[str(block.get("id"))] = str(block.get("name"))
            elif block.get("type") == "tool_result":
                call_id = str(block.get("tool_use_id"))
                if names.get(call_id) != _BASH_ERROR_TOOL:
                    continue
                content = block.get("content")
                text = content if isinstance(content, str) else json.dumps(content, default=str)
                code = exit_code_of(text)
                if bool(block.get("is_error")) and code not in (None, 0):
                    return index
    return None


def worker_row(
    run_id: str,
    sample_id: str,
    repetition: int,
    mode: str,
    name: str,
    info: dict[str, Any],
    messages: list[dict[str, Any]],
) -> WorkerRow:
    stop = info.get("stop_reason") or {}
    calls = worker_turns(messages)
    assistant_turns = sum(1 for message in messages if message.get("role") == "assistant")
    bash_calls = [item for item in calls if item[1] == _BASH_ERROR_TOOL]
    error_index = error_position(messages)
    continued = error_index is not None and any(
        message.get("role") == "assistant" for message in messages[error_index + 1 :]
    )
    repaired = error_index is not None and any(
        index > error_index and tool in REPAIR_TOOLS for index, tool, _ in calls
    )
    names: dict[str, str] = {}
    completed_errors = 0
    for message in messages:
        for block in message.get("content") or []:
            if block.get("type") == "tool_use":
                names[str(block.get("id"))] = str(block.get("name"))
            elif block.get("type") == "tool_result":
                if names.get(str(block.get("tool_use_id"))) != _BASH_ERROR_TOOL:
                    continue
                content = block.get("content")
                text = content if isinstance(content, str) else json.dumps(content, default=str)
                code = exit_code_of(text)
                if bool(block.get("is_error")) and code not in (None, 0):
                    completed_errors += 1
    return WorkerRow(
        run_id=run_id,
        sample_id=sample_id,
        repetition=repetition,
        mode=mode,
        worker=name,
        success=bool(info.get("success")),
        stop_code=str(stop.get("code") or "none"),
        stop_message=str(stop.get("message") or ""),
        tool_calls=int(info.get("tool_calls") or len(calls)),
        assistant_turns=assistant_turns,
        completed_bash_errors=completed_errors,
        continued_after_error=continued,
        repaired_after_error=repaired,
        bash_calls=len(bash_calls),
        bash_nonzero_exits=completed_errors,
    )


def run_row(
    run_dir: Path, sample_id: str, repetition: int, mode: str
) -> tuple[RunRow, list[WorkerRow], list[dict[str, Any]]]:
    result = _load(run_dir / "result.json") or {}
    identity = result.get("identity") or {}
    metrics = result.get("metrics") or {}
    oracle = result.get("oracle") or {}
    conversations_by_task = conversations(run_dir)
    workers = result.get("worker_results") or {}

    rows: list[WorkerRow] = []
    for name, info in workers.items():
        # ``tool_conversations.json`` is keyed by the DAG step id (``loader``), while the worker
        # agent name is ``coding_worker:<run_id>-loader``; the step id is the suffix.
        key = name.split(":")[-1].rsplit("-", 1)[-1]
        messages = conversations_by_task.get(key) or []
        rows.append(
            worker_row(
                str(identity.get("run_id") or run_dir.name),
                sample_id,
                repetition,
                mode,
                str(name),
                info,
                messages,
            )
        )

    # Count bash traffic from the durable transcripts: the run-level ``tool_calls`` projection
    # truncates long outputs, so a trailing exit-code marker can be missing there.
    bash_calls = sum(row.bash_calls for row in rows)
    # A worker killed by ``side_effect_unknown`` never persists the offending tool result, so its
    # non-zero exit is only visible in the persisted stop message.
    unterminated = sum(
        1
        for row in rows
        if row.stop_code == "side_effect_unknown"
        and row.completed_bash_errors == 0
        and exit_code_of(row.stop_message) is not None
    )
    nonzero = sum(row.bash_nonzero_exits for row in rows) + unterminated

    completed_error_terminations = sum(
        1
        for row in rows
        if row.stop_code == "side_effect_unknown"
        and (row.completed_bash_errors > 0 or exit_code_of(row.stop_message) is not None)
    )
    row = RunRow(
        run_id=str(identity.get("run_id") or run_dir.name),
        sample_id=sample_id,
        repetition=repetition,
        mode=mode,
        success=bool(result.get("success")),
        failure_class=result.get("failure_class"),
        oracle_passed=oracle.get("passed"),
        workers=len(workers),
        workers_success=sum(1 for info in workers.values() if info.get("success")),
        side_effect_unknown_workers=sum(
            1
            for info in workers.values()
            if (info.get("stop_reason") or {}).get("code") == "side_effect_unknown"
        ),
        completed_error_terminations=completed_error_terminations,
        max_turns_workers=sum(
            1
            for info in workers.values()
            if (info.get("stop_reason") or {}).get("code") == "max_turns"
        ),
        bash_calls=bash_calls,
        bash_nonzero_exits=nonzero,
        prompt_tokens=int(metrics.get("prompt_tokens") or 0),
        completion_tokens=int(metrics.get("completion_tokens") or 0),
        model_turns=int(metrics.get("model_turns") or 0),
        tool_calls=int(metrics.get("tool_calls") or 0),
        cost=float(metrics.get("estimated_cost_usd") or 0.0),
        latency_ms=float(metrics.get("latency_ms") or 0.0),
        budget_exceeded=str(result.get("failure_class") or "") == "BUDGET_EXCEEDED",
        budget=dict(metrics.get("budget") or {}),
        run_dir=str(run_dir),
    )
    return row, rows, trajectory_rows(run_dir)


def trajectory_rows(run_dir: Path) -> list[dict[str, Any]]:
    """M4 execution outcomes recorded for the run (tool, state, kind, retry_safe)."""

    records: list[dict[str, Any]] = []
    for path in run_dir.glob("trajectory/*/transcript.jsonl"):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            atlas = (record.get("payload") or {}).get("atlas_trajectory") or {}
            if atlas.get("event_type") != "execution_outcome":
                continue
            data = atlas.get("data") or {}
            records.append(
                {
                    "worker": str((atlas.get("correlation") or {}).get("worker_id") or ""),
                    "tool_id": str(data.get("tool_id")),
                    "state": str(data.get("state")),
                    "completion_kind": str(data.get("completion_kind")),
                    "retry_safe": data.get("retry_safe"),
                }
            )
    return records


def aggregate(rows: list[RunRow]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for mode in ("legacy", "corrected"):
        selected = [row for row in rows if row.mode == mode]
        summary[mode] = {
            "runs": len(selected),
            "oracle_success": sum(1 for row in selected if row.oracle_passed),
            "sample_success": sum(1 for row in selected if row.success),
            "side_effect_unknown_workers": sum(row.side_effect_unknown_workers for row in selected),
            "completed_error_terminations": sum(
                row.completed_error_terminations for row in selected
            ),
            "bash_nonzero_exits": sum(row.bash_nonzero_exits for row in selected),
            "bash_calls": sum(row.bash_calls for row in selected),
            "workers": sum(row.workers for row in selected),
            "worker_success": sum(row.workers_success for row in selected),
            "model_turns": sum(row.model_turns for row in selected),
            "prompt_tokens": sum(row.prompt_tokens for row in selected),
            "completion_tokens": sum(row.completion_tokens for row in selected),
            "tool_calls": sum(row.tool_calls for row in selected),
            "cost_usd": round(sum(row.cost for row in selected), 6),
            "latency_ms": round(sum(row.latency_ms for row in selected), 1),
            "budget_exceeded": sum(1 for row in selected if row.budget_exceeded),
            "max_turns_workers": sum(row.max_turns_workers for row in selected),
            "admission_rejections": sum(
                int((row.budget or {}).get("admission_rejections") or 0) for row in selected
            ),
        }
    summary["failed_checks"] = _failed_checks(rows)
    return summary


def _failed_checks(rows: list[RunRow]) -> dict[str, Counter[str]]:
    failed: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        if row.oracle_passed:
            continue
        for name, value in (
            ((_load(Path(row.run_dir) / "result.json") or {}).get("oracle") or {})
            .get("checks", {})
            .items()
        ):
            if not value:
                failed[row.mode][name] += 1
    return {mode: counts for mode, counts in failed.items()}


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
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
    parser = argparse.ArgumentParser(prog="atlas-stage-g3-1-analysis")
    parser.add_argument("--round", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)

    round_dir: Path = args.round or latest_round()
    output: Path = args.output or (round_dir / "analysis")
    output.mkdir(parents=True, exist_ok=True)

    run_rows: list[RunRow] = []
    worker_rows: list[WorkerRow] = []
    mechanism: list[dict[str, Any]] = []
    for sample_id, repetition, mode, run_dir in arm_runs(round_dir):
        row, workers, trajectory = run_row(run_dir, sample_id, repetition, mode)
        run_rows.append(row)
        worker_rows.extend(workers)
        for worker in workers:
            if worker.completed_bash_errors == 0:
                continue
            mechanism.append(
                {
                    "run_id": worker.run_id,
                    "sample_id": worker.sample_id,
                    "repetition": worker.repetition,
                    "mode": worker.mode,
                    "worker": worker.worker,
                    "completed_bash_errors": worker.completed_bash_errors,
                    "stop_code": worker.stop_code,
                    "continued_after_error": worker.continued_after_error,
                    "repaired_after_error": worker.repaired_after_error,
                }
            )

    write_csv(
        output / "runs.csv",
        [
            {
                **{key: value for key, value in row.__dict__.items() if key not in {"budget"}},
                "admission_rejections": (row.budget or {}).get("admission_rejections"),
                "budget_stop_reason": (row.budget or {}).get("stop_reason"),
            }
            for row in run_rows
        ],
    )
    write_csv(output / "workers.csv", [row.__dict__ for row in worker_rows])
    write_csv(output / "mechanism.csv", mechanism)

    summary = aggregate(run_rows)
    summary["round"] = str(round_dir)
    summary["m4_outcomes"] = dict(
        Counter(
            f"{record['tool_id']}:{record['state']}:{record['completion_kind']}"
            for sample_id, repetition, mode, run_dir in arm_runs(round_dir)
            for record in trajectory_rows(run_dir)
        )
    )
    summary["m4_retry_safe_true_records"] = sum(
        1
        for sample_id, repetition, mode, run_dir in arm_runs(round_dir)
        for record in trajectory_rows(run_dir)
        if record["retry_safe"] is True and record["completion_kind"] == "EXECUTED_ERROR"
    )
    (output / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8"
    )

    print(json.dumps(summary, indent=2, sort_keys=True))
    for row in run_rows:
        print(
            f"{row.sample_id} rep={row.repetition} {row.mode:9s} "
            f"oracle={row.oracle_passed} class={row.failure_class} "
            f"seu_workers={row.side_effect_unknown_workers} "
            f"exec_err_stops={row.completed_error_terminations} "
            f"bash_fail={row.bash_nonzero_exits} turns={row.model_turns} "
            f"cost={row.cost:.4f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
