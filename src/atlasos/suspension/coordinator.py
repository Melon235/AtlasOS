"""Atlas Runtime suspension coordinator: preflight barrier and durable pause.

The coordinator owns no policy, no execution, and no approval authority. It orchestrates the
existing M3 gate, the Runtime suspension store, and the M6 ApprovalStore in the required order.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

from anycode.types import LLMMessage, ToolUseContext

from atlasos.actions.binding import canonical_digest
from atlasos.actions.models import ActionEvaluation
from atlasos.approvals.coordinator import ApprovalBatchState, ApprovalCoordinator
from atlasos.approvals.models import derive_approval_id
from atlasos.approvals.projection import trusted_review_projection
from atlasos.suspension.batch import BatchCall, BatchPreflight
from atlasos.suspension.metrics import M6Metrics
from atlasos.suspension.models import (
    ExecutionSuspension,
    PendingInvocation,
    SuspensionStateError,
    assistant_message_digest,
    derive_suspension_id,
    trusted_identity_digest,
)
from atlasos.suspension.store import SQLiteSuspensionStore
from atlasos.tools.catalog import CORE_TOOL_DISPATCH

_TERMINAL_RUN_STATES = frozenset({"CANCELLED", "COMPLETED", "FAILED"})


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


def _turn_from_scope(execution_scope: str | None) -> str:
    if not execution_scope:
        return "unknown"
    return execution_scope.rsplit(":", 1)[-1]


class RuntimeSuspensionCoordinator:
    def __init__(
        self,
        runtime: Any,
        suspension_store: SQLiteSuspensionStore,
        approval_coordinator: ApprovalCoordinator,
        *,
        run_id: str,
        runtime_instance_id: str | None = None,
        clock: Callable[[], datetime] | None = None,
        fault_hook: Callable[[str], None] | None = None,
    ) -> None:
        self.runtime = runtime
        self.suspension_store = suspension_store
        self.approval_coordinator = approval_coordinator
        self.run_id = run_id
        self.runtime_instance_id = runtime_instance_id or f"runtime-{run_id}"
        self._clock = clock or (lambda: datetime.now(UTC))
        self._fault_hook = fault_hook
        self.metrics: M6Metrics = approval_coordinator.metrics

    def now(self) -> datetime:
        return self._clock()

    def _fault(self, point: str) -> None:
        if self._fault_hook is not None:
            self._fault_hook(point)

    # -- barrier ------------------------------------------------------------

    async def preflight_batch(
        self,
        calls: Sequence[BatchCall],
        context: ToolUseContext,
    ) -> BatchPreflight:
        ordered = sorted(calls, key=lambda call: call.ordinal)
        call_ids = [call.call_id for call in ordered]
        if len(set(call_ids)) != len(call_ids):
            raise SuspensionStateError("duplicate tool_use ids in one model response")
        invocations = tuple([await self._preflight_one(call, context) for call in ordered])
        requires_human = any(
            invocation.preflight_outcome == "REQUIRE_HUMAN" for invocation in invocations
        )
        self.metrics.tool_batches_preflighted += 1
        self.metrics.tool_calls_preflighted += len(invocations)
        return BatchPreflight(
            requires_human=requires_human,
            invocations=invocations,
            all_denied=all(item.preflight_outcome == "DENY" for item in invocations),
        )

    async def _preflight_one(
        self,
        call: BatchCall,
        context: ToolUseContext,
    ) -> PendingInvocation:
        gate = self.runtime.action_safety_gate
        if gate is None:
            raise SuspensionStateError("M6 requires the M3 action safety gate")
        logical = self.runtime.logical_tool_name(call.tool_name) or call.tool_name
        outer = await gate.evaluate(
            logical,
            call.raw_input,
            context,
            call_id=call.call_id,
            record=True,
        )
        if logical == CORE_TOOL_DISPATCH:
            return await self._preflight_dispatch(call, logical, outer, context)
        return self._pending_from_evaluation(
            call,
            evaluation=outer,
            logical_tool_id=logical,
            effective_tool_id=logical,
            binding_digest=self.runtime.tool_binding_digest(logical),
        )

    async def _preflight_dispatch(
        self,
        call: BatchCall,
        logical: str,
        outer: ActionEvaluation,
        context: ToolUseContext,
    ) -> PendingInvocation:
        gate = self.runtime.action_safety_gate
        assert gate is not None
        if outer.failure_message is not None or outer.outcome == "DENY":
            return self._pending_from_evaluation(
                call,
                evaluation=outer,
                logical_tool_id=logical,
                effective_tool_id=logical,
                binding_digest=self.runtime.tool_binding_digest(logical),
                dispatch_target=None,
            )
        target = outer.canonical_arguments.get("tool_id")
        target_arguments = outer.canonical_arguments.get("arguments")
        if not isinstance(target, str) or not target:
            return self._denied_invocation(
                call,
                logical,
                "dispatch_target_missing",
                "invalid_dispatch: tool_id is required",
                outer,
            )
        if not isinstance(target_arguments, dict):
            return self._denied_invocation(
                call,
                logical,
                "dispatch_arguments_invalid",
                "invalid_dispatch: arguments must be an object",
                outer,
            )
        if target == CORE_TOOL_DISPATCH:
            return self._denied_invocation(
                call,
                logical,
                "dispatch_recursion",
                "tool_unavailable: dispatch_action cannot dispatch itself",
                outer,
            )
        if not self.runtime.state.is_dispatchable(target):
            return self._denied_invocation(
                call,
                logical,
                "tool_not_disclosed",
                f"tool_not_disclosed: {target!r} has not been disclosed by tool.search",
                outer,
                effective_tool_id=target,
            )
        if not self.runtime.registry.has(target):
            return self._denied_invocation(
                call,
                logical,
                "tool_unavailable",
                f"tool_unavailable: {target!r} is disclosed for this run but unavailable",
                outer,
                effective_tool_id=target,
            )
        target_evaluation = await gate.evaluate(
            target,
            target_arguments,
            context,
            call_id=call.call_id,
            record=True,
        )
        outer_binding = self.runtime.tool_binding_digest(logical)
        target_binding = self.runtime.tool_binding_digest(target)
        binding = canonical_digest({"outer": outer_binding, "target": target_binding})
        return self._pending_from_evaluation(
            call,
            evaluation=target_evaluation,
            logical_tool_id=logical,
            effective_tool_id=target,
            binding_digest=binding,
            dispatch_target=target,
            dispatch_input=outer.canonical_arguments,
        )

    def _pending_from_evaluation(
        self,
        call: BatchCall,
        *,
        evaluation: ActionEvaluation,
        logical_tool_id: str,
        effective_tool_id: str,
        binding_digest: str,
        dispatch_target: str | None = None,
        dispatch_input: dict[str, Any] | None = None,
    ) -> PendingInvocation:
        request = evaluation.request
        action = request.action if request is not None else "unknown"
        resource = request.resource if request is not None else f"tool:{effective_tool_id}"
        failure = evaluation.failure_message
        action_hash = evaluation.action_digest or canonical_digest(
            {"failure": failure or "evaluation", "tool": effective_tool_id}
        )
        basis_hash = evaluation.decision_basis_digest or canonical_digest(
            {"failure": failure or "evaluation", "tool": effective_tool_id}
        )
        projection: dict[str, Any] = {}
        if evaluation.outcome == "REQUIRE_HUMAN" and request is not None:
            projection = trusted_review_projection(
                tool_name=effective_tool_id,
                action=action,
                resource=resource,
                canonical_arguments=evaluation.canonical_arguments,
                action_context=request.context,
                action_digest=action_hash,
                decision_basis_digest=basis_hash,
            )
        return PendingInvocation(
            ordinal=call.ordinal,
            call_id=call.call_id,
            tool_name=call.tool_name,
            logical_tool_id=logical_tool_id,
            effective_tool_id=effective_tool_id,
            action=action,
            resource=resource,
            review_projection=projection,
            dispatch_target=dispatch_target,
            dispatch_input=dispatch_input,
            canonical_input=evaluation.canonical_arguments,
            input_digest=evaluation.input_digest
            or canonical_digest(evaluation.canonical_arguments),
            tool_binding_digest=binding_digest,
            action_digest=action_hash,
            decision_basis_digest=basis_hash,
            preflight_outcome=evaluation.outcome,
            execution_key=call.execution_key,
            reason_codes=evaluation.reason_codes,
            failure_message=failure,
        )

    def _denied_invocation(
        self,
        call: BatchCall,
        logical: str,
        reason: str,
        failure: str,
        outer: ActionEvaluation,
        *,
        effective_tool_id: str | None = None,
    ) -> PendingInvocation:
        evaluation = outer.model_copy(
            update={
                "outcome": "DENY",
                "failure_message": failure,
                "reason_codes": (reason,),
            }
        )
        return self._pending_from_evaluation(
            call,
            evaluation=evaluation,
            logical_tool_id=logical,
            effective_tool_id=effective_tool_id or logical,
            binding_digest=self.runtime.tool_binding_digest(effective_tool_id or logical)
            if self.runtime.registry.has(effective_tool_id or logical)
            else self.runtime.tool_binding_digest(logical),
        )

    # -- durable pause ------------------------------------------------------

    async def suspend(
        self,
        plan: BatchPreflight,
        context: ToolUseContext,
        *,
        assistant_message: LLMMessage,
        execution_scope: str | None,
        sandbox_binding_digest: str | None = None,
        sandbox_required: bool = False,
    ) -> ExecutionSuspension:
        if assistant_message.role != "assistant":
            raise SuspensionStateError("held assistant message must have role='assistant'")
        execution_context = context.execution_context
        if execution_context is None:
            raise SuspensionStateError("M6 suspension requires a trusted ExecutionContext")
        run_id = _execution_attribute(context, "run_id") or self.run_id
        task_id = _execution_attribute(context, "task_id")
        worker_id = _execution_attribute(context, "worker_id")
        profile_id = _execution_attribute(context, "profile_id")
        agent_name = context.agent.name
        response_identity = (
            f"run:{run_id}:task:{task_id or '<main>'}:turn:{_turn_from_scope(execution_scope)}"
        )
        assistant_digest = assistant_message_digest(assistant_message.content)
        suspension_id = derive_suspension_id(
            run_id=run_id,
            response_identity=response_identity,
            assistant_response_digest=assistant_digest,
        )
        pending = tuple(
            self._attach_approval_id(invocation, suspension_id) for invocation in plan.invocations
        )
        wake_reference = next(
            (item.approval_id for item in pending if item.approval_id is not None),
            f"suspension:{suspension_id}",
        )
        now = self.now()
        proposed = ExecutionSuspension(
            suspension_id=suspension_id,
            run_id=run_id,
            task_id=task_id,
            worker_id=worker_id,
            profile_id=profile_id,
            agent_name=agent_name,
            assistant_response_digest=assistant_digest,
            held_assistant_message=assistant_message.model_dump(mode="json"),
            pending_invocations=pending,
            trusted_identity_digest=trusted_identity_digest(execution_context),
            response_identity=response_identity,
            execution_scope=execution_scope,
            sandbox_binding_digest=sandbox_binding_digest,
            sandbox_required=sandbox_required,
            wake_reference=wake_reference,
            created_at=now,
            updated_at=now,
        )
        existing = self.suspension_store.get(suspension_id)
        self._fault("suspension_before_persist")
        stored = self.suspension_store.save_if_absent(proposed)
        if existing is None:
            self.metrics.runtime_suspensions_created += 1
        else:
            self.metrics.runtime_suspension_reloads += 1
        self._fault("suspension_after_persist")
        self.approval_coordinator.ensure_for_suspension(stored)
        self._fault("suspension_after_approvals")
        if stored.state in ("ACTIVE", "SETTLING"):
            self.suspension_store.mark_run(
                stored.run_id, "PAUSED", suspension_id=stored.suspension_id
            )
            if stored.task_id is not None:
                self.suspension_store.mark_task(
                    stored.run_id,
                    stored.task_id,
                    "PAUSED",
                    suspension_id=stored.suspension_id,
                )
        self._fault("suspension_after_pause")
        return stored

    @staticmethod
    def _attach_approval_id(invocation: PendingInvocation, suspension_id: str) -> PendingInvocation:
        if invocation.preflight_outcome != "REQUIRE_HUMAN":
            return invocation
        approval_id = derive_approval_id(
            suspension_id=suspension_id,
            call_id=invocation.call_id,
            action_digest=invocation.action_digest,
            decision_basis_digest=invocation.decision_basis_digest,
        )
        return invocation.model_copy(update={"approval_id": approval_id})

    def reconcile(self, suspension: ExecutionSuspension) -> ApprovalBatchState:
        self.approval_coordinator.ensure_for_suspension(suspension)
        return self.approval_coordinator.batch_state(suspension)

    def mark_terminal_run(self, run_id: str, state: str, reason: str | None = None) -> None:
        if state not in _TERMINAL_RUN_STATES:
            raise SuspensionStateError(f"not a terminal run state: {state!r}")
        self.suspension_store.mark_run(run_id, state, reason=reason)
        for suspension in self.suspension_store.list_for_run(
            run_id, states=("ACTIVE", "SETTLING", "SETTLED")
        ):
            self.suspension_store.mark_stale(
                suspension.suspension_id,
                f"run entered terminal state {state}: {reason or ''}".strip(),
            )

    def list_resumable(self) -> tuple[ExecutionSuspension, ...]:
        resumable: list[ExecutionSuspension] = []
        for suspension in self.suspension_store.list_active():
            run_state = self.suspension_store.run_state(suspension.run_id)
            if run_state in _TERMINAL_RUN_STATES:
                continue
            state = self.approval_coordinator.batch_state(suspension)
            if state.ready:
                resumable.append(suspension)
        return tuple(resumable)

    def metrics_snapshot(self) -> dict[str, int]:
        return self.metrics.snapshot()
