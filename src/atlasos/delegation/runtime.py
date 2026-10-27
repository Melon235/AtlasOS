"""Runtime orchestration: validate → route → review → gates → issue → revalidate → persist."""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from typing import Literal

from anycode.identity.context import ExecutionContext
from anycode.types import AgentConfig, ToolUseContext

from atlasos.delegation.capabilities import (
    InvalidActor,
    InvalidScope,
    canonical_actor,
    canonicalize_scopes,
    context_hash,
    derive_child_expiry,
    effective_scopes,
)
from atlasos.delegation.gates import (
    DelegationApprovalGate,
    DelegationPolicyGate,
    GateResult,
)
from atlasos.delegation.issuer import GrantIssuer
from atlasos.delegation.models import (
    GATE_ORDER,
    DelegationDenied,
    DelegationOutcome,
    DelegationRequest,
    Gate,
    ReviewerInput,
    ReviewerResult,
)
from atlasos.delegation.reviewer import DecisionReviewer
from atlasos.delegation.router import IntentRouter
from atlasos.delegation.store import ExecutionContextStore
from atlasos.delegation.validator import (
    DelegationValidationError,
    DelegationValidator,
)

ReviewMode = Literal["always", "protected_only", "off"]


def _gate_tuple(gates: set[Gate]) -> tuple[Gate, ...]:
    return tuple(gate for gate in GATE_ORDER if gate in gates)


