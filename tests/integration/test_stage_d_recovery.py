"""Stage D D1: trusted runtime reconstruction and fail-closed identity checks."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
import pytest_asyncio
from tests.helpers.stage_d_world import (
    PRINCIPAL,
    PROFILE_ID,
    RUN_ID,
    TASK_ID,
    TENANT,
    WORKER_ID,
    World,
    close_open_worlds,
    config,
    prepared_world,
)

from atlasos.trusted_recovery import (
    TrustedRecoveryConfig,
    TrustedRecoveryError,
    TrustedRecoverySession,
)


@pytest_asyncio.fixture(autouse=True)
async def _close_stage_d_worlds():
    yield
    await close_open_worlds()


async def test_d1_reconstructs_same_worker_runtime_and_sqlite_m4(tmp_path: Path) -> None:
    world, session = await prepared_world(tmp_path)
    assert session.worker.worker_id == WORKER_ID
    assert session.worker.profile.profile_id == PROFILE_ID
    assert session.worker.record.task_id == TASK_ID
    assert session.worker.execution_context.principal == PRINCIPAL
    assert session.worker.execution_context.tenant_scope == TENANT
    assert world.worker is not None
    assert session.worker.context_hash == world.worker.context_hash
    assert session.runtime is not world.runtime
    assert session.runtime.action_safety_gate is not None
    assert session.runtime.action_ledger is not None
    assert isinstance(session.idempotency, type(world.idempotency))
    assert session.ledger.path == world.ledger.path
    assert session.idempotency._path == str(world.idempotency._path)
    assert world.provider.create_count == 1
    assert session.manager.create_count == 0
    assert session.manager.bindings == {}
    await session.close()


async def test_d1_missing_trusted_state_fails_closed(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(TrustedRecoveryError, match="missing"):
        TrustedRecoveryConfig(
            state_root=tmp_path / "absent",
            run_id=RUN_ID,
            task_id=TASK_ID,
            worker_id=WORKER_ID,
            profile_id=PROFILE_ID,
            suspension_id="s",
            config=config(tmp_path),
            policy_path=outside / "policy.json",
            expected_principal=PRINCIPAL,
            expected_tenant_scope=TENANT,
        )


async def test_d1_worker_identity_mismatch_fails_closed(tmp_path: Path) -> None:
    world = World(tmp_path)
    await world.create_suspension()
    await world.release_a()
    with pytest.raises(TrustedRecoveryError, match="worker"):
        await TrustedRecoverySession.prepare(world.recovery_config(worker_id="other-worker"))


async def test_d1_tampered_worker_record_fails_closed(tmp_path: Path) -> None:
    world = World(tmp_path)
    await world.create_suspension()
    await world.release_a()
    path = world.state_root / "workers.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["workers"][WORKER_ID]["execution_context"]["principal"] = "user:attacker"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(TrustedRecoveryError, match="principal|identity|changed"):
        await TrustedRecoverySession.prepare(world.recovery_config())


async def test_d1_missing_idempotency_store_fails_closed(tmp_path: Path) -> None:
    world = World(tmp_path)
    await world.create_suspension()
    await world.release_a()
    await world.idempotency.teardown()
    (world.state_root / "idempotency.db").unlink()
    with pytest.raises(TrustedRecoveryError, match="idempotency"):
        await TrustedRecoverySession.prepare(world.recovery_config())


async def test_d1_tampered_tool_binding_fails_closed(tmp_path: Path) -> None:
    world = World(tmp_path)
    suspension = await world.create_suspension()
    await world.release_a()
    db = world.suspension_store.path
    with sqlite3.connect(db) as connection:
        row = connection.execute(
            "SELECT payload FROM execution_suspensions WHERE suspension_id=?",
            (suspension.suspension_id,),
        ).fetchone()
        payload = json.loads(row[0])
        payload["pending_invocations"][0]["tool_binding_digest"] = "0" * 64
        connection.execute(
            "UPDATE execution_suspensions SET payload=? WHERE suspension_id=?",
            (json.dumps(payload), suspension.suspension_id),
        )
        connection.commit()
    with pytest.raises(TrustedRecoveryError, match="binding"):
        await TrustedRecoverySession.prepare(world.recovery_config())
