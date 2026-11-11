"""Offline tests for the evaluation layer (no model or sandbox calls)."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest
from experiments.evaluation.fixtures import (
    materialize_fixture,
    path_matches,
    workspace_hashes,
)
from experiments.evaluation.models import BenchmarkTask, WorkerStep
from experiments.evaluation.oracle import evaluate_oracle
from experiments.evaluation.tasks import TASKS, task_by_id, validate_tasks


def test_dataset_has_thirty_tasks_and_expected_category_counts() -> None:
    assert len(TASKS) == 30
    counts = Counter(task.category for task in TASKS)
    assert counts == {
        "bugfix": 10,
        "feature": 5,
        "tool_discovery": 5,
        "multi_worker": 5,
        "long_context": 5,
    }
    validate_tasks()


def test_task_fixture_digest_is_stable_and_sensitive() -> None:
    task = task_by_id("bugfix-001-slugify")
    assert task.fixture_digest == task_by_id("bugfix-001-slugify").fixture_digest
    assert task.fixture_digest != task_by_id("bugfix-002-median").fixture_digest


def test_path_matching_for_forbidden_and_allowed_paths() -> None:
    assert path_matches("tests/test_hidden.py", ("tests/**",))
    assert not path_matches("slugify.py", ("tests/**",))
    assert path_matches("slugify.py", ("slugify.py",))


def test_multi_worker_requires_two_workers() -> None:
    with pytest.raises(ValueError, match="multi_worker"):
        BenchmarkTask(
            sample_id="bad-multiworker",
            category="multi_worker",
            mode="multi_worker",
            prompt="x",
            target="y",
            visible_files={},
            workers=(WorkerStep(task_id="one", title="one", prompt="x"),),
        )


def _oracle_workspace(tmp_path: Path, task: BenchmarkTask) -> tuple[Path, dict[str, str]]:
    fixture = tmp_path / "fixture"
    workspace = tmp_path / "workspace"
    materialize_fixture(task, fixture)
    materialize_fixture(task, workspace)
    baseline = workspace_hashes(fixture)
    return workspace, baseline


def test_hidden_oracle_accepts_a_correct_fix(tmp_path: Path) -> None:
    task = task_by_id("bugfix-001-slugify")
    workspace, baseline = _oracle_workspace(tmp_path, task)
    (workspace / "slugify.py").write_text(
        "import re\n\n\ndef slugify(text: str) -> str:\n"
        '    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")\n',
        encoding="utf-8",
    )
    outcome = evaluate_oracle(
        task,
        workspace=workspace,
        baseline_hashes=baseline,
        tool_calls=[],
        worker_results={},
        run_dir=tmp_path,
        host_fixture_unchanged=True,
    )
    assert outcome.passed is True
    assert outcome.checks["hidden_tests_passed"] is True
    assert outcome.checks["only_allowed_paths_changed"] is True


def test_hidden_oracle_rejects_baseline_and_forbidden_change(tmp_path: Path) -> None:
    task = task_by_id("bugfix-001-slugify")
    workspace, baseline = _oracle_workspace(tmp_path, task)
    outcome = evaluate_oracle(
        task,
        workspace=workspace,
        baseline_hashes=baseline,
        tool_calls=[],
        worker_results={},
        run_dir=tmp_path / "baseline-run",
        host_fixture_unchanged=True,
    )
    assert outcome.passed is False
    assert outcome.checks["hidden_tests_passed"] is False

    (workspace / "tests" / "test_public.py").write_text("# changed\n", encoding="utf-8")
    forbidden = evaluate_oracle(
        task,
        workspace=workspace,
        baseline_hashes=baseline,
        tool_calls=[],
        worker_results={},
        run_dir=tmp_path / "forbidden-run",
        host_fixture_unchanged=True,
    )
    assert forbidden.checks["forbidden_paths_unchanged"] is False
    assert forbidden.passed is False


def test_tool_contract_requires_discovery_and_dispatch(tmp_path: Path) -> None:
    task = task_by_id("tool-001-fare")
    workspace, baseline = _oracle_workspace(tmp_path, task)
    (workspace / "answer.txt").write_text("fare_usd=412\n", encoding="utf-8")
    outcome = evaluate_oracle(
        task,
        workspace=workspace,
        baseline_hashes=baseline,
        tool_calls=[
            {"tool_name": "tool_search", "input": {"query": "fare"}, "output": "ok"},
            {
                "tool_name": "dispatch_action",
                "input": {"tool_id": "mcp_evaltools_lookup_fare", "arguments": {}},
                "output": "fare_usd=412",
            },
        ],
        worker_results={},
        run_dir=tmp_path,
        host_fixture_unchanged=True,
    )
    assert outcome.passed is True
    assert outcome.checks["tool_search_performed"] is True
    assert outcome.checks["required_tools_dispatched"] is True
    assert outcome.checks["tool_output_contract"] is True
