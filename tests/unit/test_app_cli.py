from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from atlasos.app.cli import app


def _seed(root: Path) -> None:
    run = root / "app-cli"
    run.mkdir(parents=True)
    (run / "run.json").write_text(
        json.dumps(
            {
                "run_id": "app-cli",
                "mode": "workflow",
                "task": "cli task",
                "created_at": "2026-11-09T00:00:00+00:00",
                "updated_at": "2026-11-09T00:00:01+00:00",
                "status": "COMPLETED",
            }
        ),
        encoding="utf-8",
    )
    (run / "events.jsonl").write_text(
        json.dumps({"id": 1, "type": "run_submitted", "timestamp": "2026-11-09T00:00:00Z"}) + "\n",
        encoding="utf-8",
    )


def test_cli_json_history_status_and_logs(tmp_path: Path) -> None:
    _seed(tmp_path)
    runner = CliRunner()
    result = runner.invoke(app, ["runs", "--runs-root", str(tmp_path), "--json"])
    assert result.exit_code == 0
    assert json.loads(result.output)[0]["run_id"] == "app-cli"

    result = runner.invoke(app, ["status", "app-cli", "--runs-root", str(tmp_path), "--json"])
    assert result.exit_code == 0
    assert json.loads(result.output)["status"] == "COMPLETED"

    result = runner.invoke(app, ["logs", "app-cli", "--runs-root", str(tmp_path), "--json"])
    assert result.exit_code == 0
    assert json.loads(result.output.splitlines()[0])["type"] == "run_submitted"


def test_cli_rejects_invalid_mode_without_starting_a_run(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        app, ["run", "task", "--mode", "dynamic-supervisor", "--runs-root", str(tmp_path)]
    )
    assert result.exit_code == 2
    assert not any(path.is_dir() and path.name.startswith("app-") for path in tmp_path.iterdir())


def test_cli_approve_is_not_a_fake_success(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["approve", "unknown", "--runs-root", str(tmp_path)])
    assert result.exit_code == 2
    assert "approval" in result.output.lower()


def test_cli_resume_is_not_a_fake_success(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["resume", "unknown", "--runs-root", str(tmp_path)])
    assert result.exit_code == 2
    assert "trusted" in result.output.lower() or "no active" in result.output.lower()
