"""Hard structural/authority validation before any routing, review, policy, or HITL."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from anycode.identity.context import ExecutionContext

from atlasos.delegation.capabilities import (
    InvalidScope,
    canonical_actor,
    canonicalize_scopes,
    effective_scopes,
)
from atlasos.delegation.models import DelegationRequest


class DelegationValidationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class DelegationValidator:
    """Fail-closed checks over a parent context and untrusted request."""

    def __init__(self, *, now: Callable[[], datetime] | None = None) -> None:
        self._now = now or (lambda: datetime.now(UTC))

    def validate_context(
        self,
        context: ExecutionContext,
        *,
        expected_actor: str | None = None,
    ) -> None:
        grants = context.delegation
        if not grants:
            raise DelegationValidationError(
                "delegation_chain_missing",
                "a capability-bearing agent requires a non-empty delegation chain",
            )
        if grants[0].delegator != context.principal:
            raise DelegationValidationError(
                "invalid_root_delegator",
                "the first grant's delegator must be the context principal",
            )
        path: set[str] = set()
        for index, grant in enumerate(grants):
            if grant.delegatee in path:
                raise DelegationValidationError(
                    "delegation_cycle",
                    f"delegatee {grant.delegatee!r} appears more than once in the chain",
                )
            path.add(grant.delegatee)
            if index > 0:
                prior = grants[index - 1]
                if prior.delegatee != grant.delegator:
                    raise DelegationValidationError(
                        "chain_not_contiguous",
                        "grant[i].delegatee must equal grant[i+1].delegator",
                    )
                if not set(grant.scopes) <= effective_scopes_from_grants(grants[:index]):
                    raise DelegationValidationError(
                        "scope_escalation",
                        "a child grant exceeds its parent effective scopes",
                    )
            if grant.expires_at is not None and grant.expires_at <= self._now():
                raise DelegationValidationError(
                    "expired_grant",
                    f"grant {index} is expired",
                )
        if expected_actor is not None:
            actor = canonical_actor(expected_actor)
            if grants[-1].delegatee != actor:
                raise DelegationValidationError(
                    "actor_not_chain_tail",
                    f"current actor {actor!r} is not the delegation chain tail",
                )

    def validate_request(
        self,
        parent_context: ExecutionContext,
        actor: str,
        request: DelegationRequest,
    ) -> None:
        current_actor = canonical_actor(actor)
        delegatee = canonical_actor(request.delegatee)
        self.validate_context(parent_context, expected_actor=current_actor)

        if delegatee == current_actor:
            raise DelegationValidationError(
                "delegatee_is_actor",
                "an agent cannot delegate authority to itself",
            )
        path = {grant.delegatee for grant in parent_context.delegation}
        if delegatee in path:
            raise DelegationValidationError(
                "delegation_cycle",
                f"delegatee {delegatee!r} already appears in the ancestor delegation path",
            )
        if not request.requested_scopes:
            raise DelegationValidationError(
                "empty_requested_scopes",
                "at least one requested scope is required",
            )
        try:
            requested_scopes = canonicalize_scopes(request.requested_scopes)
        except InvalidScope as error:
            raise DelegationValidationError("invalid_scope", str(error)) from error
        parent_scopes = effective_scopes(parent_context)
        missing = set(requested_scopes) - parent_scopes
        if missing:
            raise DelegationValidationError(
                "scope_escalation",
                f"requested scopes exceed parent effective scopes: {sorted(missing)}",
            )


def effective_scopes_from_grants(grants) -> frozenset[str]:
    if not grants:
        return frozenset()
    return frozenset.intersection(*(frozenset(grant.scopes) for grant in grants))
