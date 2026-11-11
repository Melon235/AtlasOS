"""Stage D recovery-session cleanup and ownership handoff tests."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
import pytest_asyncio
from tests.helpers.stage_d_world import (
    TASK_ID,
    World,
    close_open_worlds,
)
from tests.integration.test_sandbox_ownership import (
    _attempt_lock_in_process,
    _spawn_holder,
    _stop_holder,
)

from atlasos.sandbox import (
    ProjectSandboxManager,
    SandboxProjectConfig,
    SQLiteSandboxBindingStore,
    WorkloadOwnershipLock,
)
from atlasos.trusted_recovery import (
    RecoveryCleanupResult,
    TrustedRecoveryDisposalError,
    TrustedRecoveryError,
    TrustedRecoveryOwnershipUnresolved,
    TrustedRecoverySession,
    resume_durable_sandbox_suspension,
)


@pytest_asyncio.fixture(autouse=True)
async def _close_worlds():
    yield
    await close_open_worlds()


async def _approved_session(tmp_path: Path) -> tuple[World, TrustedRecoverySession]:
    world = World(tmp_path)
    await world.create_suspension()
    await world.approve()
    await world.release_a()
    session = await TrustedRecoverySession.prepare(world.recovery_config())
    return world, session


def _durable_ready(world: World) -> None:
    store = SQLiteSandboxBindingStore(world.state_root / "sandbox_bindings.db")
    durable = store.get_for_task("run-stage-d", TASK_ID)
    assert durable is not None
    assert durable.state == "READY"


async def test_default_wrapper_releases_ownership_without_destroying_s1(
    tmp_path: Path,
) -> None:
    world = World(tmp_path)
    await world.create_suspension()
    await world.approve()
    await world.release_a()
    before_create = world.provider.create_count
    outcome = await resume_durable_sandbox_suspension(
        world.recovery_config(),
        dispose_sandbox_on_settlement=False,
    )
    assert outcome.status == "settled"
    assert world.provider.create_count == before_create
    assert world.provider.destroy_count == 0
    _durable_ready(world)
    assert world.sandbox_id is not None
    assert world.sandbox_id in world.provider.remote_state
    target = world.provider.workspaces[world.sandbox_id] / "workspace" / "project" / "recovered.txt"
    assert target.read_text(encoding="utf-8") == "approved recovery\n"

    # Another real OS process can acquire the workload lock after wrapper cleanup.
    ready = tmp_path / "holder.ready"
    stop = tmp_path / "holder.stop"
    holder = _spawn_holder(
        world.state_root,
        run_id="run-stage-d",
        task_id=TASK_ID,
        ready=ready,
        stop=stop,
    )
    _stop_holder(holder, stop)
    probe = WorkloadOwnershipLock(world.state_root, run_id="run-stage-d", task_id=TASK_ID)
    probe.acquire(timeout_seconds=5.0)
    probe.release()

    # A new Manager must use verified reattachment, not create S2.
    assert world.worker is not None
    manager = ProjectSandboxManager(
        world.provider,
        SandboxProjectConfig(
            provider_name="fake",
            image="fake@sha256:0123456789abcdef",
            workspace_root="/workspace/project",
        ),
        run_id="run-stage-d",
        binding_store=SQLiteSandboxBindingStore(world.state_root / "sandbox_bindings.db"),
        ownership_root=world.state_root,
    )
    attached = await manager.reattach_verified(
        task_id=TASK_ID,
        context=world.worker.execution_context,
    )
    assert attached.sandbox_handle.id == world.sandbox_id
    assert world.provider.destroy_count == 0
    await manager.release_ownership(TASK_ID)


async def test_session_close_is_idempotent_and_releases_safely(tmp_path: Path) -> None:
    world, session = await _approved_session(tmp_path)
    outcome = await session.resume()
    assert outcome.status == "settled"
    first = await session.close()
    second = await session.close()
    assert first == second
    assert first.ownership_released is True
    assert first.ownership_unresolved is False
    assert first.runtime_closed is True
    assert first.idempotency_closed is True
    assert session.manager.has_ownership(TASK_ID) is False
    assert world.provider.destroy_count == 0
    _durable_ready(world)


async def test_local_detach_failure_retains_ownership(tmp_path: Path) -> None:
    world, session = await _approved_session(tmp_path)
    assert (await session.resume()).status == "settled"
    world.provider.detach_error = RuntimeError("local detach failed")
    cleanup = await session.close()
    assert cleanup.ownership_released is False
    assert cleanup.ownership_unresolved is True
    assert cleanup.error is not None
    assert session.manager.has_ownership(TASK_ID) is True
    assert (
        _attempt_lock_in_process(world.state_root, run_id="run-stage-d", task_id=TASK_ID) is False
    )
    assert world.provider.destroy_count == 0
    _durable_ready(world)
    assert await session.close() == cleanup

    world.provider.detach_error = None
    # The failed release already invalidated the runtime cache and marked uncertainty, so the
    # explicit safe terminal resolution is mark_lost rather than a raw close.
    await session.manager.mark_lost(TASK_ID)
    assert session.manager.has_ownership(TASK_ID) is False


async def test_uncertain_execution_is_not_released_by_cleanup(tmp_path: Path) -> None:
    world, session = await _approved_session(tmp_path)
    world.provider.raise_write_after_effect = True
    outcome = await session.resume()
    assert outcome.status == "settled"
    assert outcome.settlement is not None
    assert [item.outcome for item in outcome.settlement.results] == ["UNCERTAIN"]
    cleanup = await session.close()
    assert cleanup.ownership_unresolved is True
    assert cleanup.ownership_released is False
    assert session.manager.has_ownership(TASK_ID) is True
    assert (
        _attempt_lock_in_process(world.state_root, run_id="run-stage-d", task_id=TASK_ID) is False
    )
    assert world.provider.destroy_count == 0
    _durable_ready(world)
    assert await session.close() == cleanup

    world.provider.raise_write_after_effect = False
    assert await session.manager.close(TASK_ID) is True
    assert world.provider.destroy_count == 1


async def test_wrapper_exposes_uncertain_ownership_without_handoff(tmp_path: Path) -> None:
    world = World(tmp_path)
    await world.create_suspension()
    await world.approve()
    await world.release_a()
    world.provider.raise_write_after_effect = True
    with pytest.raises(TrustedRecoveryOwnershipUnresolved) as raised:
        await resume_durable_sandbox_suspension(
            world.recovery_config(),
            dispose_sandbox_on_settlement=False,
        )
    error = raised.value
    assert error.cleanup.ownership_unresolved is True
    assert error.outcome is not None
    assert error.outcome.settlement is not None
    assert [item.outcome for item in error.outcome.settlement.results] == ["UNCERTAIN"]
    assert (
        _attempt_lock_in_process(world.state_root, run_id="run-stage-d", task_id=TASK_ID) is False
    )
    assert world.provider.destroy_count == 0
    _durable_ready(world)


async def test_disposal_is_refused_when_settlement_is_uncertain(tmp_path: Path) -> None:
    world = World(tmp_path)
    await world.create_suspension()
    await world.approve()
    await world.release_a()
    world.provider.raise_write_after_effect = True
    with pytest.raises(TrustedRecoveryDisposalError, match="UNCERTAIN") as raised:
        await resume_durable_sandbox_suspension(
            world.recovery_config(),
            dispose_sandbox_on_settlement=True,
        )
    assert raised.value.disposed is False
    assert raised.value.cleanup is not None
    assert raised.value.cleanup.ownership_unresolved is True
    assert world.provider.destroy_count == 0
    assert world.sandbox_id is not None
    assert world.sandbox_id in world.provider.remote_state
    _durable_ready(world)


async def test_disposal_failure_is_not_reported_as_success(tmp_path: Path) -> None:
    world = World(tmp_path)
    await world.create_suspension()
    await world.approve()
    await world.release_a()
    world.provider.fail_destroy = True
    with pytest.raises(TrustedRecoveryDisposalError, match="destruction") as raised:
        await resume_durable_sandbox_suspension(
            world.recovery_config(),
            dispose_sandbox_on_settlement=True,
        )
    assert raised.value.disposed is False
    assert raised.value.cleanup is not None
    assert world.provider.destroy_count == 1
    store = SQLiteSandboxBindingStore(world.state_root / "sandbox_bindings.db")
    durable = store.get_for_task("run-stage-d", TASK_ID)
    assert durable is not None
    assert durable.state == "LOST"


async def test_disposal_exception_exposes_cleanup_and_retains_ownership(
    tmp_path: Path,
) -> None:
    world = World(tmp_path)
    await world.create_suspension()
    await world.approve()
    await world.release_a()
    world.provider.raise_destroy = True
    with pytest.raises(TrustedRecoveryDisposalError, match="destroy") as raised:
        await resume_durable_sandbox_suspension(
            world.recovery_config(),
            dispose_sandbox_on_settlement=True,
        )
    assert raised.value.disposed is False
    assert raised.value.cleanup is not None
    assert raised.value.cleanup.ownership_unresolved is True
    assert raised.value.cleanup.ownership_released is False
    assert world.provider.destroy_count == 1
    _durable_ready(world)
    assert (
        _attempt_lock_in_process(world.state_root, run_id="run-stage-d", task_id=TASK_ID) is False
    )


async def test_cleanup_reports_runtime_close_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, session = await _approved_session(tmp_path)
    assert (await session.resume()).status == "settled"

    async def fail_close() -> None:
        raise RuntimeError("runtime close failed")

    monkeypatch.setattr(session.runtime, "close", fail_close)
    cleanup = await session.close()
    assert cleanup.ownership_released is True
    assert cleanup.ownership_unresolved is False
    assert cleanup.runtime_closed is False
    assert cleanup.idempotency_closed is True
    assert cleanup.error is not None
    assert "runtime close failed" in (cleanup.reason or "")


async def test_cleanup_reports_idempotency_teardown_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, session = await _approved_session(tmp_path)
    assert (await session.resume()).status == "settled"

    async def fail_teardown() -> None:
        raise RuntimeError("idempotency close failed")

    monkeypatch.setattr(session.idempotency, "teardown", fail_teardown)
    cleanup = await session.close()
    assert cleanup.ownership_released is True
    assert cleanup.ownership_unresolved is False
    assert cleanup.runtime_closed is True
    assert cleanup.idempotency_closed is False
    assert cleanup.error is not None
    assert "idempotency close failed" in (cleanup.reason or "")


async def test_wrapper_exposes_cleanup_when_recovery_and_cleanup_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = World(tmp_path)
    suspension = await world.create_suspension()
    await world.approve()
    await world.release_a()
    db = world.state_root / "suspension.db"
    with sqlite3.connect(db) as connection:
        row = connection.execute(
            "SELECT payload FROM execution_suspensions WHERE suspension_id=?",
            (suspension.suspension_id,),
        ).fetchone()
        payload = json.loads(row[0])
        payload["sandbox_binding_digest"] = "f" * 64
        connection.execute(
            "UPDATE execution_suspensions SET payload=? WHERE suspension_id=?",
            (json.dumps(payload), suspension.suspension_id),
        )
        connection.commit()
    captured: list[TrustedRecoverySession] = []
    original_close = TrustedRecoverySession.close

    async def partially_failing_close(self: TrustedRecoverySession) -> RecoveryCleanupResult:
        captured.append(self)
        return RecoveryCleanupResult(
            ownership_released=True,
            ownership_unresolved=False,
            runtime_closed=False,
            idempotency_closed=False,
            reason="cleanup failed after the original recovery error",
            error=RuntimeError("cleanup failed after the original recovery error"),
        )

    monkeypatch.setattr(TrustedRecoverySession, "close", partially_failing_close)
    try:
        with pytest.raises(TrustedRecoveryError, match="continuity") as raised:
            await resume_durable_sandbox_suspension(world.recovery_config())
    finally:
        monkeypatch.setattr(TrustedRecoverySession, "close", original_close)
    notes = "\n".join(raised.value.__notes__)
    assert "cleanup failed after the original recovery error" in notes
    assert "runtime_closed=False" in notes
    assert "idempotency_closed=False" in notes
    for session in captured:
        await session.close()


async def test_cleanup_does_not_mask_recovery_exception(tmp_path: Path) -> None:
    world = World(tmp_path)
    suspension = await world.create_suspension()
    await world.approve()
    await world.release_a()
    db = world.state_root / "suspension.db"
    with sqlite3.connect(db) as connection:
        row = connection.execute(
            "SELECT payload FROM execution_suspensions WHERE suspension_id=?",
            (suspension.suspension_id,),
        ).fetchone()
        payload = json.loads(row[0])
        payload["sandbox_binding_digest"] = "f" * 64
        connection.execute(
            "UPDATE execution_suspensions SET payload=? WHERE suspension_id=?",
            (json.dumps(payload), suspension.suspension_id),
        )
        connection.commit()
    world.provider.detach_error = RuntimeError("local detach failed")
    with pytest.raises(TrustedRecoveryError, match="continuity") as raised:
        await resume_durable_sandbox_suspension(world.recovery_config())
    assert any("ownership" in note.lower() for note in raised.value.__notes__)
    assert world.provider.destroy_count == 0
    _durable_ready(world)
