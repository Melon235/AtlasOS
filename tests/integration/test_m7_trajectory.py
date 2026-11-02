"""M7 trajectory schema, reader dedupe/conflict, and evidence-only reconciliation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from anycode.runstore.store import FilesystemRunStore
from pydantic import ValidationError
from tests.helpers.m6_harness import M6World

from atlasos.trajectory.codec import (
    UnsupportedTrajectorySchema,
    decode_envelope,
    encode_envelope,
)
from atlasos.trajectory.models import (
    ActionProposedData,
    TrajectoryCorrelation,
    TrajectoryEnvelope,
    derive_event_id,
)
from atlasos.trajectory.reader import TrajectoryReader
from atlasos.trajectory.reconcile import TrajectoryReconciler
from atlasos.trajectory.recorder import NoopTrajectoryRecorder, TrajectoryRecorder


class _DuplicateAppendStore(FilesystemRunStore):
    """Simulates a transport that writes the same evidence payload twice."""

    def append_event(
        self,
        run_id: str,
        kind: Any,
        payload: dict[str, object] | None = None,
    ) -> Any:
        super().append_event(run_id, kind, payload)
        return super().append_event(run_id, kind, payload)


def _proposed(
    *,
    run_id: str = "run-m6",
    call_id: str = "call-1",
    tool_name: str = "file_read",
    input_digest: str = "a" * 64,
    backfilled: bool = False,
) -> TrajectoryEnvelope:
    event_id = derive_event_id(
        event_type="action_proposed",
        run_id=run_id,
        identity={
            "model_response_id": "resp-1",
            "ordinal": 0,
            "call_id": call_id,
            "tool_id": "file_read",
            "input_digest": input_digest,
        },
    )
    return TrajectoryEnvelope(
        event_id=event_id,
        run_id=run_id,
        event_type="action_proposed",
        source="model",
        backfilled=backfilled,
        correlation=TrajectoryCorrelation(
            model_response_id="resp-1",
            ordinal=0,
            call_id=call_id,
            tool_id="file_read",
        ),
        data=ActionProposedData(
            model_response_id="resp-1",
            ordinal=0,
            call_id=call_id,
            tool_id="file_read",
            tool_name=tool_name,
            input_digest=input_digest,
        ).model_dump(mode="json"),
    )


def test_m7_schema_roundtrip_frozen_invalid_and_unknown() -> None:
    envelope = _proposed()
    payload = encode_envelope(envelope)
    decoded = decode_envelope(payload, seq=1)
    assert decoded == envelope
    with pytest.raises(ValidationError):
        envelope.event_type = "execution_outcome"  # type: ignore[misc]
    broken = dict(payload["atlas_trajectory"])
    broken.pop("event_id")
    with pytest.raises(ValidationError):
        decode_envelope({"atlas_trajectory": broken}, seq=1)
    unknown = dict(payload["atlas_trajectory"])
    unknown["schema_version"] = 99
    with pytest.raises(UnsupportedTrajectorySchema):
        decode_envelope({"atlas_trajectory": unknown}, seq=1)


def test_m7_reader_reports_unknown_schema_without_raising(tmp_path: Path) -> None:
    store = FilesystemRunStore(tmp_path / "trajectory")
    recorder = TrajectoryRecorder(store, "run-m6")
    recorder.record_action_proposed(
        model_response_id="resp-1",
        ordinal=0,
        call_id="call-1",
        tool_id="file_read",
        tool_name="file_read",
        input_digest="a" * 64,
    )
    store.append_event(
        "run-m6",
        "lifecycle",
        {"atlas_trajectory": {"schema_version": 99, "event_type": "action_proposed"}},
    )
    view = TrajectoryReader(store).read("run-m6")
    assert view.metrics.logical_events == 1
    assert view.metrics.decode_failures == 1
    assert view.decode_failures[0].seq == 2
    assert [event.seq for event in view.events] == [1]


def test_m7_same_occurrence_duplicate_write_dedupes(tmp_path: Path) -> None:
    store = _DuplicateAppendStore(tmp_path / "trajectory")
    recorder = TrajectoryRecorder(store, "run-m6")
    occurrence_id = "settlement:sus-1:9"
    recorder.record_action_decision(
        phase="resume_revalidation",
        decision="ALLOW",
        decision_source="policy",
        tool_id="email.send",
        call_id="call-1",
        occurrence_id=occurrence_id,
        policy_rule_id="allow-communication",
        decision_basis_digest="b" * 64,
        action_digest="a" * 64,
        input_digest="i" * 64,
        tool_binding_digest="t" * 64,
        reason_codes=(),
        scope_alignment="within_scope",
        reviewer_verdict=None,
        external_policy_outcome=None,
        correlation=TrajectoryCorrelation(
            call_id="call-1",
            tool_id="email.send",
            occurrence_id=occurrence_id,
        ),
    )
    view = TrajectoryReader(store).read("run-m6")
    decisions = [event for event in view.events if event.event_type == "action_decision"]
    assert view.metrics.physical_events == 2
    assert view.metrics.logical_events == 1
    assert view.metrics.evidence_conflicts == 0
    assert len(decisions) == 1
    assert decisions[0].correlation.occurrence_id == occurrence_id
    assert decisions[0].physical_refs == (1, 2)


def test_m7_reader_projects_native_observation_and_suppresses_atlas_duplicate(
    tmp_path: Path,
) -> None:
    store = FilesystemRunStore(tmp_path / "trajectory")
    store.create_run("run-m6", agent_name="test", model="fake")
    store.append_event(
        "run-m6",
        "message",
        {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
    )
    store.append_event(
        "run-m6",
        "tool_result",
        {
            "tool_use_id": "call-1",
            "tool_name": "file_read",
            "output": "hello",
            "is_error": False,
        },
    )
    recorder = TrajectoryRecorder(store, "run-m6")
    recorder.record_tool_observation(
        call_id="call-1",
        tool_id="file_read",
        is_error=False,
        result_digest="d" * 64,
        result_size=5,
        correlation=TrajectoryCorrelation(call_id="call-1", tool_id="file_read"),
    )
    view = TrajectoryReader(store).read("run-m6")
    messages = [event for event in view.events if event.event_type == "model_message"]
    assert len(messages) == 1
    assert messages[0].source == "anycode"
    observations = [event for event in view.events if event.event_type == "tool_observation"]
    assert len(observations) == 1
    assert observations[0].source == "anycode"
    assert observations[0].correlation.call_id == "call-1"
    assert view.metrics.native_tool_observations_duplicated == 1


def test_m7_reader_logical_dedupe_prefers_lowest_sequence(tmp_path: Path) -> None:
    store = FilesystemRunStore(tmp_path / "trajectory")
    recorder = TrajectoryRecorder(store, "run-m6")
    for _ in range(2):
        recorder.record_action_proposed(
            model_response_id="resp-1",
            ordinal=0,
            call_id="call-1",
            tool_id="file_read",
            tool_name="file_read",
            input_digest="a" * 64,
        )
    view = TrajectoryReader(store).read("run-m6")
    assert view.metrics.physical_events == 2
    assert view.metrics.logical_events == 1
    assert view.events[0].seq == 1
    assert view.events[0].physical_refs == (1, 2)
    assert view.metrics.evidence_conflicts == 0


def test_m7_reader_prefers_non_backfilled_evidence(tmp_path: Path) -> None:
    store = FilesystemRunStore(tmp_path / "trajectory")
    store.create_run("run-m6", agent_name="test", model="fake")
    backfilled = _proposed(backfilled=True)
    live = _proposed(backfilled=False)
    store.append_event("run-m6", "lifecycle", encode_envelope(backfilled))
    store.append_event("run-m6", "lifecycle", encode_envelope(live))
    view = TrajectoryReader(store).read("run-m6")
    assert view.metrics.physical_events == 2
    assert view.metrics.logical_events == 1
    assert view.events[0].seq == 2
    assert view.events[0].backfilled is False
    assert view.events[0].physical_refs == (1, 2)


def test_m7_reader_reports_evidence_conflict_without_last_write_wins(
    tmp_path: Path,
) -> None:
    store = FilesystemRunStore(tmp_path / "trajectory")
    store.create_run("run-m6", agent_name="test", model="fake")
    first = _proposed(tool_name="file_read")
    second = _proposed(tool_name="email.send")
    assert first.event_id == second.event_id
    store.append_event("run-m6", "lifecycle", encode_envelope(first))
    store.append_event("run-m6", "lifecycle", encode_envelope(second))
    view = TrajectoryReader(store).read("run-m6")
    assert view.metrics.logical_events == 0
    assert view.metrics.evidence_conflicts == 1
    assert view.conflicts[0].event_id == first.event_id
    assert view.conflicts[0].seqs == (1, 2)


def test_m7_reconciler_backfills_execution_outcome_from_m4(tmp_path: Path) -> None:
    world = M6World(tmp_path / "world")
    runtime = world.runtime()
    assert runtime.durable_bridge is not None
    # Execute one M4 action without a trajectory recorder: durable authority exists, evidence
    # does not. Reconciliation must repair evidence only.
    result = asyncio_run(
        runtime.durable_bridge.execute(
            "email.send",
            {"to": "ops@example.test", "body": "x"},
            world.context(),
            idempotency_key="reconcile-key",
        )
    )
    assert result.is_error is not True
    ledger = runtime.action_ledger
    assert ledger is not None and len(ledger.list()) == 1
    store = FilesystemRunStore(tmp_path / "trajectory")
    recorder = TrajectoryRecorder(store, "run-m6")
    reader = TrajectoryReader(store)
    reconciler = TrajectoryReconciler(
        reader=reader,
        recorder=recorder,
        ledger=ledger,
        approvals=runtime.approval_store,
        suspensions=runtime.suspension_store,
    )
    report = reconciler.reconcile("run-m6")
    assert report.appended_count == 1
    assert report.consistency.failures == 0
    assert report.consistency.unknown == 0
    view = reader.read("run-m6")
    outcomes = [event for event in view.events if event.event_type == "execution_outcome"]
    assert len(outcomes) == 1
    assert outcomes[0].backfilled is True
    assert outcomes[0].correlation.action_id == ledger.list()[0].action_id
    assert world.provider.effects == 1


def test_m7_reconciler_reports_unreconstructable_m3_gap(tmp_path: Path) -> None:
    store = FilesystemRunStore(tmp_path / "trajectory")
    recorder = TrajectoryRecorder(store, "run-m6")
    recorder.record_action_proposed(
        model_response_id="resp-1",
        ordinal=0,
        call_id="call-1",
        tool_id="file_read",
        tool_name="file_read",
        input_digest="a" * 64,
    )
    reader = TrajectoryReader(store)
    report = TrajectoryReconciler(reader=reader, recorder=recorder).reconcile("run-m6")
    assert report.appended_count == 0
    assert any(gap.kind == "missing_action_decision" for gap in report.gaps)
    view = reader.read("run-m6")
    assert [event.event_type for event in view.events] == ["action_proposed"]


def test_m7_run_terminal_is_metadata_only(tmp_path: Path) -> None:
    store = FilesystemRunStore(tmp_path / "trajectory")
    recorder = TrajectoryRecorder(store, "run-m6")
    recorder.record_run_terminal(
        status="succeeded",
        task_count=1,
        final_output_digest="d" * 64,
        final_output_size=8,
        occurred_at=None,
    )
    view = TrajectoryReader(store).read("run-m6")
    terminal = [event for event in view.events if event.event_type == "run_terminal"]
    assert len(terminal) == 1
    assert terminal[0].data["status"] == "succeeded"
    assert terminal[0].data["task_count"] == 1
    assert "output" not in terminal[0].data


def test_m7_noop_recorder_never_writes(tmp_path: Path) -> None:
    recorder = NoopTrajectoryRecorder()
    recorder.record_action_proposed(
        model_response_id="r",
        ordinal=0,
        call_id="c",
        tool_id="t",
        tool_name="t",
        input_digest="d",
    )
    assert list(tmp_path.iterdir()) == []


def asyncio_run(coro):
    import asyncio

    return asyncio.run(coro)