class DelegationRuntime:
    """Trusted delegation runtime for one run."""

    def __init__(
        self,
        *,
        run_id: str,
        store: ExecutionContextStore,
        root_context: ExecutionContext,
        validator: DelegationValidator | None = None,
        router: IntentRouter | None = None,
        reviewer: DecisionReviewer | None = None,
        review_mode: ReviewMode = "always",
        policy_gate: DelegationPolicyGate | None = None,
        approval_gate: DelegationApprovalGate | None = None,
        issuer: GrantIssuer | None = None,
    ) -> None:
        if review_mode not in ("always", "protected_only", "off"):
            raise ValueError(f"invalid review_mode {review_mode!r}")
        self.run_id = run_id
        self.store = store
        self.validator = validator or DelegationValidator()
        self.router = router or IntentRouter()
        self.reviewer = reviewer
        self.review_mode = review_mode
        self.policy_gate = policy_gate or DelegationPolicyGate(None)
        self.approval_gate = approval_gate or DelegationApprovalGate(None)
        self.issuer = issuer or GrantIssuer(self.validator)
        self._lock = asyncio.Lock()
        self._contexts: dict[str, ExecutionContext] = store.load()

        if not root_context.delegation:
            raise ValueError("trusted bootstrap must provide a non-empty root delegation chain")
        root_actor = canonical_actor(root_context.delegation[-1].delegatee)
        self.validator.validate_context(root_context, expected_actor=root_actor)
        existing_root = self._contexts.get(root_actor)
        if existing_root is None:
            self._contexts[root_actor] = root_context
            self.store.save_all(self._contexts)
        elif existing_root != root_context:
            raise ValueError("stored root execution context does not match trusted bootstrap")
        for actor, context in self._contexts.items():
            self.validator.validate_context(context, expected_actor=actor)
        self.root_actor = root_actor
        self.stats: dict[str, int] = {
            "requests": 0,
            "granted": 0,
            "denied": 0,
            "reviewer_agree": 0,
            "reviewer_disagree": 0,
            "reviewer_uncertain": 0,
            "reviewer_failure": 0,
            "policy_allow": 0,
            "policy_deny": 0,
            "hitl_approve": 0,
            "hitl_deny": 0,
        }

    def context_for(self, actor: str) -> ExecutionContext | None:
        try:
            canonical = canonical_actor(actor)
        except InvalidActor:
            return None
        return self._contexts.get(canonical)

    def context_hash_for(self, actor: str) -> str | None:
        context = self.context_for(actor)
        return context_hash(context) if context is not None else None

    def build_child_agent_config(
        self,
        base_config: AgentConfig,
        outcome: DelegationOutcome,
    ) -> AgentConfig:
        """Sub-agent seam: only a committed child context may be attached."""

        if outcome.status != "granted" or outcome.child_context is None:
            raise DelegationDenied(outcome)
        return base_config.model_copy(update={"execution_context": outcome.child_context})

    async def propose(
        self,
        tool_context: ToolUseContext,
        request: DelegationRequest,
    ) -> DelegationOutcome:
        """Accept only the request fields; identity comes from trusted runtime state."""

        async with self._lock:
            self.stats["requests"] += 1
            request_id = uuid.uuid4().hex
            try:
                actor = canonical_actor(tool_context.agent.name)
            except InvalidActor:
                return self._denied(request_id, ("invalid_actor",))
            parent = self._contexts.get(actor)
            if parent is None:
                return self._denied(request_id, ("context_missing",))
            try:
                normalized = DelegationRequest(
                    delegatee=canonical_actor(request.delegatee),
                    intent=request.intent,
                    requested_scopes=canonicalize_scopes(request.requested_scopes),
                    task_summary=request.task_summary,
                    reason=request.reason,
                )
            except (InvalidScope, InvalidActor) as error:
                code = "invalid_scope" if isinstance(error, InvalidScope) else "invalid_delegatee"
                return self._denied(request_id, (code,))

            try:
                self.validator.validate_request(parent, actor, normalized)
            except DelegationValidationError as error:
                return self._denied(request_id, (error.code,))

            route = self.router.route(normalized.intent, normalized.requested_scopes)
            reviewer = await self._review(actor, parent, normalized, route)
            gates = set(route.required_gates)
            review_codes = list(reviewer.reason_codes)
            if reviewer.verdict != "AGREE":
                gates.add("HITL")
                review_codes.append(
                    "reviewer_disagree" if reviewer.verdict == "DISAGREE" else "reviewer_uncertain"
                )
            required_gates = _gate_tuple(gates)

            policy_result: GateResult | None = None
            if "POLICY" in required_gates:
                policy_result = await self.policy_gate.evaluate(
                    run_id=self.run_id,
                    request_id=request_id,
                    actor=actor,
                    parent_context=parent,
                    request=normalized,
                )
                self.stats["policy_allow" if policy_result.allowed else "policy_deny"] += 1
                if not policy_result.allowed:
                    return self._denied(
                        request_id,
                        policy_result.reason_codes or ("policy_denied",),
                        route=route,
                        reviewer=reviewer,
                        required_gates=required_gates,
                        policy_outcome=policy_result.outcome,
                    )

            approval_result: GateResult | None = None
            if "HITL" in required_gates:
                approval_result = await self.approval_gate.request(
                    actor=actor,
                    principal=parent.principal,
                    parent_context=parent,
                    request=normalized,
                    route=route,
                    reviewer=reviewer,
                    request_id=request_id,
                )
                self.stats["hitl_approve" if approval_result.allowed else "hitl_deny"] += 1
                if not approval_result.allowed:
                    return self._denied(
                        request_id,
                        approval_result.reason_codes or ("approval_denied",),
                        route=route,
                        reviewer=reviewer,
                        required_gates=required_gates,
                        policy_outcome=policy_result.outcome if policy_result else None,
                        approval_outcome=approval_result.outcome,
                        approval_request_id=approval_result.decision_id,
                    )

            try:
                grant, child_context = self.issuer.issue(
                    parent_context=parent,
                    actor=actor,
                    request=normalized,
                    expires_at=derive_child_expiry(parent),
                )
            except DelegationValidationError as error:
                return self._denied(
                    request_id,
                    (error.code,),
                    route=route,
                    reviewer=reviewer,
                    required_gates=required_gates,
                    policy_outcome=policy_result.outcome if policy_result else None,
                    approval_outcome=approval_result.outcome if approval_result else None,
                    approval_request_id=approval_result.decision_id if approval_result else None,
                )

            try:
                self.store.save_context(normalized.delegatee, child_context)
            except Exception:
                return self._denied(
                    request_id,
                    ("persistence_error",),
                    route=route,
                    reviewer=reviewer,
                    required_gates=required_gates,
                    policy_outcome=policy_result.outcome if policy_result else None,
                    approval_outcome=approval_result.outcome if approval_result else None,
                    approval_request_id=approval_result.decision_id if approval_result else None,
                )
            self._contexts[normalized.delegatee] = child_context
            self.stats["granted"] += 1
            reason_codes = tuple(
                sorted(
                    {
                        *route.reason_codes,
                        *review_codes,
                        *(policy_result.reason_codes if policy_result else ()),
                        *(approval_result.reason_codes if approval_result else ()),
                        "grant_issued",
                    }
                )
            )
            return DelegationOutcome(
                request_id=request_id,
                status="granted",
                reason_codes=reason_codes,
                route=route,
                reviewer=reviewer,
                required_gates=required_gates,
                policy_outcome=policy_result.outcome if policy_result else None,
                approval_outcome=approval_result.outcome if approval_result else None,
                approval_request_id=(
                    approval_result.decision_id if approval_result is not None else None
                ),
                grant=grant,
                child_context=child_context,
            )

    async def _review(
        self,
        actor: str,
        parent: ExecutionContext,
        request: DelegationRequest,
        route,
    ) -> ReviewerResult:
        if self.review_mode == "off":
            return ReviewerResult(verdict="AGREE", reason_codes=("review_off",))
        if self.review_mode == "protected_only" and not route.required_gates:
            return ReviewerResult(verdict="AGREE", reason_codes=("review_not_required",))
        if self.reviewer is None:
            self.stats["reviewer_uncertain"] += 1
            self.stats["reviewer_failure"] += 1
            return ReviewerResult(verdict="UNCERTAIN", reason_codes=("reviewer_unavailable",))
        reviewer_input = ReviewerInput(
            principal=parent.principal,
            current_actor=actor,
            effective_scopes=tuple(sorted(effective_scopes(parent))),
            delegatee=request.delegatee,
            intent=request.intent,
            requested_scopes=request.requested_scopes,
            task_summary=request.task_summary,
            reason=request.reason,
            matched_rules=route.matched_rules,
            router_gates=route.required_gates,
            router_reason_codes=route.reason_codes,
        )
        try:
            result = await self.reviewer.review(reviewer_input)
        except Exception:
            self.stats["reviewer_uncertain"] += 1
            self.stats["reviewer_failure"] += 1
            return ReviewerResult(verdict="UNCERTAIN", reason_codes=("reviewer_error",))
        key = {
            "AGREE": "reviewer_agree",
            "DISAGREE": "reviewer_disagree",
            "UNCERTAIN": "reviewer_uncertain",
        }[result.verdict]
        self.stats[key] += 1
        return result

    def _denied(
        self,
        request_id: str,
        reason_codes: tuple[str, ...],
        *,
        route=None,
        reviewer: ReviewerResult | None = None,
        required_gates: tuple[Gate, ...] = (),
        policy_outcome: str | None = None,
        approval_outcome: str | None = None,
        approval_request_id: str | None = None,
    ) -> DelegationOutcome:
        self.stats["denied"] += 1
        return DelegationOutcome(
            request_id=request_id,
            status="denied",
            reason_codes=reason_codes,
            route=route,
            reviewer=reviewer,
            required_gates=required_gates,
            policy_outcome=policy_outcome,
            approval_outcome=approval_outcome,
            approval_request_id=approval_request_id,
        )

    def snapshot_hash(self) -> str:
        """Stable hash over all committed contexts, used by recovery tests."""

        payload = json.dumps(
            {
                actor: context.model_dump(mode="json")
                for actor, context in sorted(self._contexts.items())
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()
