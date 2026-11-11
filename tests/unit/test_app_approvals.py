from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from anycode.types import LLMMessage, TextBlock

from atlasos.app.service import AtlasApplicationService
from atlasos.approvals.coordinator import ApprovalCoordinator
from atlasos.approvals.models import derive_approval_id
from atlasos.approvals.store import SQLiteApprovalStore
from atlasos.suspension.models import ExecutionSuspension, PendingInvocation
from atlasos.suspension.store import SQLiteSuspensionStore


def _suspension(suspension_id: str, call_id: str, *, ordinal: int = 0) -> ExecutionSuspension:
    action = "a" * 64
    basis = "b" * 64
    approval_id = derive_approval_id(
        suspension_id=suspension_id,
        call_id=call_id,
        action_digest=action,
        decision_basis_digest=basis,
    )
    invocation = PendingInvocation(
        ordinal=ordinal,
        call_id=call_id,
        tool_name="bash",
        logical_tool_id="bash",
        effective_tool_id="bash",
        action="shell.execute",
        resource="demo",
        review_projection={"command": "echo demo"},
        canonical_input={"command": "echo demo"},
        input_digest="c" * 64,
        tool_binding_digest="d" * 64,
        action_digest=action,
        decision_basis_digest=basis,
        preflight_outcome="REQUIRE_HUMAN",
        approval_id=approval_id,
    )
    now = datetime.now(UTC)
    return ExecutionSuspension(
        suspension_id=suspension_id,
        run_id="app-run",
        task_id="task",
        assistant_response_digest="e" * 64,
        held_assistant_message=LLMMessage(
            role="assistant", content=[TextBlock(text="held")]
        ).model_dump(mode="json"),
        pending_invocations=(invocation,),
        trusted_identity_digest="f" * 64,
        response_identity=f"run:app-run:task:task:turn:{ordinal + 1}",
        created_at=now,
        updated_at=now,
    )


def _world(tmp_path: Path) -> tuple[AtlasApplicationService, SQLiteApprovalStore, str]:
    root = tmp_path / "runs"
    root.mkdir(parents=True, exist_ok=True)
    suspensions = SQLiteSuspensionStore(root / "suspension.db")
    approvals = SQLiteApprovalStore(root / "approvals.db")
    suspension = _suspension("susp-1", "call-1")
    suspensions.save_if_absent(suspension)
    challenge = ApprovalCoordinator(approvals).ensure_for_suspension(suspension)[0]
    service = AtlasApplicationService(runs_root=root)
    return service, approvals, challenge.approval_id


def test_approve_and_reject_record_real_m6_decisions(tmp_path: Path) -> None:
    service, approvals, approval_id = _world(tmp_path)
    result = service.approve(approval_id)
    assert result["state"] == "APPROVED"
    assert approvals.get(approval_id).state == "APPROVED"  # type: ignore[union-attr]

    service2, approvals2, second_id = _world(tmp_path / "second")
    result = service2.reject(second_id)
    assert result["state"] == "REJECTED"
    assert approvals2.get(second_id).state == "REJECTED"  # type: ignore[union-attr]


def test_web_approval_endpoints_use_same_authority(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from atlasos.app.web import create_app

    service, approvals, approval_id = _world(tmp_path)
    del service
    client = TestClient(create_app(runs_root=tmp_path / "runs"))
    listed = client.get("/api/approvals")
    assert listed.status_code == 200
    assert listed.json()[0]["approval_id"] == approval_id
    response = client.post(f"/api/approvals/{approval_id}/approve")
    assert response.status_code == 200
    assert response.json()["state"] == "APPROVED"
    assert approvals.get(approval_id).state == "APPROVED"  # type: ignore[union-attr]

    service2, approvals2, second_id = _world(tmp_path / "reject")
    del service2
    client2 = TestClient(create_app(runs_root=tmp_path / "reject" / "runs"))
    response = client2.post(f"/api/approvals/{second_id}/reject")
    assert response.status_code == 200
    assert response.json()["state"] == "REJECTED"
    assert approvals2.get(second_id).state == "REJECTED"  # type: ignore[union-attr]


def test_recovery_status_is_projected_but_resume_needs_trusted_harness(tmp_path: Path) -> None:
    from atlasos.app.service import RecoveryAuthorityUnavailable

    service, _approvals, _approval_id = _world(tmp_path)
    recovery = service.recovery("app-run")
    assert recovery["state"] == "RECOVERY_REQUIRED"
    assert recovery["suspensions"][0]["suspension_id"] == "susp-1"
    assert "pending_invocations" not in recovery["suspensions"][0]
    try:
        service.resume("app-run")
    except RecoveryAuthorityUnavailable:
        pass
    else:
        raise AssertionError("ordinary app resume must not reattach a sandbox")


def test_approval_resolution_requires_matching_active_suspension(tmp_path: Path) -> None:
    service, approvals, approval_id = _world(tmp_path)
    suspension_path = tmp_path / "runs" / "suspension.db"
    suspensions = SQLiteSuspensionStore(suspension_path)
    suspension = suspensions.get("susp-1")
    assert suspension is not None
    suspensions.mark_stale("susp-1", "test cleanup")
    from atlasos.app.service import ApprovalAuthorityUnavailable

    try:
        service.approve(approval_id)
    except ApprovalAuthorityUnavailable:
        pass
    else:
        raise AssertionError("stale suspension must not resolve an approval")
    assert approvals.get(approval_id).state == "PENDING"  # type: ignore[union-attr]
