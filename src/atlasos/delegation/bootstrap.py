"""Trusted-host bootstrap for the root delegation grant.

This helper is called by trusted host/run configuration, never by an LLM or agent tool. M2 does
not implement authentication; the host is the source of principal, root scopes, and initial
agent identity.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from anycode.identity.context import ArtifactClassification, DelegationGrant, ExecutionContext

from atlasos.delegation.capabilities import canonical_actor, canonicalize_scopes


@dataclass(frozen=True)
class TrustedRootIdentity:
    principal: str
    main_agent: str
    scopes: tuple[str, ...]
    expires_at: datetime | None = None

    subject: str | None = None
    workload_identity: str | None = None
    tenant_scope: str = "default"
    classification: ArtifactClassification = "internal"
    allowed_regions: tuple[str, ...] = ()
    required_region: str | None = None
    credential_references: tuple[str, ...] = ()
    trace_id: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)


def bootstrap_root_context(identity: TrustedRootIdentity) -> ExecutionContext:
    """Create the trusted root context from host configuration."""

    if not identity.principal.strip():
        raise ValueError("trusted principal must be non-empty")
    actor = canonical_actor(identity.main_agent)
    scopes = canonicalize_scopes(identity.scopes)
    grant = DelegationGrant(
        delegator=identity.principal,
        delegatee=actor,
        scopes=scopes,
        expires_at=identity.expires_at,
    )
    return ExecutionContext(
        principal=identity.principal,
        subject=identity.subject,
        workload_identity=identity.workload_identity,
        tenant_scope=identity.tenant_scope,
        delegation=(grant,),
        classification=identity.classification,
        allowed_regions=identity.allowed_regions,
        required_region=identity.required_region,
        credential_references=identity.credential_references,
        trace_id=identity.trace_id,
        attributes=dict(identity.attributes),
    )
