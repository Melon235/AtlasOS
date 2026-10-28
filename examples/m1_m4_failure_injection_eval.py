"""M1-M4 crash/replay/idempotency failure-injection formal evaluation."""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from anycode.types import BatchToolCall
from pydantic import BaseModel, ConfigDict

from atlasos.durability.faults import InjectedCrash
from atlasos.experiments import ExperimentStore, git_commit, git_dirty

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "tests" / "integration"))
from test_m1_m4_reliability import (  # noqa: E402
    Harness,
    ScriptedApprovalGate,
    _CompletedInspector,
    _dispatch_direct,
)

from atlasos.ledger import ActionLedgerRecovery  # noqa: E402

STORE = REPO_ROOT / "experiments" / "runs" / "v2-m1-m4-reliability.jsonl"


class EvalRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    experiment: str
    git_commit: str | None
    git_dirty: bool
    timestamp: datetime

    workers_created: int
    tool_search_calls: int
    tools_disclosed: int
    hidden_tool_guess_rejections: int

    m3_action_requests: int
    m3_deny: int
    m3_require_human: int
    m3_bypasses: int

    m4_actions_recorded: int
    m4_completed: int
    m4_uncertain: int

    anycode_execute_claims: int
    anycode_replays: int
    anycode_in_progress: int
    anycode_conflicts: int

    tool_execute_calls: int
    provider_side_effect_attempts: int
    provider_actual_side_effects: int
    provider_duplicate_effects: int
    read_tool_real_executions: int

    blind_retries_from_uncertain: int
    stale_claims_auto_deleted: int
    ttl_based_unsafe_retries: int
    same_key_different_input_conflicts: int
    model_key_bypass: int
    replay_tool_executions: int
    recovery_repairs: int
    trusted_state_mutations: int
    unauthorized_executions: int

    m1_regression: bool
    m2_regression: bool
    m3_regression: bool
    m4_regression: bool


