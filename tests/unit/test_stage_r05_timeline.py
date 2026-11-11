"""Stage R0.5: the parallel timeline is measured, never inferred from the DAG.

A wave width of two proves that two workers *may* run together. These tests pin the only claim the
report is allowed to make: two intervals overlap only when their measured wall-clock windows
overlap, and a serialised multi-worker run is labelled ``PARALLEL_EXECUTION_NOT_OBSERVED``.
"""

from __future__ import annotations

from experiments.evaluation.stage_r05_analysis import (
    PARALLEL_NOT_OBSERVED,
    parse_intervals,
    timeline_metrics,
)


def _start(task: str, title: str, ts: str, agent: str = "") -> dict[str, object]:
    return {"type": "task_start", "task": task, "title": title, "ts": ts, "agent": agent}


def _complete(task: str, ts: str) -> dict[str, object]:
    return {"type": "task_complete", "task": task, "ts": ts}


def _agent_complete(agent: str, ts: str) -> dict[str, object]:
    return {"type": "agent_complete", "agent": agent, "ts": ts}


def test_paired_events_report_real_overlap() -> None:
    evidence = {
        "task_events": [
            _start("t1", "run-a", "2026-11-11T10:00:00+00:00", "coding_worker:a"),
            _start("t2", "run-b", "2026-11-11T10:00:01+00:00", "coding_worker:b"),
            _complete("t2", "2026-11-11T10:00:30+00:00"),
            _complete("t1", "2026-11-11T10:00:40+00:00"),
        ]
    }
    metrics = timeline_metrics(parse_intervals(evidence))
    assert metrics["worker_count"] == 2
    assert metrics["max_observed_concurrency"] == 2
    assert metrics["overlap_pair_count"] == 1
    assert metrics["overlapping_worker_intervals"][0]["overlap_ms"] == 29_000.0
    assert metrics["sum_worker_duration_ms"] == 69_000.0
    assert metrics["total_run_wall_time_ms"] == 40_000.0
    assert metrics["marker"] is None


def test_serialised_workers_are_not_reported_as_parallel() -> None:
    evidence = {
        "task_events": [
            _start("t1", "run-a", "2026-11-11T10:00:00+00:00"),
            _complete("t1", "2026-11-11T10:00:20+00:00"),
            _start("t2", "run-b", "2026-11-11T10:00:20+00:00"),
            _complete("t2", "2026-11-11T10:00:35+00:00"),
        ]
    }
    metrics = timeline_metrics(parse_intervals(evidence))
    assert metrics["max_observed_concurrency"] == 1
    assert metrics["overlap_pair_count"] == 0
    assert metrics["marker"] == PARALLEL_NOT_OBSERVED


def test_events_are_paired_by_runtime_task_id_not_by_order() -> None:
    """A completion is matched to its own start even when the start order is reversed."""

    evidence = {
        "task_events": [
            _start("t2", "run-b", "2026-11-11T10:00:00+00:00"),
            _start("t1", "run-a", "2026-11-11T10:00:05+00:00"),
            _complete("t1", "2026-11-11T10:00:06+00:00"),
            _complete("t2", "2026-11-11T10:00:40+00:00"),
        ]
    }
    intervals = {item.label: item for item in parse_intervals(evidence)}
    assert intervals["run-a"].duration_ms == 1_000.0
    assert intervals["run-b"].duration_ms == 40_000.0


def test_failed_task_falls_back_to_its_agent_completion() -> None:
    evidence = {
        "task_events": [
            _start("t1", "run-a", "2026-11-11T10:00:00+00:00", "coding_worker:a"),
            _agent_complete("coding_worker:a", "2026-11-11T10:00:12+00:00"),
        ]
    }
    interval = parse_intervals(evidence)[0]
    assert interval.end_source == "agent_complete"
    assert interval.duration_ms == 12_000.0


def test_missing_completion_stays_unmeasured() -> None:
    evidence = {
        "task_events": [_start("t1", "run-a", "2026-11-11T10:00:00+00:00", "coding_worker:a")]
    }
    interval = parse_intervals(evidence)[0]
    assert interval.end is None
    assert interval.end_source == "missing"
    metrics = timeline_metrics([interval])
    assert metrics["max_observed_concurrency"] == 1
    assert metrics["total_run_wall_time_ms"] is None


def test_worker_events_without_titles_are_ignored() -> None:
    """The router's own ``task_start`` event carries no task title and must not be counted."""

    evidence = {
        "task_events": [
            {"type": "task_start", "task": "", "title": None, "ts": "2026-11-11T10:00:00+00:00"},
            _start("t1", "run-a", "2026-11-11T10:00:01+00:00"),
            _complete("t1", "2026-11-11T10:00:03+00:00"),
        ]
    }
    assert len(parse_intervals(evidence)) == 1
