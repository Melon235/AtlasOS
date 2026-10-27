"""Central authorization and explicit delegation runtime.

Central authorization is bounded by a trusted, durable RunAuthority ceiling and does not use the
requester's current scopes. Explicit delegation is a separate attenuation operation bounded by the
parent's effective scopes.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from typing import Literal

from anycode.identity.context import ExecutionContext
from anycode.types import AgentConfig, ToolUseContext

from atlasos.delegation.authority import (
    RunAuthority,
    RunAuthorityStore,
    run_authority_hash,
)
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
    AuthorizationOutcome,
    AuthorizationRequest,
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


class AuthorizationRuntime:
    """One central authority runtime for a run; all agents use the same pipeline."""

    def __init__(
        self,
        *,
        store: ExecutionContextStore,
        run_authority: RunAuthority,
        authority_store: RunAuthorityStore,
        base_context: ExecutionContext,
        main_agent: str,
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
        if store.run_id != run_authority.run_id or authority_store.run_id != run_authority.run_id:
            raise ValueError("run authority, authority store, and context store run ids must match")
        if base_context.delegation:
            raise ValueError("trusted base context must not carry a capability chain")
        if base_context.principal != run_authority.principal:
            raise ValueError("trusted base context principal must match run authority")

        self.run_authority = run_authority
        self.authority_store = authority_store
        self.store = store
        self.base_context = base_context
        self.main_agent = canonical_actor(main_agent)
        self.validator = validator or DelegationValidator()
        self.router = router or IntentRouter()
        self.reviewer = reviewer
        self.review_mode = review_mode
        self.policy_gate = policy_gate or DelegationPolicyGate(None)
        self.approval_gate = approval_gate or DelegationApprovalGate(None)
        self.issuer = issuer or GrantIssuer(self.validator)
        self._lock = asyncio.Lock()

        stored_authority = authority_store.load()
        if stored_authority is None:
            authority_store.save(run_authority)
        elif stored_authority != run_authority:
            raise ValueError("stored run authority does not match trusted bootstrap")

        self._contexts: dict[str, ExecutionContext] = store.load()
        main_context = self._contexts.get(self.main_agent)
        if main_context is None:
            self._contexts[self.main_agent] = base_context
            self.store.save_all(self._contexts)
        elif main_context.principal != run_authority.principal:
            raise ValueError("stored main agent principal does not match run authority")
        for actor, context in self._contexts.items():
            if context.delegation:
                self.validator.validate_context(context, expected_actor=actor)

        self.root_actor = self.main_agent
        self.stats: dict[str, int] = {
            "requests": 0,
            "granted": 0,
            "denied": 0,
            "central_authorized": 0,
            "explicit_delegated": 0,
            "reviewer_agree": 0,
            "reviewer_disagree": 0,
            "reviewer_uncertain": 0,
            "reviewer_failure": 0,
            "policy_allow": 0,
            "policy_deny": 0,
            "hitl_approve": 0,
            "hitl_deny": 0,
        }

    @property
    def run_id(self) -> str:
        return self.run_authority.run_id

    def context_for(self, actor: str) -> ExecutionContext | None:
        try:
            canonical = canonical_actor(actor)
        except InvalidActor:
            return None
        return self._contexts.get(canonical)

    def context_hash_for(self, actor: str) -> str | None:
        context = self.context_for(actor)
        return context_hash(context) if context is not None else None

    def run_authority_hash(self) -> str:
        return run_authority_hash(self.run_authority)

    def build_child_agent_config(
        self,
        base_config: AgentConfig,
        outcome: AuthorizationOutcome,
    ) -> AgentConfig:
        """Attach only a committed central/delegated child context to a child AgentConfig."""

        if outcome.status != "granted" or outcome.child_context is None:
            raise DelegationDenied(outcome)
        return base_config.model_copy(update={"execution_context": outcome.child_context})

    async def authorize_agent(
        self,
        tool_context: ToolUseContext,
        request: AuthorizationRequest,
    ) -> AuthorizationOutcome:
        """Central authorization: requester scopes are irrelevant; the run ceiling bounds it."""

        async with self._lock:
            self.stats["requests"] += 1
            request_id = uuid.uuid4().hex
            try:
                requester = canonical_actor(tool_context.agent.name)
            except InvalidActor:
                return self._denied(
                    request_id,
                    ("invalid_actor",),
                    operation="central_authorization",
                )
            if requester not in self._contexts:
                return self._denied(
                    request_id,
                    ("context_missing",),
                    operation="central_authorization",
                    requester_actor=requester,
                )
            try:
                target = canonical_actor(request.target_agent)
                scopes = canonicalize_scopes(request.requested_scopes)
            except (InvalidActor, InvalidScope) as error:
                code = "invalid_scope" if isinstance(error, InvalidScope) else "invalid_target"
                return self._denied(
                    request_id,
                    (code,),
                    operation="central_authorization",
                    requester_actor=requester,
                )
            if not scopes:
                return self._denied(
                    request_id,
                    ("empty_requested_scopes",),
                    operation="central_authorization",
                    requester_actor=requester,
                    target_agent=target,
                )
            outside = set(scopes) - set(self.run_authority.allowed_scopes)
            if outside:
                return self._denied(
                    request_id,
                    ("outside_run_authority",),
                    operation="central_authorization",
                    requester_actor=requester,
                    target_agent=target,
                )

            normalized = DelegationRequest(
                delegatee=target,
                intent=request.intent,
                requested_scopes=scopes,
                task_summary=request.task_summary,
                reason=request.reason,
            )
            return await self._pipeline(
                request_id=request_id,
                requester_actor=requester,
                target_agent=target,
                normalized=normalized,
                policy_context=self.base_context,
                reviewer_context=self._contexts[requester],
                operation="central_authorization",
                issue=lambda: self.issuer.issue_central(
                    base_context=self.base_context,
                    principal=self.run_authority.principal,
                    target_agent=target,
                    scopes=scopes,
                    expires_at=self.run_authority.expires_at,
                ),
            )

    async def delegate_capability(
        self,
        tool_context: ToolUseContext,
        request: DelegationRequest,
    ) -> AuthorizationOutcome:
        """Explicit attenuation: requested scopes must be within the parent's effective scopes."""

        async with self._lock:
            self.stats["requests"] += 1
            request_id = uuid.uuid4().hex
            try:
                actor = canonical_actor(tool_context.agent.name)
            except InvalidActor:
                return self._denied(
                    request_id,
                    ("invalid_actor",),
                    operation="explicit_delegation",
                )
            parent = self._contexts.get(actor)
            if parent is None:
                return self._denied(
                    request_id,
                    ("context_missing",),
                    operation="explicit_delegation",
                )
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
                return self._denied(
                    request_id,
                    (code,),
                    operation="explicit_delegation",
                    requester_actor=actor,
                )
            try:
                self.validator.validate_request(parent, actor, normalized)
            except DelegationValidationError as error:
                return self._denied(
                    request_id,
                    (error.code,),
                    operation="explicit_delegation",
                    requester_actor=actor,
                    target_agent=normalized.delegatee,
                )

            return await self._pipeline(
                request_id=request_id,
                requester_actor=actor,
                target_agent=normalized.delegatee,
                normalized=normalized,
                policy_context=parent,
                reviewer_context=parent,
                operation="explicit_delegation",
                issue=lambda: self.issuer.issue(
                    parent_context=parent,
                    actor=actor,
                    request=normalized,
                    expires_at=derive_child_expiry(parent),
                ),
            )

    async def propose(
        self,
        tool_context: ToolUseContext,
        request: DelegationRequest,
    ) -> AuthorizationOutcome:
        """Compatibility alias: delegatee becomes a central-authorization target.

        New code should call authorize_agent() for central authority or delegate_capability() for
        explicit attenuation.
        """

        return await self.authorize_agent(
            tool_context,
            AuthorizationRequest(
                target_agent=request.delegatee,
                intent=request.intent,
                requested_scopes=request.requested_scopes,
                task_summary=request.task_summary,
                reason=request.reason,
            ),
        )

    async def _pipeline(
        self,
        *,
        request_id: str,
        requester_actor: str,
        target_agent: str,
        normalized: DelegationRequest,
        policy_context: ExecutionContext,
        reviewer_context: ExecutionContext,
        operation: Literal["central_authorization", "explicit_delegation"],
        issue,
    ) -> AuthorizationOutcome:
        route = self.router.route(normalized.intent, normalized.requested_scopes)
        reviewer = await self._review(
            requester_actor,
            policy_context,
            reviewer_context,
            normalized,
            route,
        )
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
                actor=requester_actor,
                parent_context=policy_context,
                request=normalized,
            )
            self.stats["policy_allow" if policy_result.allowed else "policy_deny"] += 1
            if not policy_result.allowed:
                return self._denied(
                    request_id,
                    policy_result.reason_codes or ("policy_denied",),
                    operation=operation,
                    requester_actor=requester_actor,
                    target_agent=target_agent,
                    route=route,
                    reviewer=reviewer,
                    required_gates=required_gates,
                    policy_outcome=policy_result.outcome,
                )

        approval_result: GateResult | None = None
        if "HITL" in required_gates:
            approval_result = await self.approval_gate.request(
                actor=requester_actor,
                principal=policy_context.principal,
                parent_context=policy_context,
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
                    operation=operation,
                    requester_actor=requester_actor,
                    target_agent=target_agent,
                    route=route,
                    reviewer=reviewer,
                    required_gates=required_gates,
                    policy_outcome=policy_result.outcome if policy_result else None,
                    approval_outcome=approval_result.outcome,
                    approval_request_id=approval_result.decision_id,
                )

        try:
            grant, child_context = issue()
        except DelegationValidationError as error:
            return self._denied(
                request_id,
                (error.code,),
                operation=operation,
                requester_actor=requester_actor,
                target_agent=target_agent,
                route=route,
                reviewer=reviewer,
                required_gates=required_gates,
                policy_outcome=policy_result.outcome if policy_result else None,
                approval_outcome=approval_result.outcome if approval_result else None,
                approval_request_id=approval_result.decision_id if approval_result else None,
            )

        try:
            self.store.save_context(target_agent, child_context)
        except Exception:
            return self._denied(
                request_id,
                ("persistence_error",),
                operation=operation,
                requester_actor=requester_actor,
                target_agent=target_agent,
                route=route,
                reviewer=reviewer,
                required_gates=required_gates,
                policy_outcome=policy_result.outcome if policy_result else None,
                approval_outcome=approval_result.outcome if approval_result else None,
                approval_request_id=approval_result.decision_id if approval_result else None,
            )
        self._contexts[target_agent] = child_context
        self.stats["granted"] += 1
        self.stats[
            "central_authorized" if operation == "central_authorization" else "explicit_delegated"
        ] += 1
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
            operation=operation,
            requester_actor=requester_actor,
            target_agent=target_agent,
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
        principal_context: ExecutionContext,
        reviewer_context: ExecutionContext,
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
            principal=principal_context.principal,
            current_actor=actor,
            effective_scopes=tuple(sorted(effective_scopes(reviewer_context))),
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
        operation: Literal["central_authorization", "explicit_delegation"],
        requester_actor: str | None = None,
        target_agent: str | None = None,
        route=None,
        reviewer: ReviewerResult | None = None,
        required_gates: tuple[Gate, ...] = (),
        policy_outcome: str | None = None,
        approval_outcome: str | None = None,
        approval_request_id: str | None = None,
    ) -> AuthorizationOutcome:
        self.stats["denied"] += 1
        return DelegationOutcome(
            request_id=request_id,
            status="denied",
            operation=operation,
            requester_actor=requester_actor,
            target_agent=target_agent,
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


# Backward-compatible name for the explicit-delegation era. Both APIs live on this runtime.
DelegationRuntime = AuthorizationRuntime
