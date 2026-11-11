"""Stage R0.5: the round runner cannot mix policy versions or start on a stale benchmark.

No model is called here. The tests pin the arm configuration, the pre-registration guard, and the
fact that A1 and A2 execute the very same task object (only the routing mode differs).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from experiments.evaluation import stage_r05_rounds as rounds


def test_arms_only_change_the_routing_mode() -> None:
    a0 = rounds._settings("a0", "r0.1.0", Path("runs"))
    a1 = rounds._settings("a1", "r0.1.0", Path("runs"))
    a2 = rounds._settings("a2", "r0.2.0", Path("runs"))
    assert a0.delegation_route_mode == "off"
    assert a1.delegation_route_mode == "off"
    assert a2.delegation_route_mode == "selective"
    assert a2.delegation_policy_version == "r0.2.0"
    for field in (
        "max_turns",
        "max_tool_calls",
        "token_budget",
        "budget_mode",
        "handoff_mode",
        "compaction_mode",
        "tool_error_mode",
        "tool_result_mode",
        "worker_contract_mode",
        "temperature",
    ):
        assert getattr(a0, field) == getattr(a2, field), field


def test_policy_version_is_part_of_the_config_digest() -> None:
    narrow = rounds._settings("a2", "r0.1.0", Path("runs"))
    wide = rounds._settings("a2", "r0.2.0", Path("runs"))
    assert narrow.digest() != wide.digest()


def test_unknown_arm_is_rejected() -> None:
    with pytest.raises(ValueError):
        rounds._settings("a9", "r0.1.0", Path("runs"))


def test_a1_and_a2_share_the_same_task_object() -> None:
    for sample_id in ("r05-p1-analysis", "r05-p2-modules", "r05-p3-frozen-interface"):
        tasks = rounds._tasks_for("a", sample_id)
        assert tasks["a1"] is tasks["a2"]
        assert tasks["a0"].sample_id.endswith("-a0")
        assert tasks["a1"].sample_id.endswith("-a1")


def test_track_b_reuses_the_stage_e1_plans() -> None:
    tasks = rounds._tasks_for("b", "e1-01-config")
    assert [worker.task_id for worker in tasks["a1"].workers] == ["loader", "merge", "validate"]
    assert tasks["a2"] is tasks["a1"]


def test_track_policy_versions_are_fixed() -> None:
    assert rounds.POLICY_VERSION_BY_TRACK == {"a": "r0.1.0", "b": "r0.2.0"}


def test_preregistration_guard_blocks_a_changed_benchmark(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    frozen_path = tmp_path / "preregistration.json"
    monkeypatch.setattr(rounds, "PREREG_PATH", frozen_path)

    payload = rounds.write_preregistration()
    assert frozen_path.is_file()
    assert (
        rounds.assert_preregistered()["preregistration_digest"] == payload["preregistration_digest"]
    )

    stale = json.loads(frozen_path.read_text(encoding="utf-8"))
    stale["preregistration_digest"] = "0" * 64
    frozen_path.write_text(json.dumps(stale), encoding="utf-8")
    with pytest.raises(SystemExit):
        rounds.assert_preregistered()
    with pytest.raises(SystemExit):
        rounds.write_preregistration()


def test_missing_preregistration_blocks_a_round(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rounds, "PREREG_PATH", tmp_path / "missing.json")
    with pytest.raises(SystemExit):
        rounds.assert_preregistered()
