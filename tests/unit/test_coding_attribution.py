"""Regression: a coding session must never report an unclassified failure.

Final-2 found that a run which merely exhausted its turn budget reported ``status=failed`` with
``failure_class=null``, which makes "the model stopped" indistinguishable from "the runtime broke".
"""

from __future__ import annotations

from atlasos.app.coding import _classify_agent_outcome


def test_successful_agent_has_no_failure() -> None:
    assert (
        _classify_agent_outcome(
            agent_success=True, stop_reason="success", terminal_phase="completed", turns=7
        )
        is None
    )


def test_turn_limit_exhaustion_is_labelled_as_model_unfinished() -> None:
    failure = _classify_agent_outcome(
        agent_success=False,
        stop_reason="max_turns",
        terminal_phase="failed",
        turns=10,
    )

    assert failure is not None
    assert failure.failure_class == "MODEL_UNFINISHED"
    assert failure.stage == "agent_loop"
    assert "max_turns" in failure.message
    assert "turns=10" in failure.message


def test_unknown_stop_reason_still_produces_a_class() -> None:
    failure = _classify_agent_outcome(
        agent_success=False, stop_reason=None, terminal_phase=None, turns=0
    )

    assert failure is not None
    assert failure.failure_class == "MODEL_UNFINISHED"
    assert "stop_reason=None" in failure.message
