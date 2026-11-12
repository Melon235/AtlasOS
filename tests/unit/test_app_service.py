from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from atlasos.app.service import (
    ApplicationError,
    ApprovalAuthorityUnavailable,
    AtlasApplicationService,
    RecoveryAuthorityUnavailable,
    WorkspaceNotSupported,
)
from atlasos.approvals.models import ApprovalChallenge
from atlasos.approvals.store import SQLiteApprovalStore


def _seeded_run(root: Path, run_id: str = "app-seeded") -> Path:
    run_dir = root / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "mode": "workflow",
                "task": "persisted task",
                "workspace": None,
                "created_at": "2026-11-09T00:00:00+00:00",
                "updated_at": "2026-11-09T00:00:05+00:00",
                "status": "COMPLETED",
                "atlas_run_id": "atlas-1",
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "result.json").write_text(
        json.dumps(
            {
                "metrics": {"total_prompt_tokens": 10, "total_completion_tokens": 4},
                "tasks": [
                    {
                        "task_id": "research",
                        "title": "research",
                        "assignee": "researcher",
                        "status": "completed",
                        "output": "draft",
                    }
                ],
                "final_output": "persisted answer",
                "model": "deepseek-chat",
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "events.jsonl").write_text(
        json.dumps({"id": 1, "type": "run_submitted", "timestamp": "2026-11-09T00:00:00Z"}) + "\n",
        encoding="utf-8",
    )
    return run_dir


def test_history_and_detail_survive_restart(tmp_path: Path) -> None:
    _seeded_run(tmp_path)
    service = AtlasApplicationService(runs_root=tmp_path)
    assert service.list_runs()[0]["run_id"] == "app-seeded"
    detail = service.detail("app-seeded")
    assert detail["status"] == "COMPLETED"
    assert detail["final_output"] == "persisted answer"
    assert detail["tasks"][0]["title"] == "research"
    assert service.events("app-seeded")[0]["type"] == "run_submitted"


def test_workspace_must_be_inside_workspace_root(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    (workspace_root / "ok").mkdir()
    (workspace_root / "ok" / "csv_parser.py").write_text("", encoding="utf-8")
    service = AtlasApplicationService(runs_root=tmp_path / "runs", workspace_root=workspace_root)
    assert service._resolve_workspace("ok") == workspace_root / "ok"
    with pytest.raises(WorkspaceNotSupported):
        service._resolve_workspace("../outside")
    with pytest.raises(WorkspaceNotSupported):
        service._resolve_workspace("/tmp")


def test_invalid_submission_is_rejected_before_execution(tmp_path: Path) -> None:
    service = AtlasApplicationService(runs_root=tmp_path / "runs")
    with pytest.raises(ApplicationError):
        service.submit("")
    with pytest.raises(ApplicationError):
        service.submit("task", mode="dynamic_supervisor")
    with pytest.raises(ApplicationError):
        service.submit("task", max_turns=0)


def test_approval_projection_never_resolves_without_authority(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    root.mkdir()
    store = SQLiteApprovalStore(root / "approvals.db")
    challenge = ApprovalChallenge(
        approval_id="approval-1",
        suspension_id="suspension-1",
        run_id="app-run",
        call_id="call-1",
        ordinal=0,
        action_digest="a" * 64,
        decision_basis_digest="b" * 64,
        tool_name="bash",
        created_at=datetime.now(UTC),
    )
    store.create_if_absent(challenge)
    service = AtlasApplicationService(runs_root=root)
    assert [item["approval_id"] for item in service.list_approvals("app-run")] == ["approval-1"]
    with pytest.raises(ApprovalAuthorityUnavailable):
        service.approve("approval-1")
    with pytest.raises(ApprovalAuthorityUnavailable):
        service.reject("approval-1")
    assert store.get("approval-1").state == "PENDING"  # type: ignore[union-attr]


def test_concurrent_service_instances_read_same_run(tmp_path: Path) -> None:
    _seeded_run(tmp_path)
    first = AtlasApplicationService(runs_root=tmp_path)
    second = AtlasApplicationService(runs_root=tmp_path)
    assert first.detail("app-seeded")["final_output"] == "persisted answer"
    assert second.detail("app-seeded")["final_output"] == "persisted answer"
    assert first.events("app-seeded") == second.events("app-seeded")


def test_cleanup_error_is_visible_not_masked(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    run = root / "app-cleanup"
    run.mkdir(parents=True)
    (run / "run.json").write_text(
        json.dumps(
            {
                "run_id": "app-cleanup",
                "mode": "coding",
                "task": "cleanup visibility",
                "created_at": "2026-11-09T00:00:00+00:00",
                "updated_at": "2026-11-09T00:00:01+00:00",
                "status": "CLEANUP_UNRESOLVED",
                "cleanup_error": "runtime close failed",
            }
        ),
        encoding="utf-8",
    )
    service = AtlasApplicationService(runs_root=root)
    detail = service.detail("app-cleanup")
    assert detail["status"] == "CLEANUP_UNRESOLVED"
    assert detail["cleanup_error"] == "runtime close failed"


def test_missing_trusted_suspension_is_not_resumable(tmp_path: Path) -> None:
    service = AtlasApplicationService(runs_root=tmp_path / "runs")
    assert service.recovery("app-run")["state"] == "NONE"
    with pytest.raises(RecoveryAuthorityUnavailable):
        service.resume("app-run")


def test_coding_mode_calls_the_runtime_composition_in_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: coding mode must not shell out to the evaluation harness."""

    from atlasos.app import coding as coding_module
    from atlasos.app.service import RunRecord

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    captured: dict[str, object] = {}

    async def fake_session(options: coding_module.CodingRunOptions):
        captured["options"] = options
        return ({"run_id": "coding-1", "status": "completed", "sandbox": {}}, 0)

    monkeypatch.setattr(coding_module, "run_coding_session", fake_session)

    service = AtlasApplicationService(runs_root=tmp_path / "runs")
    record = RunRecord(
        run_id="app-coding",
        mode="coding",
        task="fix the parser",
        workspace=str(workspace),
        created_at="2026-11-12T00:00:00+00:00",
    )
    service._execute_coding(record, max_turns=5)

    options = captured["options"]
    assert isinstance(options, coding_module.CodingRunOptions)
    assert options.fixture == workspace
    assert options.task == "fix the parser"
    assert options.max_turns == 5
    assert options.require_real_sandbox is True
    assert record.status == "COMPLETED"
    assert record.uat_run_id == "coding-1"
    assert record.session is not None
