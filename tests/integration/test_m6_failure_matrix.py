"""M6 failure-injection matrix F0-F13: interrupted suspension and settlement windows."""

# The M6 harness always constructs the durable stores; optional attributes are configured here.
# pyright: reportOptionalMemberAccess=false

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from tests.helpers.m6_harness import M6World

from atlasos.approvals.models import ApprovalStoreUnavailable
from atlasos.durability.faults import InjectedCrash
from atlasos.suspension.models import SuspensionStoreUnavailable
from atlasos.suspension.store import SQLiteSuspensionStore


class MutableClock:
    def __init__(self) -> None:
        self.current = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current = self.current + timedelta(seconds=seconds)


async def _ready(world: M6World, *, policy=None):
    runtime = world.runtime(policy=policy)
    await world.disclose(runtime, "send external email notification")
    return runtime


def _email_call(world: M6World):
    return [world.dispatch_call(0, "call", "email.send", {"to": "ops@example.test", "body": "x"})]


async def test_f0_crash_before_suspension_durable(tmp_path: Path) -> None:
    world = M6World(tmp_path / "world")
    runtime = await _ready(world)

    def hook(point: str) -> None:
        if point == "suspension_before_persist":
            raise InjectedCrash("F0")

    assert runtime.suspension_coordinator is not None
    runtime.suspension_coordinator._fault_hook = hook  # noqa: SLF001
    with pytest.raises(InjectedCrash):
        await world.suspend(runtime, _email_call(world))
    assert runtime.suspension_store.list_active() == ()
    assert runtime.approval_store.list_all() == ()
    assert runtime.action_ledger.list() == ()
    assert world.provider.effects == 0


async def test_f1_suspension_durable_approval_missing(tmp_path: Path) -> None:
    world = M6World(tmp_path / "world")
    runtime = await _ready(world)

    def hook(point: str) -> None:
        if point == "suspension_after_persist":
            raise InjectedCrash("F1")

    assert runtime.suspension_coordinator is not None
    runtime.suspension_coordinator._fault_hook = hook  # noqa: SLF001
    with pytest.raises(InjectedCrash):
        await world.suspend(runtime, _email_call(world))
    assert len(runtime.suspension_store.list_active()) == 1
    assert runtime.approval_store.list_all() == ()

    suspension = runtime.suspension_store.list_active()[0]
    runtime.suspension_coordinator._fault_hook = None  # noqa: SLF001
    state = runtime.suspension_coordinator.reconcile(suspension)
    assert state.all_terminal is False
    assert len(runtime.approval_store.list_for_suspension(suspension.suspension_id)) == 1
    world.approve(runtime, suspension)
    outcome = await world.resume(runtime, suspension)
    assert outcome.status == "settled"
    assert world.provider.effects == 1


async def test_f2_approval_pending_before_run_paused(tmp_path: Path) -> None:
    world = M6World(tmp_path / "world")
    runtime = await _ready(world)

    def hook(point: str) -> None:
        if point == "suspension_after_approvals":
            raise InjectedCrash("F2")

    assert runtime.suspension_coordinator is not None
    runtime.suspension_coordinator._fault_hook = hook  # noqa: SLF001
    with pytest.raises(InjectedCrash):
        await world.suspend(runtime, _email_call(world))
    suspension = runtime.suspension_store.list_active()[0]
    assert runtime.suspension_store.run_state("run-m6") is None
    pending = runtime.approval_store.list_pending(suspension_id=suspension.suspension_id)
    assert len(pending) == 1
    runtime.suspension_coordinator._fault_hook = None  # noqa: SLF001
    world.approve(runtime, suspension)
    outcome = await world.resume(runtime, suspension)
    assert outcome.status == "settled"
    assert world.provider.effects == 1


