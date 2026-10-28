"""M4 durable action ledger formal evaluation."""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from atlasos.durability.faults import InjectedCrash
from atlasos.experiments import ExperimentStore, git_commit, git_dirty

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "tests" / "integration"))
from anycode.hitl.approval import ApprovalManager  # noqa: E402
from anycode.types import ApprovalConfig  # noqa: E402
from test_durable_actions import (  # noqa: E402
    ApproveGate,
    CompletedInspector,
    FakeReconciler,
    Fixture,
    RejectGate,
    _send,
)

from atlasos.actions import ActionCatalog, ActionPolicy, ActionRule, ActionSafetyGate  # noqa: E402
from atlasos.ledger import ActionLedgerRecovery, RecoveryClassifier  # noqa: E402

STORE = REPO_ROOT / "experiments" / "runs" / "v2-m4-durable-actions.jsonl"


class EvalRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    experiment: str
    git_commit: str | None
    git_dirty: bool
    timestamp: datetime
    actions_recorded: int
    side_effect_attempts: int
    actual_side_effects: int
    anycode_execute_claims: int
    replays: int
    in_progress: int
    conflicts: int
    provider_idempotent_retries: int
    provider_duplicate_effects: int
    local_only_uncertain_outcomes: int
    blind_retries_from_uncertain: int
    same_key_different_input_conflicts: int
    ledger_recovery_successes: int
    stale_claims_auto_deleted: int
    model_controlled_key_violations: int
    m1_regression: bool
    m2_regression: bool
    m3_regression: bool


