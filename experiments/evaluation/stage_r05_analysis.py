"""Stage R0.5 analysis: route discrimination, run metrics, and the observed parallel timeline.

Three read-only outputs, none of which touches a run directory:

* a per-run table (cost, tokens, turns, tool calls, oracle, latency),
* a worker timeline built from the evaluation's own ``task_events`` (start/end per runtime task),
* a route audit that separates false admission, false rejection, evidence insufficiency, policy
  constraint and execution failure.

The timeline is the only honest source for "did this actually run in parallel": a DAG can have a
wave width of three while the scheduler starts the workers one after another. When a multi-worker
run shows no overlapping intervals the analysis records ``PARALLEL_EXECUTION_NOT_OBSERVED`` instead
of inferring parallelism from the DAG.
"""

from __future__ import annotations

import argparse
import csv
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from experiments.evaluation.stage_r05_tasks import (
    DEFINITIONS,
    NEGATIVE_IDS,
    POSITIVE_IDS,
    benchmark_digest,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
RESULTS_ROOT = REPO_ROOT / "experiments" / "results" / "stage-r0-5"
PARALLEL_NOT_OBSERVED = "PARALLEL_EXECUTION_NOT_OBSERVED"

_NETWORK_MARKERS = (
    "http://",
    "https://",
    "curl ",
    "wget ",
    "pip install",
    "git clone",
    "uv add",
    "urlopen",
    "requests.get",
)
_ORACLE_MARKERS = ("test_hidden", "hidden_files", "oracle-workspace")


def _load(path: Path) -> Any:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


# --------------------------------------------------------------------------- timeline


@dataclass(frozen=True)
class Interval:
    key: str
    label: str
    agent: str
    start: datetime
    end: datetime | None
    end_source: str

    @property
    def duration_ms(self) -> float | None:
        if self.end is None:
            return None
        return round((self.end - self.start).total_seconds() * 1000.0, 3)


def parse_intervals(evidence: dict[str, Any]) -> list[Interval]:
    """Pair every ``task_start`` with its own completion by runtime task id.

    The evaluation records the runtime task id on every progress event, so a start is paired with
    its own end rather than with "the next completion". Failed tasks emit no ``task_complete``; for
    those the matching ``agent_complete`` is used and the source is recorded.
    """

    events = evidence.get("task_events") or []
    starts: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for event in events:
        if event.get("type") != "task_start" or not event.get("title"):
            continue
        key = str(event.get("task") or event.get("title") or "")
        if key in starts:
            continue
        starts[key] = event
        order.append(key)

    completions: dict[str, str] = {}
    agent_completions: dict[str, str] = {}
    for event in events:
        key = str(event.get("task") or "")
        if event.get("type") == "task_complete" and key:
            completions.setdefault(key, str(event.get("ts") or ""))
        if event.get("type") == "agent_complete" and event.get("agent"):
            agent_completions.setdefault(str(event["agent"]), str(event.get("ts") or ""))

    intervals: list[Interval] = []
    for key in order:
        start_event = starts[key]
        start = _ts(start_event.get("ts"))
        if start is None:
            continue
        agent = str(start_event.get("agent") or start_event.get("assignee") or "")
        end_value = completions.get(key, "")
        end_source = "task_complete"
        if not end_value:
            end_value = agent_completions.get(agent, "")
            end_source = "agent_complete"
        intervals.append(
            Interval(
                key=key,
                label=str(start_event.get("title") or ""),
                agent=agent,
                start=start,
                end=_ts(end_value),
                end_source=end_source if end_value else "missing",
            )
        )
    return intervals


def _overlap_ms(first: Interval, second: Interval) -> float:
    if first.end is None or second.end is None:
        return 0.0
    latest_start = max(first.start, second.start)
    earliest_end = min(first.end, second.end)
    return max(0.0, round((earliest_end - latest_start).total_seconds() * 1000.0, 3))


def timeline_metrics(intervals: list[Interval]) -> dict[str, Any]:
    """Observed concurrency, pairwise overlap and wall time for one run."""

    points: list[tuple[datetime, int]] = []
    for interval in intervals:
        points.append((interval.start, 1))
        if interval.end is not None:
            points.append((interval.end, -1))
    # Ends are processed before starts at the same instant, so a hand-off is not a overlap.
    points.sort(key=lambda item: (item[0], item[1]))
    current = 0
    peak = 0
    for _, delta in points:
        current += delta
        peak = max(peak, current)

    overlapping: list[dict[str, Any]] = []
    for index, first in enumerate(intervals):
        for second in intervals[index + 1 :]:
            overlap = _overlap_ms(first, second)
            if overlap > 0:
                overlapping.append(
                    {
                        "first": first.label,
                        "second": second.label,
                        "overlap_ms": overlap,
                    }
                )

    starts = [item.start for item in intervals]
    ends = [item.end for item in intervals if item.end is not None]
    wall_ms = (
        round((max(ends) - min(starts)).total_seconds() * 1000.0, 3) if starts and ends else None
    )
    durations = [item.duration_ms for item in intervals if item.duration_ms is not None]
    return {
        "worker_intervals": [
            {
                "label": item.label,
                "agent": item.agent,
                "start": item.start.isoformat(),
                "end": item.end.isoformat() if item.end else None,
                "duration_ms": item.duration_ms,
                "end_source": item.end_source,
            }
            for item in intervals
        ],
        "worker_count": len(intervals),
        "max_observed_concurrency": peak,
        "overlapping_worker_intervals": overlapping,
        "overlap_pair_count": len(overlapping),
        "sum_worker_duration_ms": round(sum(durations), 3) if durations else None,
        "total_run_wall_time_ms": wall_ms,
        "marker": PARALLEL_NOT_OBSERVED if peak < 2 else None,
    }


# --------------------------------------------------------------------------- run records


@dataclass(frozen=True)
class RunRecord:
    round_id: str
    track: str
    definition: str
    repetition: int
    arm: str
    run_id: str
    run_dir: str
    policy_version: str | None
    route: str | None
    reasons: tuple[str, ...]
    executed_worker_count: int
    workers_started: int
    workers_succeeded: int
    oracle_passed: bool | None
    task_success: bool
    failure_class: str
    prompt_tokens: int | None
    completion_tokens: int | None
    cache_hit_tokens: int | None
    cache_miss_tokens: int | None
    cost_usd: float | None
    latency_ms: float | None
    model_turns: int
    tool_calls: int
    stop_codes: tuple[str, ...]
    integrity: dict[str, Any] = field(default_factory=dict)
    timeline: dict[str, Any] = field(default_factory=dict)

    @property
    def budget_exceeded(self) -> bool:
        return "budget_exceeded" in self.stop_codes or self.failure_class == "BUDGET_EXCEEDED"

    @property
    def max_turns_reached(self) -> bool:
        return "max_turns" in self.stop_codes

    def as_row(self) -> dict[str, Any]:
        return {
            "round_id": self.round_id,
            "track": self.track,
            "definition": self.definition,
            "repetition": self.repetition,
            "arm": self.arm,
            "run_id": self.run_id,
            "policy_version": self.policy_version or "",
            "route": self.route or "",
            "reasons": ";".join(self.reasons),
            "executed_worker_count": self.executed_worker_count,
            "workers_started": self.workers_started,
            "workers_succeeded": self.workers_succeeded,
            "oracle_passed": self.oracle_passed,
            "task_success": self.task_success,
            "failure_class": self.failure_class,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cache_hit_tokens": self.cache_hit_tokens,
            "cache_miss_tokens": self.cache_miss_tokens,
            "cost_usd": self.cost_usd,
            "latency_ms": self.latency_ms,
            "model_turns": self.model_turns,
            "tool_calls": self.tool_calls,
            "budget_exceeded": self.budget_exceeded,
            "max_turns_reached": self.max_turns_reached,
            "max_observed_concurrency": self.timeline.get("max_observed_concurrency"),
            "overlap_pair_count": self.timeline.get("overlap_pair_count"),
            "sum_worker_duration_ms": self.timeline.get("sum_worker_duration_ms"),
            "total_run_wall_time_ms": self.timeline.get("total_run_wall_time_ms"),
            # A single-worker run cannot overlap with itself; the marker only describes runs whose
            # plan asked for more than one worker.
            "parallel_marker": (self.timeline.get("marker") or "")
            if self.workers_started > 1
            else "",
            "network_markers": self.integrity.get("network_markers", 0),
            "oracle_markers": self.integrity.get("oracle_markers", 0),
            "integrity_passed": self.integrity.get("isolation_passed"),
        }


def _tool_text(result: dict[str, Any]) -> str:
    parts: list[str] = []
    for call in result.get("tool_calls") or []:
        parts.append(json.dumps(call.get("input") or {}, sort_keys=True))
        parts.append(str(call.get("output") or ""))
    return "\n".join(parts).lower()


def _integrity(result: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
    text = _tool_text(result) + "\n" + _tool_text(evidence).lower()
    integrity = (result.get("metrics") or {}).get("integrity") or {}
    passed = (
        integrity.get("isolation_checks_passed", integrity.get("passed")) if integrity else None
    )
    return {
        "network_markers": sum(text.count(marker) for marker in _NETWORK_MARKERS),
        "oracle_markers": sum(text.count(marker) for marker in _ORACLE_MARKERS),
        "isolation_passed": passed,
    }


def load_run(
    run_dir: Path, *, round_id: str, track: str, definition: str, arm: str, repetition: int
) -> RunRecord:
    result = _load(run_dir / "result.json")
    evidence = _load(run_dir / "stage_e_evidence.json")
    metrics = result.get("metrics") or {}
    routing = metrics.get("routing") or {}
    decision = routing.get("decision") or {}
    worker_results = result.get("worker_results") or {}
    stop_codes = tuple(
        sorted(
            {
                str((value.get("stop_reason") or {}).get("code") or "unknown")
                for value in worker_results.values()
            }
        )
    )
    intervals = parse_intervals(evidence)
    return RunRecord(
        round_id=round_id,
        track=track,
        definition=definition,
        repetition=repetition,
        arm=arm,
        run_id=str((result.get("identity") or {}).get("run_id") or run_dir.name),
        run_dir=str(run_dir),
        policy_version=decision.get("policy_version"),
        route=decision.get("route"),
        reasons=tuple(decision.get("reason_codes") or ()),
        # The routing record exists only for the selective arm; the forced arms executed the plan
        # they were given, so the number of started workers is the number of executed workers.
        executed_worker_count=int(routing.get("executed_worker_count") or len(worker_results)),
        workers_started=len(worker_results),
        workers_succeeded=sum(1 for value in worker_results.values() if bool(value.get("success"))),
        oracle_passed=(result.get("oracle") or {}).get("passed"),
        task_success=str(result.get("failure_class")) == "TASK_SUCCESS",
        failure_class=str(result.get("failure_class") or "UNKNOWN"),
        prompt_tokens=metrics.get("prompt_tokens"),
        completion_tokens=metrics.get("completion_tokens"),
        cache_hit_tokens=metrics.get("cache_hit_tokens"),
        cache_miss_tokens=metrics.get("cache_miss_tokens"),
        cost_usd=metrics.get("estimated_cost_usd"),
        latency_ms=metrics.get("latency_ms"),
        model_turns=int(metrics.get("model_turns") or 0),
        tool_calls=int(metrics.get("tool_calls") or 0),
        stop_codes=stop_codes,
        integrity=_integrity(result, evidence),
        timeline=timeline_metrics(intervals),
    )


def collect_runs(root: Path = RESULTS_ROOT) -> list[RunRecord]:
    """Walk every ``r05-*`` round manifest and load the runs it references.

    A round whose recorded family digest no longer matches the frozen definition is skipped
    entirely: its family was edited after it ran, so its runs belong to an earlier benchmark
    version. Track B rounds have no positive digest and are always included.
    """

    frozen = {definition.sample_id: benchmark_digest(definition) for definition in DEFINITIONS}
    records: list[RunRecord] = []
    for manifest_path in sorted(root.glob("r05-*/manifest.json")):
        manifest = _load(manifest_path)
        track = str(manifest.get("track") or "")
        round_id = str(manifest.get("round_id") or manifest_path.parent.name)
        digests = manifest.get("positive_digests") or {}
        for pair in manifest.get("pairs") or []:
            definition_id = str(pair.get("definition") or "")
            recorded = digests.get(definition_id)
            if recorded and frozen.get(definition_id) and recorded != frozen[definition_id]:
                continue
            for arm, payload in sorted((pair.get("runs") or {}).items()):
                run_dir = Path(str(payload.get("run_dir") or ""))
                if not run_dir.is_dir():
                    continue
                records.append(
                    load_run(
                        run_dir,
                        round_id=round_id,
                        track=track,
                        definition=definition_id,
                        arm=arm,
                        repetition=int(pair.get("repetition") or 0),
                    )
                )
    return records


# --------------------------------------------------------------------------- aggregation


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def arm_summary(records: list[RunRecord]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for key in sorted({(record.track, record.arm) for record in records}):
        track, arm = key
        group = [record for record in records if record.track == track and record.arm == arm]
        summary[f"{track}:{arm}"] = {
            "runs": len(group),
            "oracle_pass": sum(1 for record in group if record.oracle_passed),
            "task_success": sum(1 for record in group if record.task_success),
            "worker_success": sum(record.workers_succeeded for record in group),
            "workers_started": sum(record.workers_started for record in group),
            "prompt_tokens": sum(record.prompt_tokens or 0 for record in group),
            "completion_tokens": sum(record.completion_tokens or 0 for record in group),
            "cache_hit_tokens": sum(record.cache_hit_tokens or 0 for record in group),
            "cache_miss_tokens": sum(record.cache_miss_tokens or 0 for record in group),
            "cost_usd": round(sum(record.cost_usd or 0.0 for record in group), 6),
            "cost_per_run": _mean([record.cost_usd or 0.0 for record in group]),
            "model_turns": sum(record.model_turns for record in group),
            "tool_calls": sum(record.tool_calls for record in group),
            "budget_exceeded": sum(1 for record in group if record.budget_exceeded),
            "max_turns_reached": sum(1 for record in group if record.max_turns_reached),
            "latency_ms_mean": _mean([record.latency_ms or 0.0 for record in group]),
            "wall_time_ms_mean": _mean(
                [record.timeline.get("total_run_wall_time_ms") or 0.0 for record in group]
            ),
            "max_observed_concurrency": max(
                (record.timeline.get("max_observed_concurrency") or 0 for record in group),
                default=0,
            ),
            "runs_with_overlap": sum(
                1 for record in group if (record.timeline.get("overlap_pair_count") or 0) > 0
            ),
            "oracle_failures": sorted(
                {
                    f"{record.failure_class}:{';'.join(record.reasons)}"
                    for record in group
                    if not record.oracle_passed
                }
            ),
        }
    return summary


def per_task_summary(records: list[RunRecord]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for key in sorted({(record.track, record.definition) for record in records}):
        track, definition = key
        group = [
            record
            for record in records
            if record.track == track and record.definition == definition
        ]
        entry: dict[str, Any] = {"track": track, "definition": definition, "arms": {}}
        for arm in sorted({record.arm for record in group}):
            runs = [record for record in group if record.arm == arm]
            entry["arms"][arm] = {
                "runs": len(runs),
                "oracle_pass": sum(1 for record in runs if record.oracle_passed),
                "cost_usd": round(sum(record.cost_usd or 0.0 for record in runs), 6),
                "cost_per_run": _mean([record.cost_usd or 0.0 for record in runs]),
                "latency_ms": [record.latency_ms for record in runs],
                "wall_time_ms": [record.timeline.get("total_run_wall_time_ms") for record in runs],
                "worker_count": [record.workers_started for record in runs],
                "max_concurrency": [
                    record.timeline.get("max_observed_concurrency") for record in runs
                ],
                "routes": sorted({str(record.route) for record in runs}),
                "reasons": sorted({reason for record in runs for reason in record.reasons}),
                "failure_classes": sorted({record.failure_class for record in runs}),
            }
        summary[f"{track}:{definition}"] = entry
    return summary


def paired_deltas(records: list[RunRecord]) -> list[dict[str, Any]]:
    """A2-A0, A2-A1 and A1-A0 for every (track, definition, repetition)."""

    rows: list[dict[str, Any]] = []
    keys = sorted({(record.track, record.definition, record.repetition) for record in records})
    by_key: dict[tuple[str, str, int], dict[str, RunRecord]] = {}
    for record in records:
        by_key.setdefault((record.track, record.definition, record.repetition), {})[record.arm] = (
            record
        )
    for key in keys:
        arms = by_key[key]
        for left, right in (("a2", "a0"), ("a2", "a1"), ("a1", "a0")):
            if left not in arms or right not in arms:
                continue
            first, second = arms[left], arms[right]
            rows.append(
                {
                    "track": key[0],
                    "definition": key[1],
                    "repetition": key[2],
                    "comparison": f"{left}-{right}",
                    "oracle_delta": int(bool(first.oracle_passed))
                    - int(bool(second.oracle_passed)),
                    "cost_delta_usd": round((first.cost_usd or 0.0) - (second.cost_usd or 0.0), 6),
                    "prompt_token_delta": (first.prompt_tokens or 0) - (second.prompt_tokens or 0),
                    "turn_delta": first.model_turns - second.model_turns,
                    "tool_call_delta": first.tool_calls - second.tool_calls,
                    "latency_delta_ms": round(
                        (first.latency_ms or 0.0) - (second.latency_ms or 0.0), 3
                    ),
                    "left_route": first.route,
                    "right_route": second.route,
                }
            )
    return rows


# --------------------------------------------------------------------------- route audit


def route_audit(records: list[RunRecord]) -> dict[str, Any]:
    """Offline discrimination plus the live A2 outcome, kept deliberately separate."""

    from experiments.evaluation.stage_r05_routing import audit_all

    offline = audit_all()
    positives = [row for row in offline["rows"] if row["family"].startswith("P")]
    negatives = [row for row in offline["rows"] if row["family"].startswith("N")]
    live_positive = [record for record in records if record.track == "a" and record.arm == "a2"]
    live_rejected = [record for record in live_positive if record.route != "delegate_parallel"]
    evidence_gap = [
        record
        for record in live_rejected
        if set(record.reasons)
        & {
            "UNSTABLE_INTERFACE",
            "CONTEXT_INSUFFICIENT",
            "SEMANTIC_COUPLING_UNKNOWN",
            "UNCOVERED_GOAL",
            "INSUFFICIENT_EVIDENCE",
        }
    ]
    policy_blocked = [
        record for record in live_rejected if "PARALLEL_LIMIT_EXCEEDED" in record.reasons
    ]
    return {
        "offline": offline,
        "positive_admission_count": sum(
            1 for record in live_positive if record.route == "delegate_parallel"
        ),
        "positive_rejection_count": len(live_rejected),
        "positive_runs": len(live_positive),
        "negative_admission_count": sum(1 for row in negatives if row["actual_route"] != "direct"),
        "negative_rejection_count": sum(1 for row in negatives if row["actual_route"] == "direct"),
        "unknown_evidence_count": offline["summary"]["unknown_evidence_count"],
        "false_admission": [
            row["sample_id"] for row in negatives if row["actual_route"] != "direct"
        ],
        "false_rejection": [
            row["sample_id"] for row in positives if row["actual_route"] == "direct"
        ],
        "evidence_insufficiency": [row["sample_id"] for row in evidence_gap],
        "policy_constraint": sorted({row["definition"] for row in policy_blocked}),
        "execution_failure": sorted(
            {
                record.definition
                for record in records
                if record.route == "delegate_parallel"
                and record.task_success is False
                and record.failure_class not in {"TASK_SUCCESS"}
            }
        ),
        "expected_positive": list(POSITIVE_IDS),
        "expected_negative": list(NEGATIVE_IDS),
    }


def parallel_evidence(records: list[RunRecord]) -> dict[str, Any]:
    """Separate theoretical width, scheduler starts, observed overlap and end-to-end latency."""

    rows: list[dict[str, Any]] = []
    for record in records:
        rows.append(
            {
                "run_id": record.run_id,
                "track": record.track,
                "definition": record.definition,
                "arm": record.arm,
                "route": record.route,
                "dag_width": None,
                "scheduler_started": record.workers_started,
                "observed_concurrency": record.timeline.get("max_observed_concurrency"),
                "overlap_pairs": record.timeline.get("overlap_pair_count"),
                "sum_worker_ms": record.timeline.get("sum_worker_duration_ms"),
                "wall_ms": record.timeline.get("total_run_wall_time_ms"),
                "latency_ms": record.latency_ms,
                "marker": record.timeline.get("marker") if record.workers_started > 1 else None,
            }
        )
    multi = [row for row in rows if (row["scheduler_started"] or 0) > 1]
    return {
        "rows": rows,
        "multi_worker_runs": len(multi),
        "multi_worker_with_overlap": sum(1 for row in multi if (row["overlap_pairs"] or 0) > 0),
        "not_observed": [row["run_id"] for row in multi if (row["observed_concurrency"] or 0) < 2],
        "provider_overlap": "INFERRED_FROM_WORKER_INTERVALS",
        "dag_width_note": (
            "wave width is recorded in the routing decision; the scheduler's actual starts and the "
            "observed overlap are measured separately"
        ),
    }


def version_reconciliation(root: Path = RESULTS_ROOT) -> dict[str, Any]:
    """Prove that every analysed run used the currently frozen definition of its family.

    A round records the digests it executed. Comparing them with the frozen definitions is what
    makes it possible to reuse runs from an earlier pre-registration after a fix that provably did
    not touch their family — and what catches a run that silently used an older fixture.
    """

    frozen = {definition.sample_id: benchmark_digest(definition) for definition in DEFINITIONS}
    rows: list[dict[str, Any]] = []
    for manifest_path in sorted(root.glob("r05-*/manifest.json")):
        manifest = _load(manifest_path)
        digests = manifest.get("positive_digests") or {}
        for sample_id, digest in sorted(digests.items()):
            rows.append(
                {
                    "round_id": manifest.get("round_id"),
                    "benchmark_version": manifest.get("benchmark_version"),
                    "sample_id": sample_id,
                    "digest": digest,
                    "frozen_digest": frozen.get(sample_id),
                    "matches": digest == frozen.get(sample_id),
                }
            )
    return {
        "rows": rows,
        "versions": sorted({str(row["benchmark_version"]) for row in rows}),
        "mismatched_families": sorted({row["sample_id"] for row in rows if not row["matches"]}),
        "excluded_families": sorted({row["sample_id"] for row in rows if not row["matches"]}),
        "track_b_note": (
            "Track B runs Stage E1 tasks; their fixture digests are pinned by the Stage E1 tests "
            "and are unaffected by the R0.5 benchmark version"
        ),
    }


def summarize(root: Path = RESULTS_ROOT) -> dict[str, Any]:
    records = collect_runs(root)
    return {
        "root": str(root),
        "run_count": len(records),
        "versions": version_reconciliation(root),
        "arms": arm_summary(records),
        "tasks": per_task_summary(records),
        "paired": paired_deltas(records),
        "routes": route_audit(records),
        "parallel": parallel_evidence(records),
        "integrity": {
            "network_marker_runs": sum(
                1 for record in records if record.integrity.get("network_markers")
            ),
            "oracle_marker_runs": sum(
                1 for record in records if record.integrity.get("oracle_markers")
            ),
            "isolation_failures": sum(
                1 for record in records if record.integrity.get("isolation_passed") is False
            ),
        },
        "runs": [record.as_row() for record in records],
    }


def write_outputs(summary: dict[str, Any], root: Path = RESULTS_ROOT) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8"
    )
    rows = summary["runs"]
    if rows:
        with (root / "runs.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
    (root / "summary.md").write_text(render_markdown(summary), encoding="utf-8")


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    lines.extend(
        "| " + " | ".join("" if item is None else str(item) for item in row) + " |" for row in rows
    )
    return "\n".join(lines)


def render_markdown(summary: dict[str, Any]) -> str:
    arm_headers = [
        "Arm",
        "Runs",
        "Oracle",
        "TASK_SUCCESS",
        "Prompt",
        "Completion",
        "Cache hit",
        "Cost USD",
        "$/run",
        "Turns",
        "Tools",
        "Budget",
        "MaxTurns",
        "Latency ms",
        "Wall ms",
        "Cmax",
        "Overlap runs",
    ]
    arm_rows: list[list[Any]] = []
    for key, value in summary["arms"].items():
        arm_rows.append(
            [
                key,
                value["runs"],
                value["oracle_pass"],
                value["task_success"],
                value["prompt_tokens"],
                value["completion_tokens"],
                value["cache_hit_tokens"],
                value["cost_usd"],
                value["cost_per_run"],
                value["model_turns"],
                value["tool_calls"],
                value["budget_exceeded"],
                value["max_turns_reached"],
                value["latency_ms_mean"],
                value["wall_time_ms_mean"],
                value["max_observed_concurrency"],
                value["runs_with_overlap"],
            ]
        )
    parallel_headers = [
        "Definition",
        "Arm",
        "Route",
        "Started",
        "Cmax",
        "Overlaps",
        "Sum worker ms",
        "Wall ms",
        "Marker",
    ]
    parallel_rows = [
        [
            row["definition"],
            row["arm"],
            row["route"],
            row["scheduler_started"],
            row["observed_concurrency"],
            row["overlap_pairs"],
            row["sum_worker_ms"],
            row["wall_ms"],
            row["marker"] or "",
        ]
        for row in summary["parallel"]["rows"]
    ]
    audit = summary["routes"]
    versions = summary["versions"]
    lines = [
        "# Stage R0.5 automatically generated summary",
        "",
        f"Runs: {summary['run_count']}",
        f"Pre-registration versions in use: {', '.join(versions['versions'])}",
        f"Families whose runs used a stale definition: {versions['mismatched_families'] or 'none'}",
        "",
        "## Arms",
        "",
        _table(arm_headers, arm_rows),
        "",
        "## Parallelism (observed)",
        "",
        f"Multi-worker runs: {summary['parallel']['multi_worker_runs']}, with observed overlap: "
        f"{summary['parallel']['multi_worker_with_overlap']}",
        "",
        _table(parallel_headers, parallel_rows),
        "",
        "## Router discrimination",
        "",
        f"* positive admission (live A2): {audit['positive_admission_count']}/"
        f"{audit['positive_runs']}",
        f"* positive rejection (live A2): {audit['positive_rejection_count']}",
        f"* negative admission (offline): {audit['negative_admission_count']}",
        f"* negative rejection (offline): {audit['negative_rejection_count']}",
        f"* false admission: {audit['false_admission']}",
        f"* false rejection: {audit['false_rejection']}",
        f"* evidence insufficiency: {audit['evidence_insufficiency']}",
        f"* policy constraint: {audit['policy_constraint']}",
        f"* execution failure: {audit['execution_failure']}",
        "",
        "## Integrity",
        "",
        f"* runs with network-looking tool calls: {summary['integrity']['network_marker_runs']}",
        f"* runs mentioning oracle artifacts: {summary['integrity']['oracle_marker_runs']}",
        f"* isolation failures: {summary['integrity']['isolation_failures']}",
    ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="atlas-stage-r05-analysis")
    parser.add_argument("--root", default=str(RESULTS_ROOT))
    parser.add_argument("--write", action="store_true", help="write summary.json/csv/md")
    args = parser.parse_args(argv)
    summary = summarize(Path(args.root))
    if args.write:
        write_outputs(summary, Path(args.root))
    print(render_markdown(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