async def test_f4_f26_approved_before_wake_signal(tmp_path: Path) -> None:
    world = M6World(tmp_path / "world")
    runtime = await _ready(world)
    suspension = await world.suspend(runtime, _email_call(world))
    world.approve(runtime, suspension)
    assert runtime.suspension_coordinator is not None
    resumable = runtime.suspension_coordinator.list_resumable()
    assert [item.suspension_id for item in resumable] == [suspension.suspension_id]
    outcome = await world.resume(runtime, suspension)
    assert outcome.status == "settled"
    assert world.provider.effects == 1


async def test_f5_f6_crash_before_and_after_m3_revalidation(tmp_path: Path) -> None:
    for point in ("resume_before_revalidate", "resume_after_revalidate"):
        clock = MutableClock()
        world = M6World(tmp_path / point, clock=clock)
        runtime = await _ready(world)
        suspension = await world.suspend(runtime, _email_call(world))
        world.approve(runtime, suspension)

        def hook(observed: str, target: str = point) -> None:
            if observed == target:
                raise InjectedCrash(target)

        with pytest.raises(InjectedCrash):
            await world.resume(runtime, suspension, fault_hook=hook)
        assert world.provider.effects == 0
        assert runtime.action_ledger.list() == ()
        assert runtime.suspension_store.get(suspension.suspension_id).state == "SETTLING"  # type: ignore[union-attr]
        busy = await world.resume(runtime, suspension)
        assert busy.status == "busy"
        clock.advance(61)
        resumed = await world.resume(runtime, suspension)
        assert resumed.status == "settled"
        assert world.provider.effects == 1


async def test_f7_m4_claim_before_tool_execution(tmp_path: Path) -> None:
    clock = MutableClock()
    world = M6World(tmp_path / "world", clock=clock)
    policy = world.policy(human=("crash.before",))
    runtime = await _ready(world, policy=policy)
    await world.disclose(runtime, "crash before external effect")
    calls = [world.dispatch_call(0, "call", "crash.before", {"value": "x"})]
    suspension = await world.suspend(runtime, calls)
    world.approve(runtime, suspension)
    with pytest.raises(InjectedCrash):
        await world.resume(runtime, suspension)
    assert world.provider.effects == 0
    assert runtime.action_ledger.list()[-1].state == "UNCERTAIN"
    clock.advance(61)
    outcome = await world.resume(runtime, suspension)
    assert outcome.status == "settled"
    assert world.provider.effects == 0
    assert runtime.m6_metrics is not None
    assert runtime.m6_metrics.m4_uncertain == 1
    result = runtime.conversation_store.load("task-m6")[1]["content"][0]  # type: ignore[index]
    assert result["is_error"] is True


async def test_f8_f9_side_effect_before_m4_complete_then_replay(tmp_path: Path) -> None:
    # F8: external effect happened, M4 did not complete.
    clock = MutableClock()
    world = M6World(tmp_path / "f8", clock=clock)
    policy = world.policy(human=("crash.mutate",))
    runtime = await _ready(world, policy=policy)
    await world.disclose(runtime, "crash after external effect")
    calls = [world.dispatch_call(0, "call", "crash.mutate", {"value": "x"})]
    suspension = await world.suspend(runtime, calls)
    world.approve(runtime, suspension)
    outcome = await world.resume(runtime, suspension)
    assert outcome.status == "settled"
    assert world.non_idempotent.effects == 1
    assert runtime.action_ledger.list()[-1].state == "UNCERTAIN"
    assert runtime.m6_metrics is not None
    assert runtime.m6_metrics.m4_uncertain == 1
    detail = runtime.conversation_store.load("task-m6")[1]["content"][0]  # type: ignore[index]
    assert detail["is_error"] is True
    # A later attempt still cannot duplicate the uncertain external effect.
    assert runtime.m6_metrics.duplicate_provider_side_effects == 0


