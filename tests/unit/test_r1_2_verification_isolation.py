"""R1-2A: the verifier must not read the hidden oracle and must not edit historical runs."""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import experiments.evaluation.stage_r1_2_contracts as contracts
from experiments.evaluation.stage_r1_2_verify import HistoricalRun, verify_runs

GOOD_MONEY = '''"""Money."""


def format_cents(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    dollars, rest = divmod(abs(cents), 100)
    return f"{sign}${dollars:,}.{rest:02d}"


def parse_cents(text: str) -> int:
    return int(text)


def split_evenly(total: int, parts: int) -> list[int]:
    return [total // parts] * parts
'''


def test_contract_module_never_references_the_hidden_oracle() -> None:
    """Prose may explain the boundary; code must not cross it."""

    tree = ast.parse(Path(contracts.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert "oracle" not in node.module
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert "oracle" not in alias.name
        if isinstance(node, ast.Attribute):
            assert node.attr != "hidden_files"
        if isinstance(node, ast.Name):
            assert node.id != "hidden_files"
    for spec in (contracts.p1_spec(), contracts.p2_spec(), contracts.p3_spec()):
        payload = repr(spec.payload())
        assert "hidden" not in payload
        assert "test_hidden" not in payload


def test_historical_run_is_opened_read_only(tmp_path: Path) -> None:
    run_dir = tmp_path / "round" / "runs" / "r05-p2-modules-r0-o0"
    workspace = run_dir / "workspace"
    (workspace / "ledger").mkdir(parents=True)
    (workspace / "ledger" / "entries.py").write_text(
        "def parse_entry(line):\n    return (line, 0, '')\n\n"
        "def total_for(entries, memo):\n    return 0\n\n"
        "def top_memos(entries, limit):\n    return []\n",
        encoding="utf-8",
    )
    (workspace / "ledger" / "money.py").write_text(GOOD_MONEY, encoding="utf-8")
    before = {
        item.relative_to(run_dir).as_posix(): item.read_bytes()
        for item in run_dir.rglob("*")
        if item.is_file()
    }
    run = HistoricalRun(
        stage="stage-r1-1",
        round_id="test-round",
        run_key="r05-p2-modules-r0-o0",
        sample_id="r05-p2-modules",
        arm="o0",
        run_dir=run_dir,
        workspace=workspace,
        oracle_pass=True,
        failure_class="TASK_SUCCESS",
        fixture_dir=None,
        recorded_artifact_digest=None,
    )
    records = asyncio.run(verify_runs([run]))
    after = {
        item.relative_to(run_dir).as_posix(): item.read_bytes()
        for item in run_dir.rglob("*")
        if item.is_file()
    }
    assert before == after
    assert records[0]["verification"] is not None
    assert records[0]["unavailable"] is None


def test_missing_workspace_is_marked_unavailable(tmp_path: Path) -> None:
    run_dir = tmp_path / "round" / "runs" / "r05-p2-modules-r0-o0"
    run_dir.mkdir(parents=True)
    run = HistoricalRun(
        stage="stage-r1-1",
        round_id="test-round",
        run_key="r05-p2-modules-r0-o0",
        sample_id="r05-p2-modules",
        arm="o0",
        run_dir=run_dir,
        workspace=None,
        oracle_pass=False,
        failure_class="RUNTIME_FAILURE",
        fixture_dir=None,
        recorded_artifact_digest=None,
    )
    records = asyncio.run(verify_runs([run]))
    assert records[0]["unavailable"] == "UNAVAILABLE"


def test_unknown_sample_is_reported_not_guessed(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run = HistoricalRun(
        stage="stage-r1-1",
        round_id="test-round",
        run_key="mystery-r0-o0",
        sample_id="mystery-task",
        arm="o0",
        run_dir=tmp_path,
        workspace=workspace,
        oracle_pass=True,
        failure_class="TASK_SUCCESS",
        fixture_dir=None,
        recorded_artifact_digest=None,
    )
    records = asyncio.run(verify_runs([run]))
    assert records[0]["unavailable"] == "NO_SPEC"
    assert records[0]["verification"] is None
