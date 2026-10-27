"""Runtime-internal GrantIssuer. Not exposed as a model tool or agent capability."""

from __future__ import annotations

from datetime import datetime

from anycode.identity.context import DelegationGrant, ExecutionContext

from atlasos.delegation.models import DelegationRequest
from atlasos.delegation.validator import DelegationValidator


class GrantIssuer:
    """The only code path that can append a DelegationGrant to a context."""

    def __init__(self, validator: DelegationValidator | None = None) -> None:
        self._validator = validator or DelegationValidator()

    def issue(
        self,
        *,
        parent_context: ExecutionContext,
        actor: str,
        request: DelegationRequest,
        expires_at: datetime | None,
    ) -> tuple[DelegationGrant, ExecutionContext]:
        grant = DelegationGrant(
            delegator=actor,
            delegatee=request.delegatee,
            scopes=request.requested_scopes,
            expires_at=expires_at,
        )
        child_context = parent_context.model_copy(
            update={"delegation": (*parent_context.delegation, grant)}
        )
        # Revalidate the exact context that would be committed. Parent stays immutable.
        self._validator.validate_context(child_context, expected_actor=request.delegatee)
        return grant, child_context

    def issue_central(
        self,
        *,
        base_context: ExecutionContext,
        principal: str,
        target_agent: str,
        scopes: tuple[str, ...],
        expires_at: datetime | None,
    ) -> tuple[DelegationGrant, ExecutionContext]:
        """Create principal -> target authority, independent of spawn lineage."""

        if base_context.delegation:
            raise ValueError("central authorization requires a capability-free base context")
        grant = DelegationGrant(
            delegator=principal,
            delegatee=target_agent,
            scopes=scopes,
            expires_at=expires_at,
        )
        child_context = base_context.model_copy(update={"delegation": (grant,)})
        self._validator.validate_context(child_context, expected_actor=target_agent)
        return grant, child_context
