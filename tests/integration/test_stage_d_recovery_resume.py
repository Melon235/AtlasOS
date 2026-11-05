"""Stage D D2: M5 ownership and durable HITL settlement integration."""

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
    write_policy,
)

from atlasos.approvals.resume import resume_execution_suspension
from atlasos.durability.faults import InjectedCrash
from atlasos.sandbox import SandboxLostError, SandboxNetworkPolicyViolation, SandboxOwnershipBusy
from atlasos.trusted_recovery import TrustedRecoveryError, TrustedRecoverySession


@pytest_asyncio.fixture(autouse=True)
async def _close_stage_d_worlds():
    yield
    await close_open_worlds()


async def test_d2_approved_resume_executes_original_write_once(tmp_path: Path) -> None:
    world = World(tmp_path)
    suspension = await world.create_suspension()
    await world.approve()
    await world.release_a()
    session = await TrustedRecoverySession.prepare(world.recovery_config())
    before_writes = world.provider.write_calls
    outcome = await session.resume()
    assert outcome.status == "settled"
    assert outcome.suspension is not None
    assert outcome.suspension.state == "CLEARED"
    assert world.provider.write_calls == before_writes + 1
    assert world.provider.create_count == 1
    assert session.sandbox_id == world.sandbox_id
    assert session.binding_id == world.binding_id
    assert session.continuity_digest == suspension.sandbox_binding_digest
    assert session.network_verified is True
    assert world.sandbox_id is not None
    target = world.provider.workspaces[world.sandbox_id] / "workspace" / "project" / "recovered.txt"
    assert target.read_text(encoding="utf-8") == "approved recovery\n"
    ledger = session.ledger.list()
    assert len(ledger) == 1
    assert ledger[0].state == "COMPLETED"
    conversation = session.runtime.conversation_store.load(TASK_ID)
    assert conversation is not None
    assert conversation[1]["content"][0]["tool_use_id"] == "write-call"
    assert session.suspension_store.get(suspension.suspension_id).state == "CLEARED"  # type: ignore[union-attr]
    assert await session.dispose_sandbox() is True
    assert world.provider.destroy_count == 1
    await session.close()


async def test_d2_pending_approval_returns_waiting_without_sandbox_connect(
    tmp_path: Path,
) -> None:
    world = World(tmp_path)
    await world.create_suspension()
    await world.release_a()
    session = await TrustedRecoverySession.prepare(world.recovery_config())
    before = (
        world.provider.create_count,
        world.provider.inspect_calls,
        world.provider.attach_calls,
        world.provider.detach_calls,
        world.provider.write_calls,
    )
    outcome = await session.resume()
    assert outcome.status == "waiting"
    assert (
        world.provider.create_count,
        world.provider.inspect_calls,
        world.provider.attach_calls,
        world.provider.detach_calls,
        world.provider.write_calls,
    ) == before
    assert session.manager.bindings == {}
    await session.close()


async def test_d2_rejected_resume_has_no_side_effect(tmp_path: Path) -> None:
    world = World(tmp_path)
    await world.create_suspension()
    await world.reject()
    await world.release_a()
    session = await TrustedRecoverySession.prepare(world.recovery_config())
    before_writes = world.provider.write_calls
    outcome = await session.resume()
    assert outcome.status == "settled"
    assert world.provider.write_calls == before_writes
    assert session.ledger.list() == ()
    assert world.sandbox_id is not None
    target = world.provider.workspaces[world.sandbox_id] / "workspace" / "project" / "recovered.txt"
    assert target.exists() is False
    conversation = session.runtime.conversation_store.load(TASK_ID)
    assert conversation is not None
    assert conversation[1]["content"][0]["is_error"] is True
    assert "Rejected" in conversation[1]["content"][0]["content"]
    assert await session.dispose_sandbox() is True
    await session.close()


async def test_d2_lost_sandbox_fails_closed_without_creation(tmp_path: Path) -> None:
    world = World(tmp_path)
    await world.create_suspension()
    assert world.manager is not None
    assert world.sandbox_id is not None
    await world.approve()
    handle = world.provider.handles[world.sandbox_id]
    world.provider.lose(handle)
    await world.manager.mark_lost(TASK_ID)
    await world.release_a()
    session = await TrustedRecoverySession.prepare(world.recovery_config())
    before_writes = world.provider.write_calls
    with pytest.raises(SandboxLostError):
        await session.resume()
    assert world.provider.create_count == 1
    assert world.provider.write_calls == before_writes
    await session.close()