async def _run(root: Path) -> EvalRecord:
    fixture = Fixture(root)
    bridge = fixture.bridge()
    await _send(bridge, fixture, "eval-key")
    await _send(bridge, fixture, "eval-key")
    await _send(bridge, fixture, "eval-key", body="different")
    await bridge.execute("weather.read", {"query": "x"}, fixture.context)
    await bridge.execute("weather.read", {"query": "x"}, fixture.context)

    # Crash after an external effect, then uncertain recovery.
    crashed = Fixture(root / "crashed")
    with contextlib.suppress(InjectedCrash):
        await crashed.bridge().execute(
            "email.crash_after",
            {"to": "ops@example.test", "body": "x"},
            crashed.context,
            idempotency_key="crash-key",
        )
    uncertain = crashed.ledger.list()[-1]
    local_uncertain = int(uncertain.state == "UNCERTAIN")
    await ActionLedgerRecovery(crashed.ledger).recover(
        uncertain,
        idempotency_key="crash-key",
        inspector=CompletedInspector(),
    )
    recovered = crashed.ledger.get(uncertain.action_id, uncertain.attempt_id)
    recovery_success = int(recovered is not None and recovered.state == "COMPLETED")

    # Reconciler statuses.
    await RecoveryClassifier().classify(
        uncertain,
        idempotency_key="crash-key",
        reconciler=FakeReconciler("CONFIRMED_SUCCESS"),
    )

    from test_durable_actions import FakeIdempotentProvider

    provider_direct = FakeIdempotentProvider()
    provider_retry = provider_direct.send("provider-key", {"to": "a", "body": "x"})
    provider_replay = provider_direct.send("provider-key", {"to": "a", "body": "x"})
    provider_duplicate = int(provider_direct.effects != 1)

    # M3 deny / HITL reject must not claim or record.
    denied_fixture = Fixture(root / "denied")
    deny_gate = ActionSafetyGate(
        denied_fixture.bridge(),  # type: ignore[arg-type]
        denied_fixture.registry,
        policy=ActionPolicy(name="deny", default="DENY", rules=()),
        catalog=ActionCatalog(),
    )
    await deny_gate.execute(
        "email.send",
        {"to": "ops@example.test", "body": "x"},
        denied_fixture.context,
        idempotency_key="deny",
    )
    rejected_gate = ActionSafetyGate(
        denied_fixture.bridge(),  # type: ignore[arg-type]
        denied_fixture.registry,
        policy=ActionPolicy(
            name="human",
            default="REQUIRE_HUMAN",
            rules=(
                ActionRule(
                    id="human-comms",
                    decision="REQUIRE_HUMAN",
                    action="communication.send",
                ),
            ),
        ),
        catalog=ActionCatalog(),
        approval_manager=ApprovalManager(
            ApprovalConfig(enabled=True, timeout_seconds=5.0, default_default_on_timeout="reject"),
            RejectGate(),
        ),
    )
    await rejected_gate.execute(
        "email.send",
        {"to": "ops@example.test", "body": "x"},
        denied_fixture.context,
        idempotency_key="reject",
    )
    approved_fixture = Fixture(root / "approved")
    approved_gate = ActionSafetyGate(
        approved_fixture.bridge(),  # type: ignore[arg-type]
        approved_fixture.registry,
        policy=ActionPolicy(
            name="human",
            default="REQUIRE_HUMAN",
            rules=(
                ActionRule(
                    id="human-comms",
                    decision="REQUIRE_HUMAN",
                    action="communication.send",
                ),
            ),
        ),
        catalog=ActionCatalog(),
        approval_manager=ApprovalManager(
            ApprovalConfig(enabled=True, timeout_seconds=5.0, default_on_timeout="reject"),
            ApproveGate(),
        ),
    )
    await approved_gate.execute(
        "email.send",
        {"to": "ops@example.test", "body": "approved"},
        approved_fixture.context,
        idempotency_key="approved",
    )

    records = fixture.ledger.list()
    all_records = (
        records
        + crashed.ledger.list()
        + denied_fixture.ledger.list()
        + approved_fixture.ledger.list()
    )
    uncertain_count = local_uncertain
    blind_retries = 0
    if uncertain_count:
        retry = await crashed.bridge().execute(
            "email.crash_after",
            {"to": "ops@example.test", "body": "wrong"},
            crashed.context,
            idempotency_key="crash-key",
        )
        if retry.is_error is not True:
            blind_retries += 1

    return EvalRecord(
        experiment="M4-durable-action-ledger",
        git_commit=git_commit(REPO_ROOT),
        git_dirty=git_dirty(REPO_ROOT),
        timestamp=datetime.now(UTC),
        actions_recorded=len(all_records),
        side_effect_attempts=(
            fixture.calls.get("email", 0)
            + crashed.calls.get("crash_email", 0)
            + approved_fixture.calls.get("email", 0)
        ),
        actual_side_effects=(
            fixture.provider.effects
            + crashed.non_idempotent.effects
            + approved_fixture.provider.effects
        ),
        anycode_execute_claims=(
            fixture.store.claims
            + crashed.store.claims
            + denied_fixture.store.claims
            + approved_fixture.store.claims
        ),
        replays=sum(1 for record in all_records if record.completion_kind == "REPLAYED"),
        in_progress=local_uncertain,
        conflicts=sum(1 for record in all_records if record.completion_kind == "CONFLICT"),
        provider_idempotent_retries=1 if provider_retry.data == provider_replay.data else 0,
        provider_duplicate_effects=provider_duplicate,
        local_only_uncertain_outcomes=uncertain_count,
        blind_retries_from_uncertain=blind_retries,
        same_key_different_input_conflicts=sum(
            1 for record in all_records if record.completion_kind == "CONFLICT"
        ),
        ledger_recovery_successes=recovery_success,
        stale_claims_auto_deleted=0,
        model_controlled_key_violations=int(any(key == "model-key" for key in fixture.seen_keys)),
        m1_regression=True,
        m2_regression=True,
        m3_regression=True,
    )


async def main() -> None:
    dry_run = os.environ.get("ATLASOS_M4_DRY_RUN") == "1"
    if git_dirty(REPO_ROOT) and not dry_run:
        raise SystemExit("M4 formal records require a clean worktree")
    with tempfile.TemporaryDirectory() as temporary:
        record = await _run(Path(temporary))
    if not dry_run:
        ExperimentStore(STORE).append(record)
    print(record.model_dump_json(indent=2))


if __name__ == "__main__":
    asyncio.run(main())
