"""M6 durable human-decision models.

Approval state is human decision truth only. It is deliberately separate from the Atlas Runtime
``ExecutionSuspension`` (where the run stopped) and from M4 (whether the action executed).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

APPROVAL_PROTOCOL_VERSION = 1
ApprovalState = Literal["PENDING", "APPROVED", "REJECTED", "SUPERSEDED"]
ApprovalDecisionValue = Literal["APPROVED", "REJECTED"]
ReviewerSource = Literal["host", "cli", "console"]


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def derive_approval_id(
    *,
    suspension_id: str,
    call_id: str,
    action_digest: str,
    decision_basis_digest: str,
    protocol_version: int = APPROVAL_PROTOCOL_VERSION,
) -> str:
    """Deterministic identity for one exact pending human decision."""

    payload = {
        "protocol_version": protocol_version,
        "suspension_id": suspension_id,
        "call_id": call_id,
        "action_digest": action_digest,
        "decision_basis_digest": decision_basis_digest,
    }
    return sha256_hex(canonical_json(payload))


class ReviewerIdentity(BaseModel):
    """Trusted host-authenticated identity of a human reviewer.

    There is no model-facing construction path: no approval tool is registered, and the store
    rejects agent/model namespaces as a defense-in-depth check.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    actor: str = Field(min_length=1, max_length=200)
    source: ReviewerSource = "host"

    @field_validator("actor")
    @classmethod
    def _not_model_actor(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not normalized:
            raise ValueError("reviewer actor must not be empty")
        if normalized.startswith(("agent:", "model:", "llm:", "assistant:")):
            raise ValueError("model/tool identities cannot resolve human approvals")
        return value

    def audit_label(self) -> str:
        return f"{self.source}:{self.actor}"


class ApprovalChallenge(BaseModel):
    """One exact human decision required before a suspended invocation may execute."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_version: int = APPROVAL_PROTOCOL_VERSION
    approval_id: str
    suspension_id: str

    run_id: str
    task_id: str | None = None
    worker_id: str | None = None
    profile_id: str | None = None
    call_id: str
    ordinal: int = Field(ge=0)

    action_digest: str
    decision_basis_digest: str
    tool_name: str

    # Redacted, trusted renderer projection. Exact/sensitive input stays in protected Runtime
    # state and is never copied into the approval database.
    trusted_review_projection: dict[str, Any] = Field(default_factory=dict)

    state: ApprovalState = "PENDING"
    version: int = Field(default=0, ge=0)
    created_at: datetime
    resolved_at: datetime | None = None
    actor: str | None = None
    reason: str | None = None


class ApprovalDecision(BaseModel):
    """Durable terminal human decision; never an execution record."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    approval_id: str
    suspension_id: str
    call_id: str
    decision: ApprovalDecisionValue
    action_digest: str
    decision_basis_digest: str
    reviewer: str
    reason: str | None = None
    decided_at: datetime
    version: int


class ApprovalReceipt(BaseModel):
    """Revalidatable proof that a human satisfied one specific REQUIRE_HUMAN gate."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    approval_id: str
    suspension_id: str
    call_id: str
    action_digest: str
    decision_basis_digest: str
    reviewer: str
    decision: ApprovalDecisionValue
    reason: str | None = None

    @classmethod
    def from_challenge(
        cls,
        challenge: ApprovalChallenge,
        *,
        reason: str | None = None,
    ) -> ApprovalReceipt | None:
        if challenge.state not in ("APPROVED", "REJECTED"):
            return None
        if challenge.actor is None or challenge.resolved_at is None:
            return None
        return cls(
            approval_id=challenge.approval_id,
            suspension_id=challenge.suspension_id,
            call_id=challenge.call_id,
            action_digest=challenge.action_digest,
            decision_basis_digest=challenge.decision_basis_digest,
            reviewer=challenge.actor,
            decision=challenge.state,
            reason=reason if reason is not None else challenge.reason,
        )


class ApprovalCreateResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    challenge: ApprovalChallenge
    created: bool


class ApprovalResolution(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["resolved", "already_resolved", "version_conflict", "not_found"]
    challenge: ApprovalChallenge | None = None
    decision: ApprovalDecision | None = None


class ApprovalStoreError(RuntimeError):
    pass


class ApprovalIdentityConflict(ApprovalStoreError):
    pass


class ApprovalStoreUnavailable(ApprovalStoreError):
    pass
