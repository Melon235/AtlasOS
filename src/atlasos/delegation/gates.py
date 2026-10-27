"""Delegation-only POLICY and HITL gates using existing AnyCode seams."""

from __future__ import annotations

from dataclasses import dataclass

from anycode.hitl.approval import ApprovalManager
from anycode.identity.context import ExecutionContext
from anycode.identity.policy import PolicyEnforcer, PolicyRequest

from atlasos.delegation.models import DelegationRequest, ReviewerResult, RouteDecision


@dataclass(frozen=True)
class GateResult:
    allowed: bool
    outcome: str
    reason_codes: tuple[str, ...] = ()
    decision_id: str | None = None


class DelegationPolicyGate:
    """One PolicyRequest per delegation request; never mutates ExecutionContext."""

    def __init__(self, enforcer: PolicyEnforcer | None) -> None:
        self._enforcer = enforcer

    async def evaluate(
        self,
        *,
        run_id: str,
        request_id: str,
        actor: str,
        parent_context: ExecutionContext,
        request: DelegationRequest,
    ) -> GateResult:
        if self._enforcer is None:
            return GateResult(False, "unavailable", ("policy_unavailable",))
        policy_request = PolicyRequest(
            run_id=run_id,
            task_id=None,
            action="delegate",
            resource=f"agent:{request.delegatee.removeprefix('agent:')}",
            boundary="a2a",
            context=parent_context,
            correlation_id=request_id,
            input={
                "intent": request.intent,
                "requested_scopes": list(request.requested_scopes),
                "task_summary": request.task_summary,
                "current_actor": actor,
            },
        )
        try:
            result = await self._enforcer.enforce(policy_request)
        except Exception:
            return GateResult(False, "error", ("policy_error",))
        if not result.allowed:
            codes = tuple(result.decision.reason_codes) or ("policy_denied",)
            return GateResult(
                False,
                "deny",
                codes,
                decision_id=result.decision.id,
            )
        if {"policy_not_configured", "policy_unavailable"} & set(result.decision.reason_codes):
            return GateResult(
                False,
                "unavailable",
                ("policy_unavailable",),
                decision_id=result.decision.id,
            )
        return GateResult(True, "allow", decision_id=result.decision.id)


class DelegationApprovalGate:
    """HITL using AnyCode ApprovalManager; fail closed when absent or denied."""

    def __init__(self, manager: ApprovalManager | None) -> None:
        self._manager = manager

    async def request(
        self,
        *,
        actor: str,
        principal: str,
        parent_context: ExecutionContext,
        request: DelegationRequest,
        route: RouteDecision,
        reviewer: ReviewerResult,
        request_id: str,
    ) -> GateResult:
        if self._manager is None:
            return GateResult(False, "unavailable", ("approval_unavailable",))
        description = (
            f"Delegate to {request.delegatee} "
            f"(intent={request.intent}, scopes={', '.join(request.requested_scopes)})"
        )
        context = {
            "principal": principal,
            "from_agent": actor,
            "to_agent": request.delegatee,
            "task_summary": request.task_summary,
            "reason": request.reason or "",
            "intent": request.intent,
            "requested_scopes": list(request.requested_scopes),
            "router_gates": list(route.required_gates),
            "router_reason_codes": list(route.reason_codes),
            "reviewer_verdict": reviewer.verdict,
            "reviewer_reason_codes": list(reviewer.reason_codes),
            "tenant_scope": parent_context.tenant_scope,
            "delegation_request_id": request_id,
        }
        try:
            response = await self._manager.check_and_request(
                request_type="task",
                agent=actor,
                description=description,
                context=context,
            )
        except Exception:
            return GateResult(False, "error", ("approval_error",))
        if response is None:
            return GateResult(False, "unavailable", ("approval_unavailable",))
        if not response.approved:
            return GateResult(
                False,
                "deny",
                ("approval_denied",),
                decision_id=response.request_id,
            )
        return GateResult(True, "approve", decision_id=response.request_id)
