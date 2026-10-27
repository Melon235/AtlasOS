"""Trusted, durable run authority ceiling.

The ceiling is the maximum capability envelope for a run. It is not an agent's authority and is
never derived from an agent's current scopes.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from anycode.identity.context import ArtifactClassification, ExecutionContext
from pydantic import BaseModel, ConfigDict, Field, field_validator

from atlasos.delegation.capabilities import canonicalize_scopes


class RunAuthority(BaseModel):
    """Immutable trusted ceiling for one run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    principal: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    allowed_scopes: tuple[str, ...]
    expires_at: datetime | None = None

    @field_validator("allowed_scopes", mode="before")
    @classmethod
    def _canonicalize(cls, value: Any) -> tuple[str, ...]:
        return canonicalize_scopes(value)


def run_authority_hash(authority: RunAuthority) -> str:
    payload = json.dumps(
        authority.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# Explicit alias matching the architecture vocabulary.
RunAuthorityCeiling = RunAuthority


class RunAuthorityStore:
    """Atomic durable sidecar for the trusted run authority ceiling."""

    def __init__(self, path: str | Path, *, run_id: str) -> None:
        self.path = Path(path)
        self.run_id = run_id

    def load(self) -> RunAuthority | None:
        if not self.path.exists():
            return None
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"cannot read run authority {self.path}: {error}") from error
        if not isinstance(payload, dict):
            raise ValueError("run authority snapshot must be a JSON object")
        if payload.get("run_id") != self.run_id:
            raise ValueError(
                f"run authority belongs to run {payload.get('run_id')!r}, not {self.run_id!r}"
            )
        try:
            return RunAuthority.model_validate(payload)
        except Exception as error:
            raise ValueError(f"invalid run authority snapshot: {error}") from error

    def save(self, authority: RunAuthority) -> None:
        if authority.run_id != self.run_id:
            raise ValueError("run authority run_id does not match store")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(authority.model_dump(mode="json"), sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(temporary, self.path)


@dataclass(frozen=True)
class TrustedRunIdentity:
    """Host-supplied run identity and ceiling. Not model input."""

    principal: str
    run_id: str
    main_agent: str
    allowed_scopes: tuple[str, ...]
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


def bootstrap_run_authority(
    identity: TrustedRunIdentity,
) -> tuple[RunAuthority, ExecutionContext]:
    """Create the trusted ceiling and a capability-free base context.

    The main agent receives no operational scopes here. It must call
    ``AuthorizationRuntime.authorize_agent`` like every other agent.
    """

    authority = RunAuthority(
        principal=identity.principal,
        run_id=identity.run_id,
        allowed_scopes=identity.allowed_scopes,
        expires_at=identity.expires_at,
    )
    base_context = ExecutionContext(
        principal=identity.principal,
        subject=identity.subject,
        workload_identity=identity.workload_identity,
        tenant_scope=identity.tenant_scope,
        delegation=(),
        classification=identity.classification,
        allowed_regions=identity.allowed_regions,
        required_region=identity.required_region,
        credential_references=identity.credential_references,
        trace_id=identity.trace_id,
        attributes=dict(identity.attributes),
    )
    return authority, base_context
