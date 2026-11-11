"""Stage R1-1 execution-trace integrity summary.

``result.json`` used to be the only action fact source, and it silently dropped every tool call of a
worker that the budget guard stopped (R1-0: 226 of 591 actions). The trajectory transcript already
holds those actions, so the repair is (a) keep the exact tool input in a private corpus and
(b) make the transcript self-describing, then (c) report both sources against each other instead of
trusting one.

The summary is recomputable from a run directory and never mutates it. It reports a status of
``TRACE_COMPLETE`` only when the evidence actually closes:

* a transcript exists,
* the recorder reported no write failures,
* every observed outcome joins a proposed call,
* no call id was proposed twice with a different input digest,
* the run reached a terminal marker (``run_terminal`` or a worker ``stop`` event).
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

TRACE_COMPLETE = "TRACE_COMPLETE"
TRACE_INCOMPLETE = "TRACE_INCOMPLETE"

_PRIVATE_CORPUS = Path("private") / "tool_payloads.jsonl"


def summarize_trace(run_dir: str | Path, *, recorder_failures: int = 0) -> dict[str, Any]:
    """Recompute the R1-1 trace summary for one run directory."""

    root = Path(run_dir)
    events = _read_transcript(root)
    corpus = _read_corpus(root)

    proposals: dict[str, dict[str, Any]] = {}
    duplicate_call_ids = 0
    decisions: list[dict[str, Any]] = []
    outcomes: list[dict[str, Any]] = []
    observations: list[dict[str, Any]] = []
    worker_stops = 0
    terminal = False

    for event in events:
        event_type = event.get("event_type")
        data = event.get("data") or {}
        correlation = event.get("correlation") or {}
        if event_type == "action_proposed":
            call_id = str(data.get("call_id") or correlation.get("call_id") or "")
            digest = str(data.get("input_digest") or "")
            existing = proposals.get(call_id)
            if existing is not None and existing.get("input_digest") != digest:
                duplicate_call_ids += 1
            proposals[call_id] = {
                "input_digest": digest,
                "input_ref": data.get("input_ref"),
                "input_state": data.get("input_state"),
                "worker_id": correlation.get("worker_id"),
                "task_id": correlation.get("task_id"),
                "tool_id": data.get("tool_id"),
            }
        elif event_type == "action_decision":
            decisions.append({"data": data, "correlation": correlation})
        elif event_type == "execution_outcome":
            outcomes.append({"data": data, "correlation": correlation})
        elif event_type == "tool_observation":
            observations.append({"data": data, "correlation": correlation})
        elif event_type == "worker_lifecycle" and data.get("mode") == "stop":
            worker_stops += 1
        elif event_type == "run_terminal":
            terminal = True

    authorized = sum(1 for item in decisions if item["data"].get("decision") == "ALLOW")
    denied = sum(1 for item in decisions if item["data"].get("decision") == "DENY")
    require_human = sum(1 for item in decisions if item["data"].get("decision") == "REQUIRE_HUMAN")
    ownership_denials = sum(
        1
        for item in decisions
        if item["data"].get("decision") == "DENY"
        and any(
            str(code).startswith("OWNERSHIP_") for code in (item["data"].get("reason_codes") or ())
        )
    )
    started = sum(1 for item in outcomes if item["data"].get("state") == "IN_PROGRESS")
    completed = sum(1 for item in outcomes if item["data"].get("state") == "COMPLETED")
    uncertain = sum(1 for item in outcomes if item["data"].get("state") == "UNCERTAIN")
    failed = sum(1 for item in observations if item["data"].get("is_error"))
    observed_calls = {
        str((item["correlation"] or {}).get("call_id") or "") for item in observations
    }
    unmatched_outcomes = len({call for call in observed_calls if call and call not in proposals})
    recorded_inputs = sum(
        1
        for proposal in proposals.values()
        if proposal.get("input_ref") or proposal.get("input_state") == "RECORDED"
    )
    corpus_inputs = sum(
        1
        for proposal in proposals.values()
        if proposal.get("input_ref") and proposal["input_ref"] in corpus
    )
    total = len(proposals)
    coverage = round(recorded_inputs / total, 4) if total else None
    corpus_coverage = round(corpus_inputs / total, 4) if total else None

    complete = bool(
        events
        and recorder_failures == 0
        and unmatched_outcomes == 0
        and duplicate_call_ids == 0
        and (terminal or worker_stops > 0)
    )
    return {
        "status": TRACE_COMPLETE if complete else TRACE_INCOMPLETE,
        "events": len(events),
        "transcript_present": bool(events),
        "recorder_failures": recorder_failures,
        "proposed_actions": total,
        "authorized_actions": authorized,
        "denied_actions": denied,
        "require_human_actions": require_human,
        "ownership_denials": ownership_denials,
        "started_actions": started,
        "completed_actions": completed,
        "failed_actions": failed,
        "unknown_actions": uncertain,
        "observations": len(observations),
        "input_digest_coverage": coverage,
        "corpus_coverage": corpus_coverage,
        "unmatched_outcomes": unmatched_outcomes,
        "duplicate_call_ids": duplicate_call_ids,
        "worker_stops": worker_stops,
        "run_terminal": terminal,
        "workers": _worker_breakdown(proposals),
    }


def compare_with_result(
    summary: dict[str, Any], result_tool_calls: list[dict[str, Any]]
) -> dict[str, Any]:
    """Report what ``result.json`` alone would have lost."""

    # ``result.json`` tool-call records carry no stable call id (see R1-0), so the comparison is
    # by count, not by identity: every record is one action the result file still knew about.
    recorded = len([call for call in result_tool_calls if isinstance(call, dict)])
    proposed = int(summary.get("proposed_actions") or 0)
    return {
        "result_actions": recorded,
        "trajectory_actions": proposed,
        "recovered_from_trajectory": max(0, proposed - recorded),
        "result_is_source_of_truth": False,
    }


def _worker_breakdown(proposals: dict[str, dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for proposal in proposals.values():
        counts[str(proposal.get("worker_id") or "<none>")] += 1
    return dict(sorted(counts.items()))


def _read_transcript(root: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    trajectory = root / "trajectory"
    if not trajectory.is_dir():
        return events
    for path in sorted(trajectory.rglob("transcript.jsonl")):
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = (record.get("payload") or {}).get("atlas_trajectory")
                if isinstance(payload, dict):
                    events.append(payload)
    return events


def _read_corpus(root: Path) -> dict[str, dict[str, Any]]:
    path = root / _PRIVATE_CORPUS
    records: dict[str, dict[str, Any]] = {}
    if not path.is_file():
        return records
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            ref = record.get("ref")
            if isinstance(ref, str):
                records[ref] = record
    return records


__all__ = [
    "TRACE_COMPLETE",
    "TRACE_INCOMPLETE",
    "compare_with_result",
    "summarize_trace",
]
