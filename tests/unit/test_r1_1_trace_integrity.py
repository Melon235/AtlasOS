"""Stage R1-1: execution-trace integrity (T10-T12).

The scenarios are deterministic: a recorder writes a real transcript, the summary recomputes it,
and the assertions check that evidence survives a worker that never returns a result.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from anycode.identity.context import ExecutionContext
from anycode.runstore.store import FilesystemRunStore
from experiments.evaluation.stage_r1_1_trace import (
    TRACE_COMPLETE,
    TRACE_INCOMPLETE,
    compare_with_result,
    summarize_trace,
)

from atlasos.tools.conversation import AtlasToolAgent
from atlasos.trajectory.payloads import (
    EXECUTION_INTEGRITY_OFF,
    EXECUTION_INTEGRITY_RECORD,
    INPUT_RECORDED,
    INPUT_UNAVAILABLE,
    ToolPayloadCorpus,
    read_corpus,
)
from atlasos.trajectory.recorder import TrajectoryRecorder

RUN_ID = "run-trace"


def _recorder(run_dir: Path) -> TrajectoryRecorder:
    return TrajectoryRecorder(
        FilesystemRunStore(run_dir / "trajectory"), RUN_ID, agent_name="test", model="fake"
    )


def _proposal(recorder: TrajectoryRecorder, *, call_id: str, digest: str, ref: str | None) -> None:
    recorder.record_action_proposed(
        model_response_id="resp-1",
        ordinal=0,
        call_id=call_id,
        tool_id="file_write",
        tool_name="file_write",
        input_digest=digest,
        input_ref=ref,
        input_state=INPUT_RECORDED if ref else INPUT_UNAVAILABLE,
        correlation=_correlation(worker="run-a", task="run-a"),
    )


def _decision(recorder: TrajectoryRecorder, *, call_id: str, decision: str) -> None:
    recorder.record_action_decision(
        phase="execution",
        decision=decision,
        decision_source="external_policy" if decision == "DENY" else "policy",
        tool_id="file_write",
        call_id=call_id,
        occurrence_id=f"occ-{call_id}",
        policy_rule_id=None,
        decision_basis_digest="b" * 64,
        action_digest="a" * 64,
        input_digest="i" * 64,
        tool_binding_digest="t" * 64,
        reason_codes=("OWNERSHIP_DENIED_FOREIGN_PATH",) if decision == "DENY" else (),
        scope_alignment="ALIGNED",
        reviewer_verdict=None,
        external_policy_outcome="deny" if decision == "DENY" else "allow",
        correlation=_correlation(worker="run-a", task="run-a"),
    )


def _observation(recorder: TrajectoryRecorder, *, call_id: str, is_error: bool) -> None:
    recorder.record_tool_observation(
        call_id=call_id,
        tool_id="file_write",
        is_error=is_error,
        result_digest="r" * 64,
        result_size=10,
        correlation=_correlation(worker="run-a", task="run-a"),
    )


def _correlation(*, worker: str, task: str):  # noqa: ANN202
    from atlasos.trajectory.models import TrajectoryCorrelation

    return TrajectoryCorrelation(worker_id=worker, profile_id="coding_worker", task_id=task)


def test_payload_corpus_is_private_and_readable(tmp_path: Path) -> None:
    corpus = ToolPayloadCorpus.for_run(tmp_path)
    ref = corpus.record(
        call_id="call-1",
        input_digest="d" * 64,
        tool_id="bash",
        payload={"command": "git checkout -- ledger/money.py"},
        worker_id="run-a",
        task_id="run-a",
    )
    assert ref is not None
    assert corpus.path.parent.name == "private"
    records = read_corpus(corpus.path)
    assert records[ref]["payload"] == {"command": "git checkout -- ledger/money.py"}
    assert records[ref]["worker_id"] == "run-a"


def test_payload_corpus_reports_write_failure(tmp_path: Path) -> None:
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    corpus = ToolPayloadCorpus(blocked)
    corpus.path = blocked  # a directory cannot be appended to
    ref = corpus.record(call_id="c", input_digest="d" * 64, tool_id="bash", payload={"a": 1})
    assert ref is None
    assert corpus.write_failures == 1


def test_summary_is_complete_when_events_close(tmp_path: Path) -> None:
    recorder = _recorder(tmp_path)
    corpus = ToolPayloadCorpus.for_run(tmp_path)
    ref = corpus.record(call_id="call-1", input_digest="d" * 64, tool_id="file_write", payload={})
    _proposal(recorder, call_id="call-1", digest="d" * 64, ref=ref)
    _decision(recorder, call_id="call-1", decision="ALLOW")
    _observation(recorder, call_id="call-1", is_error=False)
    recorder.record_run_terminal(
        status="SUCCESS", task_count=1, final_output_digest="f" * 64, final_output_size=1
    )

    summary = summarize_trace(tmp_path)
    assert summary["status"] == TRACE_COMPLETE
    assert summary["proposed_actions"] == 1
    assert summary["authorized_actions"] == 1
    assert summary["input_digest_coverage"] == 1.0
    assert summary["corpus_coverage"] == 1.0
    assert summary["unmatched_outcomes"] == 0
    assert summary["workers"] == {"run-a": 1}


def test_t10_budget_stop_keeps_the_actions_that_already_happened(tmp_path: Path) -> None:
    recorder = _recorder(tmp_path)
    _proposal(recorder, call_id="call-1", digest="d" * 64, ref=None)
    _decision(recorder, call_id="call-1", decision="ALLOW")
    _observation(recorder, call_id="call-1", is_error=False)
    recorder.record_worker_lifecycle(
        worker_id="run-a",
        profile_id="coding_worker",
        parent_agent="atlas-evaluation",
        mode="stop",
        context_hash="c" * 64,
        transition_id="stop:budget_exceeded",
        stop_code="budget_exceeded",
        stop_reason="run budget fully consumed",
        correlation=_correlation(worker="run-a", task="run-a"),
    )

    summary = summarize_trace(tmp_path)
    assert summary["status"] == TRACE_COMPLETE
    assert summary["worker_stops"] == 1
    assert summary["proposed_actions"] == 1
    assert summary["completed_actions"] == 0
    comparison = compare_with_result(summary, [])
    assert comparison["recovered_from_trajectory"] == 1
    assert comparison["result_is_source_of_truth"] is False


def test_t11_recorder_failure_makes_the_trace_incomplete(tmp_path: Path) -> None:
    recorder = _recorder(tmp_path)
    _proposal(recorder, call_id="call-1", digest="d" * 64, ref=None)
    recorder.record_run_terminal(
        status="SUCCESS", task_count=1, final_output_digest="f" * 64, final_output_size=1
    )
    assert summarize_trace(tmp_path, recorder_failures=2)["status"] == TRACE_INCOMPLETE


def test_t12_uncertain_outcome_is_not_reported_as_completed(tmp_path: Path) -> None:
    recorder = _recorder(tmp_path)
    _proposal(recorder, call_id="call-1", digest="d" * 64, ref=None)
    _decision(recorder, call_id="call-1", decision="ALLOW")
    recorder.record_execution_outcome(
        action_id="a1",
        attempt_id="t1",
        tool_id="bash",
        canonical_action="shell.execute",
        state="UNCERTAIN",
        completion_kind=None,
        claim_outcome=None,
        retry_safe=None,
        resource_digest=None,
        result_digest=None,
        result_size=None,
        correlation=_correlation(worker="run-a", task="run-a"),
    )
    recorder.record_run_terminal(
        status="TIMEOUT", task_count=1, final_output_digest="f" * 64, final_output_size=1
    )
    summary = summarize_trace(tmp_path)
    assert summary["unknown_actions"] == 1
    assert summary["completed_actions"] == 0
    assert summary["status"] == TRACE_COMPLETE


def test_unmatched_observation_makes_the_trace_incomplete(tmp_path: Path) -> None:
    recorder = _recorder(tmp_path)
    _observation(recorder, call_id="ghost", is_error=False)
    recorder.record_run_terminal(
        status="SUCCESS", task_count=1, final_output_digest="f" * 64, final_output_size=1
    )
    summary = summarize_trace(tmp_path)
    assert summary["unmatched_outcomes"] == 1
    assert summary["status"] == TRACE_INCOMPLETE


def test_duplicate_call_ids_with_different_inputs_are_flagged(tmp_path: Path) -> None:
    recorder = _recorder(tmp_path)
    _proposal(recorder, call_id="call-1", digest="a" * 64, ref=None)
    _proposal(recorder, call_id="call-1", digest="b" * 64, ref=None)
    recorder.record_run_terminal(
        status="SUCCESS", task_count=1, final_output_digest="f" * 64, final_output_size=1
    )
    summary = summarize_trace(tmp_path)
    assert summary["duplicate_call_ids"] == 1
    assert summary["status"] == TRACE_INCOMPLETE


def test_budget_stop_emits_a_worker_stop_event(tmp_path: Path) -> None:
    recorder = _recorder(tmp_path)
    runtime = SimpleNamespace(
        trajectory_recorder=recorder,
        execution_integrity_mode=EXECUTION_INTEGRITY_RECORD,
    )
    hook = SimpleNamespace(take_rejection=lambda: "budget_exhausted: 120000/120000")
    context = ExecutionContext(
        principal="user:evaluation",
        attributes={
            "worker_id": "run-a",
            "profile_id": "coding_worker",
            "task_id": "run-a",
            "parent_agent": "atlas-evaluation",
        },
    )
    agent = object.__new__(AtlasToolAgent)
    agent._budget_hook = hook  # type: ignore[attr-defined]
    agent._atlas_tool_runtime = runtime  # type: ignore[attr-defined]
    agent.config = SimpleNamespace(execution_context=context, system_prompt="role")  # type: ignore[attr-defined]

    result = agent._apply_budget_outcome(  # noqa: SLF001 - the seam under test
        SimpleNamespace(model_copy=lambda update: SimpleNamespace(**update), success=True)  # type: ignore[arg-type]
    )
    assert result.success is False
    assert summarize_trace(tmp_path)["worker_stops"] == 1
    assert summarize_trace(tmp_path)["status"] == TRACE_COMPLETE


def test_worker_stop_event_is_not_emitted_when_integrity_is_off(tmp_path: Path) -> None:
    recorder = _recorder(tmp_path)
    runtime = SimpleNamespace(
        trajectory_recorder=recorder,
        execution_integrity_mode=EXECUTION_INTEGRITY_OFF,
    )
    hook = SimpleNamespace(take_rejection=lambda: "budget_exhausted: x")
    context = ExecutionContext(
        principal="user:evaluation",
        attributes={"worker_id": "run-a", "profile_id": "coding_worker", "task_id": "run-a"},
    )
    agent = object.__new__(AtlasToolAgent)
    agent._budget_hook = hook  # type: ignore[attr-defined]
    agent._atlas_tool_runtime = runtime  # type: ignore[attr-defined]
    agent.config = SimpleNamespace(execution_context=context, system_prompt="role")  # type: ignore[attr-defined]

    agent._apply_budget_outcome(  # noqa: SLF001
        SimpleNamespace(model_copy=lambda update: SimpleNamespace(**update), success=True)  # type: ignore[arg-type]
    )
    assert summarize_trace(tmp_path)["events"] == 0


def test_runtime_rejects_a_corpus_without_record_mode(tmp_path: Path) -> None:
    from atlasos.tools import AtlasToolRuntime

    corpus = ToolPayloadCorpus.for_run(tmp_path)
    with pytest.raises(ValueError):
        AtlasToolRuntime(
            "run-x",
            state_path=tmp_path / "state.json",
            tool_payload_corpus=corpus,
        )


def test_runtime_rejects_an_unknown_integrity_mode(tmp_path: Path) -> None:
    from atlasos.tools import AtlasToolRuntime

    with pytest.raises(ValueError):
        AtlasToolRuntime("run-x", state_path=tmp_path / "state.json", execution_integrity_mode="x")
