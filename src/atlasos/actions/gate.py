"""One global pre-invoke action safety gate.

The gate wraps only AnyCode's ToolExecutor; it never executes a tool itself. Validation, AnyCode
hard security, policy, reviewer, external policy, and digest-bound HITL all run before the inner
executor can claim idempotency or invoke the tool.

M6 adds a reusable, side-effect-free ``evaluate`` path. It performs exactly the authorization
work and returns an ``ActionEvaluation`` without claiming idempotency, creating ledger records,
executing tools, touching a sandbox, or requesting any approval. The normal ``execute`` path uses
the same evaluator and then performs the approved action.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from anycode.hitl.approval import ApprovalManager
from anycode.identity.policy import PolicyEnforcer, PolicyRequest
from anycode.security.policy import (
    ToolSecurityError,
    check_tool_access,
    resolve_tool_path,
    validate_shell_command,
)
from anycode.security.redaction import safe_exception_message
from anycode.tools.executor import ToolExecutor
from anycode.tools.registry import ToolRegistry
from anycode.types import BatchToolCall, ToolResult, ToolUseContext

from atlasos.actions.binding import (
    action_digest as compute_action_digest,
)
from atlasos.actions.binding import (
    canonical_digest,
    infer_tool_source,
)
from atlasos.actions.binding import (
    decision_basis_digest as compute_decision_basis_digest,
)
from atlasos.actions.binding import (
    input_digest as compute_input_digest,
)
from atlasos.actions.binding import (
    tool_binding_digest as compute_tool_binding_digest,
)
from atlasos.actions.catalog import ActionCatalog, ActionResolution
from atlasos.actions.models import (
    ActionAudit,
    ActionContext,
    ActionDecision,
    ActionEvaluation,
    ActionRequest,
    ActionReviewRequest,
    ActionReviewResult,
    ScopeAlignment,
)
from atlasos.actions.policy import ActionPolicy
from atlasos.actions.reviewer import ActionReviewer
from atlasos.trajectory.context import current_call_trajectory
from atlasos.trajectory.models import TrajectoryCorrelation

if TYPE_CHECKING:
    from atlasos.trajectory.recorder import TrajectoryRecorder

logger = logging.getLogger(__name__)


class ActionSafetyGate:
    """Global gate for every real tool call in a configured Atlas runtime."""

    def __init__(
        self,
        executor: ToolExecutor,
        registry: ToolRegistry,
        *,
        policy: ActionPolicy,
        catalog: ActionCatalog | None = None,
        reviewer: ActionReviewer | None = None,
        approval_manager: ApprovalManager | None = None,
        external_policy: PolicyEnforcer | None = None,
        max_modified_attempts: int = 2,
        tool_sources: Mapping[str, str] | None = None,
        trajectory_recorder: TrajectoryRecorder | None = None,
    ) -> None:
        self._executor = executor
        self._registry = registry
        self.policy = policy
        self.catalog = catalog or ActionCatalog()
        self.reviewer = reviewer
        self.approval_manager = approval_manager
        self.external_policy = external_policy
        self.max_modified_attempts = max_modified_attempts
        self.tool_sources = dict(tool_sources or {})
        self.policy_digest = canonical_digest(policy.model_dump(mode="json"))
        self.trajectory_recorder = trajectory_recorder
        self.audits: list[ActionAudit] = []
        self.preflight_evaluations: list[ActionEvaluation] = []

    # -- side-effect-free authorization -------------------------------------

    async def evaluate(
        self,
        tool_name: str,
        raw_input: dict[str, Any],
        context: ToolUseContext,
        *,
        call_id: str | None = None,
        record: bool = False,
        trajectory_phase: str = "execution",
    ) -> ActionEvaluation:
        """Authorize one call without any execution-side effect.

        This method may run the M3 scope reviewer and the external policy enforcer (both are
        authorization authorities). It never reaches M4, AnyCode ``claim``, a ToolDefinition
        execution, a sandbox provider, or an approval manager.
        """

        actual_call_id = call_id or uuid.uuid4().hex
        tool = self._registry.get(tool_name)
        if tool is None:
            return self._record_trajectory(
                self._deny(
                    tool_name,
                    actual_call_id,
                    "not_registered",
                    f'Tool "{tool_name}" is not registered in the current registry.',
                ),
                trajectory_phase,
            )

        try:
            check_tool_access(tool_name, context.security_policy)
        except ToolSecurityError as error:
            return self._record_trajectory(
                self._deny(
                    tool_name,
                    actual_call_id,
                    "hard_denied",
                    f"action_hard_denied: {safe_exception_message(error)}",
                ),
                trajectory_phase,
            )

        try:
            validated = tool.input_model.model_validate(raw_input)
        except Exception as error:
            return self._record_trajectory(
                self._deny(
                    tool_name,
                    actual_call_id,
                    "invalid_input",
                    f'Invalid input for tool "{tool_name}": {safe_exception_message(error)}',
                ),
                trajectory_phase,
            )
        canonical_arguments = validated.model_dump(mode="json")

        try:
            resolution = self.catalog.resolve(tool_name, canonical_arguments)
            self._hard_precheck(tool_name, canonical_arguments, context, resolution)
        except ToolSecurityError as error:
            return self._record_trajectory(
                self._deny(
                    tool_name,
                    actual_call_id,
                    "hard_denied",
                    f"action_hard_denied: {safe_exception_message(error)}",
                ),
                trajectory_phase,
            )

        if context.execution_context is None:
            return self._record_trajectory(
                self._deny(
                    tool_name,
                    actual_call_id,
                    "trusted_context_required",
                    "action_denied: trusted ExecutionContext is required",
                ),
                trajectory_phase,
            )

        evaluation = await self._evaluate(
            tool_name,
            canonical_arguments,
            context,
            actual_call_id,
        )
        if record:
            self.preflight_evaluations.append(evaluation)
        return self._record_trajectory(evaluation, trajectory_phase)

    async def execute(
        self,
        tool_name: str,
        raw_input: dict[str, Any],
        context: ToolUseContext,
        *,
        idempotency_key: str | None = None,
    ) -> ToolResult:
        current_arguments = dict(raw_input)
        for attempt in range(self.max_modified_attempts + 1):
            evaluation = await self.evaluate(
                tool_name,
                current_arguments,
                context,
                call_id=uuid.uuid4().hex,
            )
            if evaluation.failure_message is not None:
                return _failure(evaluation.failure_message)

            if evaluation.outcome == "DENY":
                self._audit(
                    evaluation,
                    human_outcome=None,
                    executed=False,
                )
                return _failure(
                    "action_denied: " + ", ".join(evaluation.reason_codes or ("denied",))
                )

            if evaluation.outcome == "REQUIRE_HUMAN":
                response = await self._request_approval(evaluation, context)
                if response is None:
                    self._audit(
                        evaluation,
                        human_outcome="unavailable_or_rejected",
                        executed=False,
                    )
                    return _failure("action_denied: human approval unavailable or rejected")
                if not response.approved:
                    self._audit(
                        evaluation,
                        human_outcome="rejected",
                        executed=False,
                    )
                    return _failure("action_denied: human approval rejected")
                if response.modified_input:
                    tool = self._registry.get(tool_name)
                    if tool is None:
                        return _failure(f'Tool "{tool_name}" is not registered.')
                    try:
                        modified = tool.input_model.model_validate(response.modified_input)
                    except Exception as error:
                        self._audit(
                            evaluation,
                            human_outcome="modified_invalid",
                            executed=False,
                        )
                        return _failure(
                            "action_denied: modified input is invalid: "
                            + safe_exception_message(error)
                        )
                    current_arguments = modified.model_dump(mode="json")
                    self._audit(
                        evaluation,
                        human_outcome="modified_reauthorize",
                        executed=False,
                    )
                    if attempt >= self.max_modified_attempts:
                        return _failure("action_denied: modified input reauthorization limit")
                    continue
                self._audit(
                    evaluation,
                    human_outcome="approved",
                    executed=True,
                )
                return await self._execute(
                    tool_name,
                    evaluation.canonical_arguments,
                    context,
                    idempotency_key=idempotency_key,
                )

            # ALLOW, with optional scope reviewer already applied in evaluate.
            self._audit(evaluation, human_outcome=None, executed=True)
            return await self._execute(
                tool_name,
                evaluation.canonical_arguments,
                context,
                idempotency_key=idempotency_key,
            )

        return _failure("action_denied: reauthorization exhausted")

    async def execute_revalidated(
        self,
        evaluation: ActionEvaluation,
        context: ToolUseContext,
        *,
        idempotency_key: str | None = None,
        human_outcome: str | None = None,
    ) -> ToolResult:
        """Trusted resume seam: execute a call whose M3 revalidation already succeeded.

        The caller owns the receipt check. This method still refuses a DENY evaluation and
        re-checks the current tool binding so a registry change cannot slip between revalidation
        and execution.
        """

        if evaluation.outcome == "DENY" or evaluation.failure_message is not None:
            return _failure(evaluation.failure_message or "action_denied: evaluation denied")
        tool = self._registry.get(evaluation.tool_name)
        if tool is None:
            return _failure(f'Tool "{evaluation.tool_name}" is not registered.')
        current_binding = self._binding_digest(evaluation.tool_name, tool)
        if evaluation.tool_binding_digest and current_binding != evaluation.tool_binding_digest:
            return _failure("action_denied: tool binding changed before execution")
        self._audit(evaluation, human_outcome=human_outcome, executed=True)
        return await self._execute(
            evaluation.tool_name,
            evaluation.canonical_arguments,
            context,
            idempotency_key=idempotency_key,
        )

    async def execute_batch(
        self, calls: list[BatchToolCall], context: ToolUseContext
    ) -> dict[str, ToolResult]:
        results: dict[str, ToolResult] = {}

        async def run(call: BatchToolCall) -> tuple[str, ToolResult]:
            return call.id, await self.execute(
                call.name, call.input, context, idempotency_key=call.id
            )

        import asyncio

        pairs = await asyncio.gather(*(run(call) for call in calls))
        results.update(pairs)
        return results

    # -- evaluation internals ------------------------------------------------

    def _record_trajectory(self, evaluation: ActionEvaluation, phase: str) -> ActionEvaluation:
        recorder = self.trajectory_recorder
        if recorder is None:
            return evaluation
        call_context = current_call_trajectory()
        request = evaluation.request
        decision = evaluation.decision
        action_context = request.context if request is not None else None
        call_id = (
            call_context.call_id
            if call_context is not None and call_context.call_id
            else evaluation.call_id
        )
        tool_id = (
            call_context.tool_id
            if call_context is not None and call_context.tool_id
            else evaluation.tool_name
        )
        basis = evaluation.decision_basis_digest
        if not basis:
            basis = canonical_digest(
                {
                    "tool_id": tool_id,
                    "phase": phase,
                    "decision": evaluation.outcome,
                    "reason_codes": list(evaluation.reason_codes),
                }
            )
        try:
            recorder.record_action_decision(
                phase=phase,
                decision=evaluation.outcome,
                decision_source=self._decision_source(evaluation),
                tool_id=tool_id,
                call_id=call_id,
                policy_rule_id=decision.matched_rule if decision is not None else None,
                decision_basis_digest=basis,
                action_digest=evaluation.action_digest,
                input_digest=evaluation.input_digest,
                tool_binding_digest=evaluation.tool_binding_digest,
                reason_codes=evaluation.reason_codes,
                scope_alignment=(decision.scope_alignment if decision is not None else None),
                reviewer_verdict=(decision.reviewer_verdict if decision is not None else None),
                external_policy_outcome=(
                    decision.external_policy_outcome if decision is not None else None
                ),
                occurred_at=datetime.now(UTC),
                correlation=TrajectoryCorrelation(
                    model_response_id=(
                        call_context.model_response_id if call_context is not None else None
                    ),
                    ordinal=(call_context.ordinal if call_context is not None else None),
                    call_id=call_id,
                    tool_id=tool_id,
                    worker_id=(action_context.worker_id if action_context is not None else None),
                    profile_id=(action_context.profile_id if action_context is not None else None),
                ),
            )
        except Exception:  # noqa: BLE001 - evidence must never change runtime behavior
            logger.warning("M7 trajectory recording failed for M3 decision", exc_info=True)
        return evaluation

    @staticmethod
    def _decision_source(evaluation: ActionEvaluation) -> str:
        reasons = set(evaluation.reason_codes)
        if "hard_denied" in reasons:
            return "hard_security"
        if "invalid_input" in reasons:
            return "input_validation"
        if "not_registered" in reasons:
            return "registry"
        if "trusted_context_required" in reasons:
            return "identity"
        decision = evaluation.decision
        if decision is not None and decision.external_policy_outcome is not None:
            return "external_policy"
        if "scope_review_escalated" in reasons:
            return "reviewer"
        return "policy"

    async def _evaluate(
        self,
        tool_name: str,
        canonical_arguments: dict[str, Any],
        context: ToolUseContext,
        call_id: str,
    ) -> ActionEvaluation:
        execution_context = context.execution_context
        assert execution_context is not None
        principal = execution_context.principal
        agent = _canonical_agent(context.agent.name)
        resolution = self.catalog.resolve(tool_name, canonical_arguments)
        action_context = _action_context(context)
        base = ActionRequest(
            principal=principal,
            agent=agent,
            action=resolution.action,
            resource=resolution.resource,
            context=action_context,
            tool_name=tool_name,
            call_id=call_id,
            canonical_arguments=canonical_arguments,
        )
        policy = self.policy.decide(base)
        alignment = _scope_alignment(resolution, action_context.operating_scope)
        decision = ActionDecision(
            decision=policy.decision,
            reason_codes=policy.reason_codes,
            matched_rule=policy.matched_rule,
            policy_name=policy.policy_name,
            scope_alignment=alignment,
        )

        reviewer_verdict = None
        if policy.decision == "ALLOW" and alignment != "ALIGNED":
            reviewed = await self._review(base, resolution, action_context, context)
            reviewer_verdict = reviewed.verdict
            if reviewed.verdict != "CONSISTENT":
                decision = decision.model_copy(
                    update={
                        "decision": "REQUIRE_HUMAN",
                        "reason_codes": (
                            *decision.reason_codes,
                            *reviewed.reason_codes,
                            "scope_review_escalated",
                        ),
                    }
                )
            else:
                decision = decision.model_copy(
                    update={"reason_codes": (*decision.reason_codes, *reviewed.reason_codes)}
                )

        external_outcome = None
        if self.external_policy is not None and decision.decision != "DENY":
            external_allowed, external_outcome, external_codes = await self._external_policy(
                base, context
            )
            if not external_allowed:
                decision = decision.model_copy(
                    update={
                        "decision": "DENY",
                        "reason_codes": (*decision.reason_codes, *external_codes),
                        "external_policy_outcome": external_outcome,
                    }
                )
            else:
                decision = decision.model_copy(update={"external_policy_outcome": external_outcome})

        request = base.model_copy(update={"digest": _digest(base, decision)})
        decision = decision.model_copy(update={"reviewer_verdict": reviewer_verdict})
        tool = self._registry.get(tool_name)
        assert tool is not None
        binding = self._binding_digest(tool_name, tool)
        action_hash = compute_action_digest(
            principal=principal,
            agent=agent,
            action=request.action,
            resource=request.resource,
            canonical_arguments=canonical_arguments,
            logical_tool_id=tool_name,
        )
        basis_hash = compute_decision_basis_digest(
            decision=decision.decision,
            matched_rule=decision.matched_rule,
            policy_name=decision.policy_name,
            policy_digest=self.policy_digest,
            reason_codes=decision.reason_codes,
            scope_alignment=decision.scope_alignment,
            reviewer_verdict=decision.reviewer_verdict,
            external_policy_outcome=decision.external_policy_outcome,
            operating_scope=action_context.operating_scope,
            worker_profile=action_context.worker_profile,
            tenant=action_context.tenant,
            classification=action_context.classification,
            tool_binding=binding,
        )
        return ActionEvaluation(
            tool_name=tool_name,
            call_id=call_id,
            outcome=decision.decision,
            canonical_arguments=canonical_arguments,
            input_digest=compute_input_digest(canonical_arguments),
            action_digest=action_hash,
            decision_basis_digest=basis_hash,
            tool_binding_digest=binding,
            request=request,
            decision=decision,
            policy=policy,
            reason_codes=decision.reason_codes,
        )

    def _deny(
        self,
        tool_name: str,
        call_id: str,
        reason: str,
        failure_message: str,
    ) -> ActionEvaluation:
        return ActionEvaluation(
            tool_name=tool_name,
            call_id=call_id,
            outcome="DENY",
            failure_message=failure_message,
            reason_codes=(reason,),
        )

    async def _review(
        self,
        request: ActionRequest,
        resolution: ActionResolution,
        action_context: ActionContext,
        context: ToolUseContext,
    ) -> ActionReviewResult:
        del resolution
        if self.reviewer is None:
            return ActionReviewResult(verdict="UNCERTAIN", reason_codes=("reviewer_unavailable",))
        review_request = ActionReviewRequest(
            principal=request.principal,
            agent=request.agent,
            action=request.action,
            resource=request.resource,
            task_id=action_context.task_id,
            operating_scope=action_context.operating_scope,
            worker_profile=action_context.worker_profile,
            canonical_arguments=request.canonical_arguments,
        )
        try:
            return await self.reviewer.review(review_request)
        except Exception:
            return ActionReviewResult(verdict="UNCERTAIN", reason_codes=("reviewer_error",))

    async def _external_policy(
        self, request: ActionRequest, context: ToolUseContext
    ) -> tuple[bool, str, tuple[str, ...]]:
        assert context.execution_context is not None
        assert self.external_policy is not None
        policy_request = PolicyRequest(
            run_id=request.context.run_id or request.call_id,
            task_id=request.context.task_id,
            action=request.action,
            resource=request.resource,
            boundary="tool",
            context=context.execution_context,
            correlation_id=request.call_id,
            input={
                "tool_name": request.tool_name,
                "canonical_arguments": request.canonical_arguments,
            },
        )
        try:
            result = await self.external_policy.enforce(policy_request)
        except Exception:
            return False, "error", ("external_policy_error",)
        if not result.allowed:
            return (
                False,
                "deny",
                tuple(result.decision.reason_codes) or ("external_policy_denied",),
            )
        return True, "allow", ()

    async def _request_approval(self, evaluation: ActionEvaluation, context: ToolUseContext):
        manager = self.approval_manager
        if manager is None:
            return None
        request = evaluation.request
        assert request is not None
        decision = evaluation.decision
        assert decision is not None
        approval_context = {
            "call_id": request.call_id,
            "digest": request.digest,
            "tool_name": request.tool_name,
            "agent": request.agent,
            "principal": request.principal,
            "action": request.action,
            "resource": request.resource,
            "canonical_arguments": request.canonical_arguments,
            "policy_decision": decision.decision,
            "matched_rule": decision.matched_rule,
        }
        try:
            response = await manager.check_and_request(
                request_type="tool_call",
                agent=context.agent.name,
                description=(f"M3 approval required for {request.action} on {request.resource}"),
                context=approval_context,
            )
        except Exception:
            return None
        if response is None:
            return None
        history = getattr(manager, "history", [])
        if not history:
            return None
        last_request, last_response = history[-1]
        if (
            last_request.id != response.request_id
            or last_response.request_id != response.request_id
            or last_request.context != approval_context
        ):
            return None
        return response

    async def _execute(
        self,
        tool_name: str,
        canonical_arguments: dict[str, Any],
        context: ToolUseContext,
        *,
        idempotency_key: str | None,
    ) -> ToolResult:
        return await self._executor.execute(
            tool_name,
            canonical_arguments,
            context,
            idempotency_key=idempotency_key,
        )

    def _binding_digest(self, tool_name: str, tool) -> str:
        provider_name: str | None = None
        provider_lookup = getattr(self._registry, "provider_name", None)
        if callable(provider_lookup):
            observed = provider_lookup(tool_name)
            provider_name = str(observed) if observed is not None else None
        return compute_tool_binding_digest(
            tool,
            source=self.tool_sources.get(tool_name) or infer_tool_source(tool),
            provider_name=provider_name,
        )

    def _hard_precheck(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        context: ToolUseContext,
        resolution,
    ) -> None:
        if resolution.hard_path:
            path = arguments.get("path")
            if path is not None:
                resolve_tool_path(str(path), context)
        if tool_name == "bash":
            validate_shell_command(str(arguments.get("command", "")), context)

    def _audit(
        self,
        evaluation: ActionEvaluation,
        *,
        human_outcome: str | None,
        executed: bool,
    ) -> None:
        request = evaluation.request
        decision = evaluation.decision
        if request is None or decision is None:
            return
        audit = ActionAudit(
            call_id=request.call_id,
            principal=request.principal,
            agent=request.agent,
            tool_name=request.tool_name,
            action=request.action,
            resource=request.resource,
            digest=request.digest,
            decision=decision.decision,
            task_id=request.context.task_id,
            worker_id=request.context.worker_id,
            profile_id=request.context.profile_id,
            parent_agent=request.context.parent_agent,
            operating_scope=request.context.operating_scope,
            matched_rule=decision.matched_rule,
            reviewer_verdict=decision.reviewer_verdict,
            human_outcome=human_outcome,
            executed=executed,
            reason_codes=decision.reason_codes,
            policy_name=decision.policy_name,
        )
        self.audits.append(audit)
        logger.info(
            "m3_action_decision action=%s tool=%s agent=%s principal=%s decision=%s "
            "matched=%s call_id=%s digest=%s executed=%s",
            audit.action,
            audit.tool_name,
            audit.agent,
            audit.principal,
            audit.decision,
            audit.matched_rule,
            audit.call_id,
            audit.digest,
            audit.executed,
        )


def _canonical_agent(name: str) -> str:
    value = name.strip()
    if value.startswith("agent:"):
        return value
    return f"agent:{value}"


def _action_context(context: ToolUseContext) -> ActionContext:
    execution_context = context.execution_context
    assert execution_context is not None
    attributes = execution_context.attributes
    nested = attributes.get("atlas_worker")
    worker = nested if isinstance(nested, dict) else {}

    def attr(key: str) -> str | None:
        value = attributes.get(key)
        if value is None:
            value = worker.get(key)
        return str(value) if value is not None else None

    def worker_scope() -> str | None:
        flat = attr("operating_scope")
        if flat is not None:
            return flat
        scopes = worker.get("operating_scope")
        if isinstance(scopes, list) and scopes:
            return str(scopes[0])
        return None

    return ActionContext(
        run_id=attr("run_id"),
        task_id=attr("task_id"),
        worker_profile=attr("worker_profile") or attr("profile_id"),
        worker_id=attr("worker_id"),
        profile_id=attr("profile_id"),
        parent_agent=attr("parent_agent"),
        operating_scope=worker_scope(),
        tenant=execution_context.tenant_scope,
        classification=execution_context.classification,
        trace_id=execution_context.trace_id,
    )


def _scope_alignment(resolution: ActionResolution, operating_scope: str | None) -> ScopeAlignment:
    if not resolution.expected_scopes:
        return "ALIGNED"
    if not operating_scope:
        return "UNKNOWN"
    return "ALIGNED" if operating_scope in resolution.expected_scopes else "MISMATCH"


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _digest(request: ActionRequest, decision: ActionDecision) -> str:
    payload = {
        "principal": request.principal,
        "agent": request.agent,
        "action": request.action,
        "resource": request.resource,
        "canonical_arguments": request.canonical_arguments,
        "decision": decision.decision,
        "matched_rule": decision.matched_rule,
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _failure(message: str, *, retry_safe: bool = False) -> ToolResult:
    return ToolResult(data=message, is_error=True, retry_safe=retry_safe)
