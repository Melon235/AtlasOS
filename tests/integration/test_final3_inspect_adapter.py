"""Final-3 R-3: the Inspect AI adapter maps an Atlas run result onto a Score without lying.

The scorer is the only place where an external harness could turn a failure into a pass, so the
tests below drive it directly with synthetic run results instead of a live model.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

pytest.importorskip("inspect_ai")

from experiments.evaluation.inspect_adapter import (  # noqa: E402
    _settings_from_payload,
    _settings_payload,
    atlas_oracle_scorer,
    benchmark_dataset,
    build_task,
)
from experiments.evaluation.runner import RunnerSettings  # noqa: E402
from experiments.evaluation.tasks import TASKS  # noqa: E402
from inspect_ai.model import ModelName  # noqa: E402
from inspect_ai.scorer import Score, Target  # noqa: E402
from inspect_ai.solver import TaskState  # noqa: E402

TARGET = Target("hidden oracle passes")


def _state(result: dict[str, Any] | None) -> TaskState:
    return TaskState(
        model=ModelName("mockllm/model"),
        sample_id="bugfix-001-slugify",
        epoch=1,
        input="fix it",
        messages=[],
        target=TARGET,
        store={"atlas_result": result} if result is not None else {},
    )


async def _score(result: dict[str, Any] | None) -> Score:
    scorer = atlas_oracle_scorer()
    return cast(Score, await scorer(_state(result), TARGET))


async def test_dataset_maps_task_metadata_without_hidden_tests() -> None:
    dataset = benchmark_dataset()

    assert len(dataset) == len(TASKS)
    sample = next(item for item in dataset if item.id == "bugfix-001-slugify")
    metadata = sample.metadata or {}
    assert sample.input
    assert cast(str, sample.target) == "hidden oracle passes"
    assert metadata["category"] == "bugfix"
    # Public metadata must never carry hidden test bodies.
    assert "hidden_files" not in metadata


async def test_successful_run_scores_one_with_oracle_metadata() -> None:
    score = await _score(
        {
            "success": True,
            "final_output": "done",
            "category": "bugfix",
            "mode": "coding",
            "failure_class": "TASK_SUCCESS",
            "oracle": {"passed": True, "checks": {"hidden_tests": True}, "failure_reasons": []},
            "metrics": {"tool_calls": 4},
            "identity": {"run_id": "r1", "sample_id": "bugfix-001-slugify", "git_dirty": False},
        }
    )
    metadata = score.metadata or {}

    assert score.value == 1.0
    assert metadata["oracle_passed"] is True
    assert metadata["sample_id"] == "bugfix-001-slugify"
    assert metadata["run_id"] == "r1"


async def test_failed_run_scores_zero_and_keeps_the_failure_class() -> None:
    score = await _score(
        {
            "success": False,
            "failure_class": "MODEL_UNFINISHED",
            "failure_reason": "turn budget exhausted",
            "oracle": {"passed": False, "checks": {"hidden_tests": False}},
            "identity": {"run_id": "r2"},
        }
    )
    metadata = score.metadata or {}

    assert score.value == 0.0
    assert "MODEL_UNFINISHED" in (score.explanation or "")
    assert metadata["oracle_passed"] is False


async def test_missing_result_is_never_a_pass() -> None:
    score = await _score(None)
    metadata = score.metadata or {}

    assert score.value == 0.0
    assert metadata["failure_class"] == "NOT_EVALUATED"


def test_settings_round_trip_keeps_the_run_identity_fields() -> None:
    settings = RunnerSettings(timeout_seconds=42, max_turns=7, token_budget=1234)

    restored = _settings_from_payload(_settings_payload(settings))

    assert restored.timeout_seconds == 42
    assert restored.max_turns == 7
    assert restored.token_budget == 1234
    assert restored.benchmark_id == settings.benchmark_id


def test_build_task_wires_dataset_solver_and_scorer(monkeypatch: pytest.MonkeyPatch) -> None:
    # Inspect resolves its model client at task construction. A placeholder key lets the wiring be
    # tested without a provider call; nothing here sends a request.
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-placeholder-not-used")
    task = build_task(settings=RunnerSettings(), repetition_id=0, tasks=TASKS[:2])

    assert len(task.dataset) == 2
    assert task.solver is not None
    assert task.scorer is not None