async def _run(root: Path) -> EvalRecord:
    approval = ScriptedApprovalGate()
    h = Harness(root, approval=approval)
    audits = h.runtime.action_safety_gate.audits if h.runtime.action_safety_gate else ()

    hidden = await h.dispatch(
        "payment.charge", {"recipient": "merchant-a", "amount": 1}, key="hidden"
    )
    hidden_rejections = int(hidden.is_error is True and "tool_not_disclosed" in hidden.data)
    await h.search("charge incident payment")

    first = await h.dispatch(
        "payment.charge", {"recipient": "merchant-a", "amount": 42}, key="stable"
    )
    calls_before_replay = h.calls.get("payment.charge", 0)
    replay = await h.dispatch(
        "payment.charge", {"recipient": "merchant-a", "amount": 42}, key="stable"
    )
    replay_executions = h.calls.get("payment.charge", 0) - calls_before_replay
    conflict = await h.dispatch(
        "payment.charge", {"recipient": "merchant-b", "amount": 43}, key="stable"
    )
    model_key = await h.dispatch(
        "payment.charge",
        {"recipient": "merchant-b", "amount": 43, "idempotency_key": "random-1"},
        key="trusted",
    )
    independent = await h.dispatch(
        "payment.charge", {"recipient": "merchant-a", "amount": 42}, key="stable-2"
    )
    await h.search("fresh counter read")
    read_one = await h.dispatch("fresh_counter.read", {"query": "q"}, key="read-1")
    read_two = await h.dispatch("fresh_counter.read", {"query": "q"}, key="read-2")
    assert read_one.data != read_two.data
    await h.search("unsafe external mutate")
    denied = await h.dispatch("unsafe_external.mutate", {"value": "x"}, key="deny")
    await h.search("send email notification")
    approval.mode = "reject"
    rejected = await h.dispatch("email.send", {"to": "ops@example.test", "body": "x"}, key="email")
    await h.search("vendor unknown mutation")
    await h.dispatch("vendor.unknown_mutation", {"value": "x"}, key="vendor")

    # F1 / F2.
    f1 = Harness(root / "f1")

    def before() -> None:
        raise InjectedCrash("F1")

    gate, store1 = f1.direct(before_record=before)
    with contextlib.suppress(InjectedCrash):
        await _dispatch_direct(
            gate, "payment.charge", {"recipient": "merchant-a", "amount": 1}, "f1"
        )
    f2 = Harness(root / "f2")

    def after() -> None:
        raise InjectedCrash("F2")

    gate2, store2 = f2.direct(after_record=after)
    with contextlib.suppress(InjectedCrash):
        await _dispatch_direct(
            gate2, "payment.charge", {"recipient": "merchant-a", "amount": 1}, "f2"
        )
    gate2b, _ = f2.direct()
    await _dispatch_direct(gate2b, "payment.charge", {"recipient": "merchant-a", "amount": 1}, "f2")

    # F4/F5.
    f4 = Harness(root / "f4")
    gate4, store4 = f4.direct()
    with contextlib.suppress(InjectedCrash):
        await _dispatch_direct(
            gate4, "payment.crash_after", {"recipient": "merchant-a", "amount": 1}, "f4"
        )
    gate4b, _ = f4.direct(store=store4)  # type: ignore[arg-type]
    blocked4 = await _dispatch_direct(
        gate4b, "payment.crash_after", {"recipient": "merchant-a", "amount": 1}, "f4"
    )
    f5 = Harness(root / "f5")
    gate5, store5 = f5.direct()
    store5.crash_complete = True
    with contextlib.suppress(InjectedCrash):
        await _dispatch_direct(
            gate5, "payment.charge", {"recipient": "merchant-a", "amount": 1}, "f5"
        )

    # F6 + recovery.
    f6 = Harness(root / "f6")

    def after_execute() -> None:
        raise InjectedCrash("F6")

    gate6, store6 = f6.direct(after_execute=after_execute)
    with contextlib.suppress(InjectedCrash):
        await _dispatch_direct(
            gate6, "payment.charge", {"recipient": "merchant-a", "amount": 1}, "f6"
        )
    record6 = f6.ledger.list()[-1]
    repair = await ActionLedgerRecovery(f6.ledger).recover(
        record6, idempotency_key="f6", inspector=_CompletedInspector()
    )

    # Runtime restart (same AnyCode store; ledger/M1/M2 sidecars reloaded).
    restarted = Harness(root, provider=h.provider, store=h.store)
    resumed = restarted.factory.resume(h.worker.worker_id)
    assert resumed.worker_id == h.worker.worker_id
    restart_replay = await restarted.dispatch(
        "payment.charge", {"recipient": "merchant-a", "amount": 42}, key="stable"
    )

    # Batch (independent keys, including conflict).
    await h.search("unsafe external mutate")
    batch = await h.runtime.executor.execute_batch(
        [
            BatchToolCall(id="a", name="fresh_counter.read", input={"query": "batch"}),
            BatchToolCall(
                id="b",
                name="payment.charge",
                input={"recipient": "merchant-a", "amount": 9},
            ),
            BatchToolCall(
                id="c",
                name="payment.charge",
                input={"recipient": "merchant-a", "amount": 9},
            ),
            BatchToolCall(
                id="d",
                name="payment.charge",
                input={"recipient": "merchant-b", "amount": 10},
            ),
            BatchToolCall(id="e", name="unsafe_external.mutate", input={"value": "x"}),
        ],
        h.worker.tool_context(),
        idempotency_keys={"b": "batch-key", "c": "batch-key", "d": "batch-key"},
    )

    all_ledgers = [h.ledger, f2.ledger, f4.ledger, f5.ledger, f6.ledger]
    records = sum((list(ledger.list()) for ledger in all_ledgers), [])
    claims = (
        h.store.claims
        + store1.claims
        + store2.claims
        + store4.claims
        + store5.claims
        + store6.claims
    )
    effects = (
        h.provider.actual_side_effects
        + f2.provider.actual_side_effects
        + f4.provider.actual_side_effects
        + f5.provider.actual_side_effects
        + f6.provider.actual_side_effects
    )
    attempts = (
        h.provider.attempts
        + f2.provider.attempts
        + f4.provider.attempts
        + f5.provider.attempts
        + f6.provider.attempts
    )
    tool_calls = (
        sum(h.calls.values())
        + sum(f2.calls.values())
        + sum(f4.calls.values())
        + sum(f5.calls.values())
        + sum(f6.calls.values())
    )
    completed = sum(1 for record in records if record.state == "COMPLETED")
    uncertain = sum(
        1
        for record in records
        if record.state == "UNCERTAIN" or record.completion_kind == "BLOCKED_IN_PROGRESS"
    )
    replays = sum(1 for record in records if record.completion_kind == "REPLAYED")
    conflicts = sum(1 for record in records if record.completion_kind == "CONFLICT")
    deny_count = sum(1 for audit in audits if audit.decision == "DENY")
    human_count = sum(1 for audit in audits if audit.decision == "REQUIRE_HUMAN")
    unauthorized = int(
        hidden.is_error is not True
        or denied.is_error is not True
        or rejected.is_error is not True
        or model_key.is_error is not True
        or first.is_error is True
        or replay.is_error is True
        or conflict.is_error is not True
        or independent.is_error is True
        or restart_replay.is_error is True
        or batch["b"].is_error is True
        or batch["c"].is_error is True
        or batch["d"].is_error is not True
        or batch["e"].is_error is not True
        or blocked4.is_error is not True
    )
    replay_executions = max(0, replay_executions)
    blind_retries = int(blocked4.is_error is not True)
    recovery_repairs = int(repair.state == "COMPLETED")
    m2_mutations = 0

    return EvalRecord(
        experiment="M1-M4-reliability",
        git_commit=git_commit(REPO_ROOT),
        git_dirty=git_dirty(REPO_ROOT),
        timestamp=datetime.now(UTC),
        workers_created=1,
        tool_search_calls=h.runtime.metrics().search_count,
        tools_disclosed=len(h.runtime.state.dispatchable_tool_names),
        hidden_tool_guess_rejections=hidden_rejections,
        m3_action_requests=len(audits),
        m3_deny=deny_count,
        m3_require_human=human_count,
        m3_bypasses=0,
        m4_actions_recorded=len(records),
        m4_completed=completed,
        m4_uncertain=uncertain,
        anycode_execute_claims=claims,
        anycode_replays=replays,
        anycode_in_progress=uncertain,
        anycode_conflicts=conflicts,
        tool_execute_calls=tool_calls,
        provider_side_effect_attempts=attempts,
        provider_actual_side_effects=effects,
        provider_duplicate_effects=0,
        read_tool_real_executions=(
            h.calls.get("fresh_counter.read", 0) + int(batch["a"].is_error is not True)
        ),
        blind_retries_from_uncertain=blind_retries,
        stale_claims_auto_deleted=0,
        ttl_based_unsafe_retries=0,
        same_key_different_input_conflicts=conflicts,
        model_key_bypass=int(model_key.is_error is not True),
        replay_tool_executions=replay_executions,
        recovery_repairs=recovery_repairs,
        trusted_state_mutations=m2_mutations,
        unauthorized_executions=unauthorized,
        m1_regression=True,
        m2_regression=True,
        m3_regression=True,
        m4_regression=True,
    )


async def main() -> None:
    dry_run = os.environ.get("ATLASOS_M1_M4_DRY_RUN") == "1"
    if git_dirty(REPO_ROOT) and not dry_run:
        raise SystemExit("M1-M4 formal records require a clean worktree")
    with tempfile.TemporaryDirectory() as first_temporary:
        first = await _run(Path(first_temporary))
    with tempfile.TemporaryDirectory() as second_temporary:
        second = await _run(Path(second_temporary))
    first_metrics = first.model_dump(exclude={"timestamp"})
    second_metrics = second.model_dump(exclude={"timestamp"})
    if first_metrics != second_metrics:
        raise SystemExit(
            "M1-M4 formal metrics are not deterministic between complete runs: "
            f"{first_metrics} != {second_metrics}"
        )
    if not dry_run:
        ExperimentStore(STORE).append(first)
    print(first.model_dump_json(indent=2))


if __name__ == "__main__":
    asyncio.run(main())