async def test_d2_ownership_busy_fails_closed_without_provider_calls(tmp_path: Path) -> None:
    world = World(tmp_path)
    await world.create_suspension()
    await world.approve()
    assert world.manager is not None
    session = await TrustedRecoverySession.prepare(world.recovery_config())
    before = (
        world.provider.create_count,
        world.provider.inspect_calls,
        world.provider.attach_calls,
        world.provider.detach_calls,
        world.provider.write_calls,
    )
    with pytest.raises(SandboxOwnershipBusy):
        await session.resume()
    assert (
        world.provider.create_count,
        world.provider.inspect_calls,
        world.provider.attach_calls,
        world.provider.detach_calls,
        world.provider.write_calls,
    ) == before
    await session.close()
    await world.manager.release_ownership(TASK_ID)


async def test_d2_settled_suspension_backfills_without_tool_execution(tmp_path: Path) -> None:
    world = World(tmp_path)
    suspension = await world.create_suspension()
    await world.approve()
    assert world.runtime is not None
    assert world.runtime.suspension_coordinator is not None
    assert world.context is not None

    def crash_after_settlement(point: str) -> None:
        if point == "resume_after_settlement":
            raise InjectedCrash("stage-d crash after durable settlement")

    with pytest.raises(InjectedCrash):
        await resume_execution_suspension(
            suspension.suspension_id,
            runtime=world.runtime,
            coordinator=world.runtime.suspension_coordinator,
            context=world.context,
            owner="stage-d-crash",
            fault_hook=crash_after_settlement,
        )
    persisted = world.suspension_store.get(suspension.suspension_id)
    assert persisted is not None
    assert persisted.state == "SETTLED"
    assert persisted.settlement is not None
    await world.manager.release_ownership(TASK_ID)  # type: ignore[union-attr]
    session = await TrustedRecoverySession.prepare(world.recovery_config())
    before_writes = world.provider.write_calls
    before_inspect = world.provider.inspect_calls
    before_attach = world.provider.attach_calls
    outcome = await session.resume()
    assert outcome.status == "settled"
    assert world.provider.write_calls == before_writes
    assert world.provider.inspect_calls == before_inspect
    assert world.provider.attach_calls == before_attach
    assert session.reattached is False
    assert session.runtime.conversation_store.load(TASK_ID) is not None
    assert session.suspension_store.get(suspension.suspension_id).state == "CLEARED"  # type: ignore[union-attr]
    await session.close()


async def test_d2_network_policy_change_fails_before_execution(tmp_path: Path) -> None:
    world = World(tmp_path)
    await world.create_suspension()
    await world.approve()
    await world.release_a()
    assert world.sandbox_id is not None
    world.provider.network_policies[world.sandbox_id] = {
        "default_action": "allow",
        "egress": [],
    }
    session = await TrustedRecoverySession.prepare(world.recovery_config())
    before_writes = world.provider.write_calls
    with pytest.raises(SandboxNetworkPolicyViolation):
        await session.resume()
    assert world.provider.create_count == 1
    assert world.provider.write_calls == before_writes
    await session.close()


async def test_d2_continuity_change_fails_before_execution(tmp_path: Path) -> None:
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
    session = await TrustedRecoverySession.prepare(world.recovery_config())
    before_writes = world.provider.write_calls
    with pytest.raises(TrustedRecoveryError, match="continuity"):
        await session.resume()
    assert world.provider.create_count == 1
    assert world.provider.write_calls == before_writes
    await session.close()


async def test_d2_cleared_suspension_does_not_execute_again(tmp_path: Path) -> None:
    world = World(tmp_path)
    suspension = await world.create_suspension()
    await world.approve()
    await world.release_a()
    first = await TrustedRecoverySession.prepare(world.recovery_config())
    assert (await first.resume()).status == "settled"
    before_writes = world.provider.write_calls
    second = await TrustedRecoverySession.prepare(world.recovery_config())
    before = (
        world.provider.create_count,
        world.provider.inspect_calls,
        world.provider.attach_calls,
        world.provider.write_calls,
    )
    outcome = await second.resume()
    assert outcome.status == "cleared"
    assert (
        world.provider.create_count,
        world.provider.inspect_calls,
        world.provider.attach_calls,
        world.provider.write_calls,
    ) == before
    assert world.provider.write_calls == before_writes
    assert second.suspension_store.get(suspension.suspension_id).state == "CLEARED"  # type: ignore[union-attr]
    await second.close()
    await first.close()


async def test_d2_policy_relaxation_is_rejected_at_resume(tmp_path: Path) -> None:
    world = World(tmp_path)
    await world.create_suspension()
    await world.approve()
    await world.release_a()
    write_policy(world.policy_path, write_decision="ALLOW")
    session = await TrustedRecoverySession.prepare(world.recovery_config())
    before_writes = world.provider.write_calls
    outcome = await session.resume()
    assert outcome.status == "stale"
    assert world.provider.write_calls == before_writes
    await session.manager.close(TASK_ID)
    await session.close()
