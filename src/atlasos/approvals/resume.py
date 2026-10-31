"""Trusted resume path for a durable Runtime ExecutionSuspension.

Resume never asks the model to reconstruct the pending call. It loads the exact suspended
invocations, revalidates Runtime/M1/M2/M3/M5 facts, settles the complete batch through M4, makes
the ToolResult continuation durable, and only then clears the suspension.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from anycode.types import LLMMessage, ToolResult, ToolResultBlock, ToolUseContext

from atlasos.actions.binding import canonical_digest
from atlasos.actions.models import ActionEvaluation
from atlasos.approvals.models import ApprovalReceipt, ReviewerIdentity
from atlasos.ledger.identity import ActionIdentityProvider
from atlasos.suspension.models import (
    ExecutionSuspension,
    PendingInvocation,
    SettlementOutcome,
    StaleSuspension,
    SuspensionSettlement,
    SuspensionStateError,
    SuspensionStoreError,
    SuspensionToolResult,
    assistant_message_digest,
    derive_suspension_id,
    trusted_identity_digest,
)
from atlasos.suspension.models import (
    canonical_digest as suspension_digest,
)
from atlasos.tools.catalog import CORE_TOOL_DISPATCH

if TYPE_CHECKING:
    from atlasos.suspension.coordinator import RuntimeSuspensionCoordinator
    from atlasos.tools.conversation import ConversationStore
    from atlasos.tools.runtime import AtlasToolRuntime

ResumeStatus = Literal[
    "settled",
    "waiting",
    "stale",
    "busy",
    "terminal_run",
    "cleared",
    "not_found",
]
_TERMINAL_RUN_STATES = frozenset({"CANCELLED", "COMPLETED", "FAILED"})


@dataclass(frozen=True)
class ResumeOutcome:
    status: ResumeStatus
    suspension_id: str
    reason: str | None = None
    suspension: ExecutionSuspension | None = None
    settlement: SuspensionSettlement | None = None
    continuation: tuple[LLMMessage, ...] = ()


@dataclass(frozen=True)
class _Revalidated:
    invocation: PendingInvocation
    evaluation: ActionEvaluation | None
    disposition: Literal["EXECUTE", "DENY", "REJECT"]
    receipt: ApprovalReceipt | None = None
    reason: str | None = None


class _Refusal(RuntimeError):
    def __init__(self, status: ResumeStatus, reason: str) -> None:
        super().__init__(reason)
        self.status: ResumeStatus = status
        self.reason = reason


def _execution_attribute(context: ToolUseContext, key: str) -> str | None:
    execution_context = context.execution_context
    if execution_context is None:
        return None
    value = execution_context.attributes.get(key)
    if value is None:
        nested = execution_context.attributes.get("atlas_worker")
        if isinstance(nested, dict):
            value = nested.get(key)
    return str(value) if value is not None else None


def _failure(text: str) -> ToolResult:
    return ToolResult(data=text, is_error=True, retry_safe=False)


def _denied_result(invocation: PendingInvocation, text: str) -> ToolResult:
    message = text or f"{', '.join(invocation.reason_codes) or 'denied'}"
    if not message.startswith(("action_", "tool_", "invalid_dispatch")):
        message = f"action_denied: {message}"
    return ToolResult(data=message, is_error=True, retry_safe=False)


def _rejected_result(receipt: ApprovalReceipt | None) -> ToolResult:
    del receipt
    return ToolResult(
        data="Rejected by human reviewer.",
        is_error=True,
        retry_safe=False,
    )


async def resume_execution_suspension(
    suspension_id: str,
    *,
    runtime: AtlasToolRuntime,
    coordinator: RuntimeSuspensionCoordinator,
    context: ToolUseContext,
    owner: str,
    lease_seconds: float = 60.0,
    commit_continuation: bool = True,
    fault_hook: Callable[[str], None] | None = None,
) -> ResumeOutcome:
    def _fault(point: str) -> None:
        if fault_hook is not None:
            fault_hook(point)

    store = coordinator.suspension_store
    suspension = store.get(suspension_id)
    if suspension is None:
        return ResumeOutcome(status="not_found", suspension_id=suspension_id)
    run_state = store.run_state(suspension.run_id)
    if run_state in _TERMINAL_RUN_STATES:
        if (
            any(
                challenge.state in ("APPROVED", "REJECTED")
                for challenge in coordinator.approval_coordinator.store.list_for_suspension(
                    suspension_id
                )
            )
            or suspension.state == "SETTLED"
        ):
            coordinator.metrics.late_approval_attempts += 1
        return ResumeOutcome(
            status="terminal_run",
            suspension_id=suspension_id,
            reason=f"run is terminal: {run_state}",
            suspension=suspension,
        )
    if suspension.state == "CLEARED":
        return ResumeOutcome(status="cleared", suspension_id=suspension_id, suspension=suspension)
    if suspension.state == "STALE":
        return ResumeOutcome(
            status="stale",
            suspension_id=suspension_id,
            reason=suspension.last_error or "suspension is stale",
            suspension=suspension,
        )
    if suspension.state == "SETTLED" or (
        suspension.state == "SETTLING" and suspension.settlement is not None
    ):
        return await _apply_existing_settlement(
            runtime=runtime,
            coordinator=coordinator,
            suspension=suspension,
            owner=owner,
            commit_continuation=commit_continuation,
            fault_hook=fault_hook,
        )

    try:
        settling = store.begin_settlement(
            suspension_id,
            suspension.revision,
            owner=owner,
            lease_seconds=lease_seconds,
        )
    except StaleSuspension as error:
        return ResumeOutcome(
            status="stale",
            suspension_id=suspension_id,
            reason=str(error),
            suspension=store.get(suspension_id),
        )
    except SuspensionStateError as error:
        return ResumeOutcome(
            status="stale",
            suspension_id=suspension_id,
            reason=str(error),
            suspension=store.get(suspension_id),
        )
    except SuspensionStoreError as error:
        return ResumeOutcome(
            status="busy",
            suspension_id=suspension_id,
            reason=str(error),
            suspension=store.get(suspension_id),
        )

    _fault("resume_after_begin_settlement")
    _fault("resume_before_revalidate")
    try:
        revalidated = await _revalidate(
            runtime=runtime,
            coordinator=coordinator,
            suspension=settling,
            context=context,
        )
    except _Refusal as refusal:
        released = store.release_settlement(
            suspension_id,
            settling.revision,
            owner=owner,
            reason=refusal.reason,
        )
        return ResumeOutcome(
            status=refusal.status,
            suspension_id=suspension_id,
            reason=refusal.reason,
            suspension=released,
        )
    except Exception as error:
        store.release_settlement(
            suspension_id,
            settling.revision,
            owner=owner,
            reason=f"revalidation failed: {error}",
        )
        raise

    _fault("resume_after_revalidate")
    _fault("resume_before_execute")
    results = await _execute_batch(
        runtime=runtime,
        coordinator=coordinator,
        context=context,
        revalidated=revalidated,
    )
    _fault("resume_after_execute")
    settlement = _build_settlement(settling, results)
    try:
        settled = store.complete_settlement(
            suspension_id,
            settling.revision,
            settlement,
            owner=owner,
        )
    except SuspensionStoreError as error:
        return ResumeOutcome(
            status="busy",
            suspension_id=suspension_id,
            reason=f"another resume committed first: {error}",
            suspension=store.get(suspension_id),
        )
    _fault("resume_after_settlement")
    applied = False
    if commit_continuation:
        applied = _commit_continuation(
            runtime.conversation_store,
            settled,
            settlement,
        )
    _fault("resume_after_continuation")
    if applied:
        coordinator.metrics.tool_results_committed += len(settlement.results)
    cleared = store.clear(suspension_id, settled.revision)
    coordinator.metrics.suspensions_cleared += 1
    _fault("resume_after_clear")
    store.mark_run(cleared.run_id, "ACTIVE")
    if cleared.task_id is not None:
        store.mark_task(cleared.run_id, cleared.task_id, "ACTIVE")
    continuation = _continuation_messages(settled, settlement)
    return ResumeOutcome(
        status="settled",
        suspension_id=suspension_id,
        suspension=cleared,
        settlement=settlement,
        continuation=tuple(continuation),
    )


async def _apply_existing_settlement(
    *,
    runtime: AtlasToolRuntime,
    coordinator: RuntimeSuspensionCoordinator,
    suspension: ExecutionSuspension,
    owner: str,
    commit_continuation: bool,
    fault_hook: Callable[[str], None] | None = None,
) -> ResumeOutcome:
    del owner

    def _fault(point: str) -> None:
        if fault_hook is not None:
            fault_hook(point)

    settlement = suspension.settlement
    if settlement is None:
        return ResumeOutcome(
            status="stale",
            suspension_id=suspension.suspension_id,
            reason="settled suspension has no durable settlement",
            suspension=suspension,
        )
    try:
        _verify_settlement(suspension, settlement)
    except _Refusal as refusal:
        return ResumeOutcome(
            status=refusal.status,
            suspension_id=suspension.suspension_id,
            reason=refusal.reason,
            suspension=suspension,
        )
    applied = False
    if commit_continuation:
        applied = _commit_continuation(runtime.conversation_store, suspension, settlement)
    _fault("resume_after_continuation")
    if applied:
        coordinator.metrics.tool_results_committed += len(settlement.results)
    cleared = coordinator.suspension_store.clear(suspension.suspension_id, suspension.revision)
    coordinator.metrics.suspensions_cleared += 1
    coordinator.suspension_store.mark_run(cleared.run_id, "ACTIVE")
    if cleared.task_id is not None:
        coordinator.suspension_store.mark_task(cleared.run_id, cleared.task_id, "ACTIVE")
    return ResumeOutcome(
        status="settled",
        suspension_id=suspension.suspension_id,
        suspension=cleared,
        settlement=settlement,
        continuation=tuple(_continuation_messages(cleared, settlement)),
    )


async def _revalidate(
    *,
    runtime: AtlasToolRuntime,
    coordinator: RuntimeSuspensionCoordinator,
    suspension: ExecutionSuspension,
    context: ToolUseContext,
) -> tuple[_Revalidated, ...]:
    metrics = coordinator.metrics
    _verify_suspension_integrity(suspension)
    execution_context = context.execution_context
    if execution_context is None:
        metrics.m2_identity_violations += 1
        raise _Refusal("stale", "trusted ExecutionContext is required to resume")
    if trusted_identity_digest(execution_context) != suspension.trusted_identity_digest:
        metrics.m2_identity_violations += 1
        raise _Refusal("stale", "M2 trusted worker identity changed")
    for key, stored in (
        ("run_id", suspension.run_id),
        ("task_id", suspension.task_id),
        ("worker_id", suspension.worker_id),
        ("profile_id", suspension.profile_id),
    ):
        current = _execution_attribute(context, key)
        if stored is not None and current != stored:
            metrics.m2_identity_violations += 1
            raise _Refusal("stale", f"M2 trusted {key} changed")

    if suspension.sandbox_required:
        try:
            current_sandbox = runtime.sandbox_binding_digest(suspension.task_id)
        except Exception as error:
            metrics.m5_resume_violations += 1
            raise _Refusal("stale", f"M5 sandbox continuity unavailable: {error}") from error
        if current_sandbox is None or current_sandbox != suspension.sandbox_binding_digest:
            metrics.m5_resume_violations += 1
            raise _Refusal("stale", "M5 sandbox binding is lost or changed")

    gate = runtime.action_safety_gate
    if gate is None:
        raise _Refusal("stale", "M6 requires the M3 action safety gate")

    revalidated: list[_Revalidated] = []
    for invocation in suspension.pending_invocations:
        revalidated.append(
            await _revalidate_invocation(
                runtime=runtime,
                coordinator=coordinator,
                suspension=suspension,
                invocation=invocation,
                context=context,
            )
        )

    waiting = [item for item in revalidated if item.reason == "approval_pending"]
    if waiting:
        raise _Refusal("waiting", "one or more required human decisions are still pending")
    if any(item.disposition == "EXECUTE" and item.evaluation is None for item in revalidated):
        raise _Refusal("stale", "execute disposition without an M3 evaluation")
    return tuple(revalidated)


async def _revalidate_invocation(
    *,
    runtime: AtlasToolRuntime,
    coordinator: RuntimeSuspensionCoordinator,
    suspension: ExecutionSuspension,
    invocation: PendingInvocation,
    context: ToolUseContext,
) -> _Revalidated:
    metrics = coordinator.metrics
    gate = runtime.action_safety_gate
    assert gate is not None
    target = invocation.effective_tool_id
    evaluation: ActionEvaluation | None = None
    if invocation.preflight_outcome == "DENY":
        # A denial recorded before suspension is already terminal. It never entered M4 and is
        # not a pending action; settlement re-emits the trusted denial instead of resurrecting
        # it through a later approval or policy relaxation.
        return _Revalidated(
            invocation=invocation,
            evaluation=None,
            disposition="DENY",
            reason=invocation.failure_message or "action_denied at suspension preflight",
        )
    if invocation.logical_tool_id == CORE_TOOL_DISPATCH:
        if invocation.dispatch_target is None or invocation.dispatch_input is None:
            raise _Refusal("stale", "dispatch suspension lacks exact target facts")
        if not runtime.state.is_dispatchable(invocation.dispatch_target):
            metrics.m1_resume_violations += 1
            raise _Refusal("stale", "M1 target tool is no longer disclosed")
        if not runtime.registry.has(invocation.dispatch_target):
            metrics.m1_resume_violations += 1
            raise _Refusal("stale", "M1 target tool is no longer available")
        outer = await gate.evaluate(
            CORE_TOOL_DISPATCH,
            invocation.dispatch_input,
            context,
            call_id=invocation.call_id,
            record=True,
        )
        if outer.failure_message is not None or outer.outcome != "ALLOW":
            return _Revalidated(
                invocation=invocation,
                evaluation=outer,
                disposition="DENY",
                reason=outer.failure_message or "dispatch control revalidation denied",
            )
    else:
        if not runtime.registry.has(target):
            metrics.m1_resume_violations += 1
            raise _Refusal("stale", "M1 tool is no longer available")
    evaluation = await gate.evaluate(
        target,
        invocation.canonical_input,
        context,
        call_id=invocation.call_id,
        record=True,
    )
    metrics.m3_revalidations += 1
    if evaluation.failure_message is not None:
        return _Revalidated(
            invocation=invocation,
            evaluation=evaluation,
            disposition="DENY",
            reason=evaluation.failure_message,
        )
    if (
        evaluation.canonical_arguments != invocation.canonical_input
        or evaluation.input_digest != invocation.input_digest
    ):
        metrics.stale_action_digests += 1
        raise _Refusal("stale", "canonical input changed since suspension")
    current_binding = runtime.tool_binding_digest(target)
    expected_binding = (
        canonical_digest(
            {
                "outer": runtime.tool_binding_digest(CORE_TOOL_DISPATCH),
                "target": current_binding,
            }
        )
        if invocation.logical_tool_id == CORE_TOOL_DISPATCH
        else current_binding
    )
    if expected_binding != invocation.tool_binding_digest:
        metrics.stale_tool_bindings += 1
        raise _Refusal("stale", "tool binding/source/schema changed since suspension")
    if evaluation.action_digest != invocation.action_digest:
        metrics.stale_action_digests += 1
        raise _Refusal("stale", "action digest changed since suspension")
    if evaluation.outcome == "DENY":
        if invocation.approval_id is not None:
            metrics.m3_new_denies_after_approval += 1
        return _Revalidated(
            invocation=invocation,
            evaluation=evaluation,
            disposition="DENY",
            reason="current M3 DENY overrides any human approval",
        )
    if evaluation.outcome == "ALLOW":
        if evaluation.decision_basis_digest != invocation.decision_basis_digest:
            metrics.stale_decision_basis += 1
        return _Revalidated(
            invocation=invocation,
            evaluation=evaluation,
            disposition="EXECUTE",
        )

    # REQUIRE_HUMAN
    if invocation.approval_id is None:
        metrics.stale_decision_basis += 1
        raise _Refusal("stale", "current M3 requires a human but no durable challenge exists")
    challenge = coordinator.approval_coordinator.store.get(invocation.approval_id)
    if challenge is None:
        metrics.stale_decision_basis += 1
        raise _Refusal("stale", "durable approval challenge is missing")
    basis_changed = evaluation.decision_basis_digest != invocation.decision_basis_digest
    receipt = coordinator.approval_coordinator.receipt_for(
        suspension,
        invocation,
        current_action_digest=evaluation.action_digest,
        current_decision_basis_digest=evaluation.decision_basis_digest,
    )
    if receipt is None:
        if challenge.state == "PENDING" and not basis_changed:
            return _Revalidated(
                invocation=invocation,
                evaluation=evaluation,
                disposition="DENY",
                reason="approval_pending",
            )
        metrics.stale_decision_basis += 1
        if challenge.state == "PENDING":
            coordinator.approval_coordinator.supersede(
                challenge.approval_id,
                expected_version=challenge.version,
                reviewer=ReviewerIdentity(actor="system:m6-basis-change", source="host"),
                reason="security basis changed while suspended",
            )
        raise _Refusal(
            "stale",
            "approval receipt no longer matches the current action/security basis",
        )
    if receipt.decision == "REJECTED":
        return _Revalidated(
            invocation=invocation,
            evaluation=evaluation,
            disposition="REJECT",
            receipt=receipt,
            reason=receipt.reason,
        )
    return _Revalidated(
        invocation=invocation,
        evaluation=evaluation,
        disposition="EXECUTE",
        receipt=receipt,
    )


async def _execute_batch(
    *,
    runtime: AtlasToolRuntime,
    coordinator: RuntimeSuspensionCoordinator,
    context: ToolUseContext,
    revalidated: tuple[_Revalidated, ...],
) -> tuple[tuple[ToolResult, SettlementOutcome], ...]:
    gate = runtime.action_safety_gate
    assert gate is not None
    results: list[tuple[ToolResult, SettlementOutcome]] = []
    for item in revalidated:
        invocation = item.invocation
        if item.disposition == "DENY":
            if item.reason == "approval_pending":
                raise _Refusal("waiting", "approval became pending during settlement")
            results.append(
                (
                    _denied_result(invocation, item.reason or ""),
                    "DENIED",
                )
            )
            continue
        if item.disposition == "REJECT":
            results.append((_rejected_result(item.receipt), "REJECTED"))
            continue
        assert item.evaluation is not None
        if invocation.preflight_outcome == "REQUIRE_HUMAN":
            coordinator.metrics.m4_attempts_after_approval += 1
        result = await gate.execute_revalidated(
            item.evaluation,
            context,
            idempotency_key=invocation.execution_key,
            human_outcome=(
                "approved_receipt"
                if item.receipt is not None and item.receipt.decision == "APPROVED"
                else None
            ),
        )
        outcome = _classify_m4(runtime, coordinator, invocation, result)
        results.append((result, outcome))
    return tuple(results)


def _classify_m4(
    runtime: AtlasToolRuntime,
    coordinator: RuntimeSuspensionCoordinator,
    invocation: PendingInvocation,
    result: ToolResult,
) -> SettlementOutcome:
    metrics = coordinator.metrics
    if not result.is_error:
        if _ledger_kind(runtime, invocation) == "REPLAYED":
            metrics.m4_replays += 1
            return "REPLAYED"
        return "EXECUTED"
    text = str(result.data)
    if _ledger_kind(runtime, invocation) == "CONFLICT":
        metrics.m4_conflicts += 1
        return "FAILED"
    if "already in progress" in text or "idempotency outcome could not be recorded" in text:
        metrics.m4_uncertain += 1
        return "UNCERTAIN"
    if "conflict" in text.lower():
        metrics.m4_conflicts += 1
        return "FAILED"
    if not result.retry_safe:
        metrics.m4_uncertain += 1
        return "UNCERTAIN"
    if invocation.logical_tool_id == CORE_TOOL_DISPATCH:
        return "FAILED"
    return "FAILED"


def _ledger_kind(runtime: AtlasToolRuntime, invocation: PendingInvocation) -> str | None:
    ledger = runtime.action_ledger
    key = invocation.execution_key
    if ledger is None or key is None:
        return None
    record = ledger.latest_for_key_hash(ActionIdentityProvider.key_hash(key))
    return record.completion_kind if record is not None else None


def _verify_suspension_integrity(suspension: ExecutionSuspension) -> None:
    try:
        held = suspension.held_message()
    except Exception as error:
        raise _Refusal("stale", f"suspension held message is invalid: {error}") from error
    if assistant_message_digest(held.content) != suspension.assistant_response_digest:
        raise _Refusal("stale", "held assistant response digest mismatch")
    expected_id = derive_suspension_id(
        run_id=suspension.run_id,
        response_identity=suspension.response_identity,
        assistant_response_digest=suspension.assistant_response_digest,
    )
    if expected_id != suspension.suspension_id:
        raise _Refusal("stale", "suspension identity is not deterministic")
    if [item.ordinal for item in suspension.pending_invocations] != list(
        range(len(suspension.pending_invocations))
    ):
        raise _Refusal("stale", "pending invocation ordinals are not contiguous")
    call_ids = [item.call_id for item in suspension.pending_invocations]
    if len(set(call_ids)) != len(call_ids):
        raise _Refusal("stale", "duplicate call ids in suspension")
    for invocation in suspension.pending_invocations:
        if invocation.input_digest != suspension_digest(invocation.canonical_input):
            raise _Refusal("stale", "suspended canonical input digest mismatch")
        if not invocation.action_digest or not invocation.decision_basis_digest:
            raise _Refusal("stale", "suspended invocation lacks M3 digests")


def _build_settlement(
    suspension: ExecutionSuspension,
    results: tuple[tuple[ToolResult, SettlementOutcome], ...],
) -> SuspensionSettlement:
    settled_results = tuple(
        SuspensionToolResult(
            ordinal=invocation.ordinal,
            call_id=invocation.call_id,
            tool_use_id=invocation.call_id,
            tool_name=invocation.tool_name,
            logical_tool_id=invocation.effective_tool_id,
            content=str(result.data),
            is_error=bool(result.is_error),
            outcome=outcome,
            action_digest=invocation.action_digest,
            decision_basis_digest=invocation.decision_basis_digest,
            approval_id=invocation.approval_id,
        )
        for invocation, (result, outcome) in zip(
            suspension.pending_invocations, results, strict=True
        )
    )
    result_batch_digest = canonical_digest(
        [result.model_dump(mode="json") for result in settled_results]
    )
    held = suspension.held_message()
    continuation_digest = canonical_digest(
        {
            "assistant": held.model_dump(mode="json"),
            "tool_results": [
                {
                    "tool_use_id": result.call_id,
                    "content": result.content,
                    "is_error": result.is_error,
                }
                for result in settled_results
            ],
        }
    )
    return SuspensionSettlement(
        suspension_id=suspension.suspension_id,
        assistant_response_digest=suspension.assistant_response_digest,
        results=settled_results,
        result_batch_digest=result_batch_digest,
        continuation_digest=continuation_digest,
        created_at=datetime.now(UTC),
    )


def _verify_settlement(suspension: ExecutionSuspension, settlement: SuspensionSettlement) -> None:
    if settlement.suspension_id != suspension.suspension_id:
        raise _Refusal("stale", "settlement belongs to another suspension")
    if settlement.assistant_response_digest != suspension.assistant_response_digest:
        raise _Refusal("stale", "settlement assistant digest mismatch")
    if (
        canonical_digest([result.model_dump(mode="json") for result in settlement.results])
        != settlement.result_batch_digest
    ):
        raise _Refusal("stale", "settlement result batch digest mismatch")


def _continuation_messages(
    suspension: ExecutionSuspension,
    settlement: SuspensionSettlement,
) -> list[LLMMessage]:
    held = suspension.held_message()
    tool_message = LLMMessage(
        role="user",
        content=[
            ToolResultBlock(
                tool_use_id=result.call_id,
                content=result.content,
                is_error=result.is_error,
            )
            for result in settlement.results
        ],
    )
    return [held, tool_message]


def _commit_continuation(
    conversation_store: ConversationStore,
    suspension: ExecutionSuspension,
    settlement: SuspensionSettlement,
) -> bool:
    key = suspension.task_id or f"run:{suspension.run_id}"
    base = conversation_store.load(key) or []
    held = suspension.held_message().model_dump(mode="json")
    tool_message = LLMMessage(
        role="user",
        content=[
            ToolResultBlock(
                tool_use_id=result.call_id,
                content=result.content,
                is_error=result.is_error,
            )
            for result in settlement.results
        ],
    ).model_dump(mode="json")
    if len(base) >= 2 and base[-2] == held and base[-1] == tool_message:
        return False
    conversation_store.save(
        key,
        [
            *[LLMMessage.model_validate(message) for message in base],
            suspension.held_message(),
            LLMMessage.model_validate(tool_message),
        ],
    )
    return True