async def test_f9_f10_m4_complete_before_result_commit_and_before_clear(
    tmp_path: Path,
) -> None:
    # F9: crash after M4 complete, before the settlement is durable.
    clock = MutableClock()
    world = M6World(tmp_path / "f9", clock=clock)
    runtime = await _ready(world)
    suspension = await world.suspend(runtime, _email_call(world))
    world.approve(runtime, suspension)

    def crash_after_execute(point: str) -> None:
        if point == "resume_after_execute":
            raise InjectedCrash("F9")

    with pytest.raises(InjectedCrash):
        await world.resume(runtime, suspension, fault_hook=crash_after_execute)
    assert world.provider.effects == 1
    assert runtime.conversation_store.load("task-m6") is None
    assert runtime.suspension_store.get(suspension.suspension_id).state == "SETTLING"  # type: ignore[union-attr]
    clock.advance(61)
    resumed = await world.resume(runtime, suspension)
    assert resumed.status == "settled"
    assert world.provider.effects == 1
    assert runtime.m6_metrics is not None
    assert runtime.m6_metrics.m4_replays >= 1
    assert runtime.conversation_store.load("task-m6") is not None

    # F10: settlement durable, crash before the suspension is cleared.
    world10 = M6World(tmp_path / "f10")
    runtime10 = await _ready(world10)
    suspension10 = await world10.suspend(runtime10, _email_call(world10))
    world10.approve(runtime10, suspension10)

    def crash_after_settlement(point: str) -> None:
        if point == "resume_after_settlement":
            raise InjectedCrash("F10")

    with pytest.raises(InjectedCrash):
        await world10.resume(runtime10, suspension10, fault_hook=crash_after_settlement)
    assert world10.provider.effects == 1
    stored = runtime10.suspension_store.get(suspension10.suspension_id)
    assert stored is not None and stored.state == "SETTLED" and stored.settlement is not None
    assert runtime10.conversation_store.load("task-m6") is None
    recovered = await world10.resume(runtime10, suspension10)
    assert recovered.status == "settled"
    assert world10.provider.effects == 1
    assert runtime10.conversation_store.load("task-m6") is not None
    assert runtime10.m6_metrics is not None
    assert runtime10.m6_metrics.tool_results_committed == 1
    assert runtime10.m6_metrics.suspensions_cleared == 1


class _FailingApprovalStore:
    def create_if_absent(self, challenge):  # noqa: ANN001
        raise ApprovalStoreUnavailable("simulated approval store outage")


async def test_f12_approval_store_unavailable_fails_closed(tmp_path: Path) -> None:
    world = M6World(tmp_path / "world")
    runtime = world.runtime(approval_store=_FailingApprovalStore())  # type: ignore[arg-type]
    await world.disclose(runtime, "send external email notification")
    with pytest.raises(ApprovalStoreUnavailable):
        await world.suspend(runtime, _email_call(world))
    assert world.provider.effects == 0
    assert runtime.action_ledger.list() == ()
    suspensions = runtime.suspension_store.list_active()
    assert len(suspensions) == 1
    assert world.approval_store.list_all() == ()
    # Reconciliation with a working approval store recovers the durable suspension.
    recovered_runtime = world.runtime(
        approval_store=world.approval_store,
        suspension_store=runtime.suspension_store,
        runtime_instance_id="runtime-recovered",
    )
    await world.disclose(recovered_runtime, "send external email notification")
    state = recovered_runtime.suspension_coordinator.reconcile(suspensions[0])  # type: ignore[union-attr]
    assert state.all_terminal is False
    world.approve(recovered_runtime, suspensions[0])
    outcome = await world.resume(recovered_runtime, suspensions[0])
    assert outcome.status == "settled"
    assert world.provider.effects == 1


async def test_f13_runtime_store_unavailable_fails_closed(tmp_path: Path) -> None:
    blocked = tmp_path / "runtime.db"
    blocked.mkdir()
    with pytest.raises(SuspensionStoreUnavailable):
        SQLiteSuspensionStore(blocked)
    world = M6World(tmp_path / "world")
    runtime = await _ready(world)
    assert runtime.action_ledger.list() == ()
    assert world.provider.effects == 0
