"""Durable Runtime ExecutionSuspension models.

A suspension answers *where execution stopped*: the exact assistant response, the ordered pending
invocations, and the trusted identity/resource continuity required to resume. It is Atlas
Runtime state, not approval state, and it is not M4 execution truth.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Literal

from anycode.identity.context import ExecutionContext
from anycode.types import ContentBlock, LLMMessage
from pydantic import BaseModel, ConfigDict, Field

SUSPENSION_SCHEMA_VERSION = 1
SUSPENSION_PROTOCOL_VERSION = 1
SuspensionState = Literal["ACTIVE", "SETTLING", "SETTLED", "CLEARED", "STALE"]
SuspensionPreflightOutcome = Literal["ALLOW", "DENY", "REQUIRE_HUMAN"]
SettlementOutcome = Literal[
    "EXECUTED",
    "REPLAYED",
    "DENIED",
    "REJECTED",
    "UNCERTAIN",
    "FAILED",
]


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def canonical_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def derive_suspension_id(
    *,
    run_id: str,
    response_identity: str,
    assistant_response_digest: str,
    protocol_version: int = SUSPENSION_PROTOCOL_VERSION,
) -> str:
    """Deterministic identity for one exact paused model response.

    Crash/recover/persist-if-absent therefore addresses the same suspension instead of generating
    a new UUID on every recovery.
    """

    payload = {
        "protocol_version": protocol_version,
        "run_id": run_id,
        "response_identity": response_identity,
        "assistant_response_digest": assistant_response_digest,
    }
    return canonical_digest(payload)


def assistant_message_digest(content: list[ContentBlock]) -> str:
    return canonical_digest(
        {"role": "assistant", "content": [block.model_dump(mode="json") for block in content]}
    )


def trusted_identity_digest(context: ExecutionContext) -> str:
    return canonical_digest({"execution_context": context.model_dump(mode="json")})


class PendingInvocation(BaseModel):
    """One exact tool call held before M4.

    ``canonical_input`` is execution truth and is stored only in protected Runtime state. The
    model-visible input lives in ``held_assistant_message`` and is not used to reconstruct the
    action on resume.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    ordinal: int = Field(ge=0)
    call_id: str
    tool_name: str
    logical_tool_id: str
    effective_tool_id: str
    action: str
    resource: str
    review_projection: dict[str, Any] = Field(default_factory=dict)
    dispatch_target: str | None = None
    dispatch_input: dict[str, Any] | None = None
    canonical_input: dict[str, Any]
    input_digest: str
    tool_binding_digest: str
    action_digest: str
    decision_basis_digest: str
    preflight_outcome: SuspensionPreflightOutcome
    approval_id: str | None = None
    execution_key: str | None = None
    reason_codes: tuple[str, ...] = ()
    failure_message: str | None = None


class SuspensionToolResult(BaseModel):
    """A terminal result for one held call, in original ordinal order."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ordinal: int = Field(ge=0)
    call_id: str
    tool_use_id: str
    tool_name: str
    logical_tool_id: str
    content: str
    is_error: bool
    outcome: SettlementOutcome
    action_digest: str
    decision_basis_digest: str
    approval_id: str | None = None


class SuspensionSettlement(BaseModel):
    """Durable complete ToolResult batch. Written before the suspension is cleared."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    suspension_id: str
    assistant_response_digest: str
    results: tuple[SuspensionToolResult, ...]
    result_batch_digest: str
    continuation_digest: str
    created_at: datetime


class ExecutionSuspension(BaseModel):
    """One durable paused point owned by the Atlas Runtime."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = SUSPENSION_SCHEMA_VERSION
    protocol_version: int = SUSPENSION_PROTOCOL_VERSION
    suspension_id: str
    kind: Literal["approval"] = "approval"
    resume_point: Literal["before_tool_execution"] = "before_tool_execution"

    run_id: str
    task_id: str | None = None
    worker_id: str | None = None
    profile_id: str | None = None
    agent_name: str | None = None

    assistant_response_digest: str
    model_response_id: str | None = None
    held_assistant_message: dict[str, Any]
    pending_invocations: tuple[PendingInvocation, ...]

    trusted_identity_digest: str
    response_identity: str
    execution_scope: str | None = None

    sandbox_binding_digest: str | None = None
    sandbox_required: bool = False
    wake_reference: str | None = None

    state: SuspensionState = "ACTIVE"
    revision: int = Field(default=0, ge=0)
    created_at: datetime
    updated_at: datetime

    settling_owner: str | None = None
    settling_lease_until: datetime | None = None
    settlement: SuspensionSettlement | None = None
    last_error: str | None = None

    def held_message(self) -> LLMMessage:
        return LLMMessage.model_validate(self.held_assistant_message)

    def public_projection(self) -> dict[str, Any]:
        """Summary safe to expose outside protected Runtime state (no canonical input)."""

        return {
            "suspension_id": self.suspension_id,
            "kind": self.kind,
            "resume_point": self.resume_point,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "worker_id": self.worker_id,
            "profile_id": self.profile_id,
            "state": self.state,
            "revision": self.revision,
            "assistant_response_digest": self.assistant_response_digest,
            "pending": [
                {
                    "ordinal": item.ordinal,
                    "call_id": item.call_id,
                    "tool_name": item.tool_name,
                    "effective_tool_id": item.effective_tool_id,
                    "preflight_outcome": item.preflight_outcome,
                    "approval_id": item.approval_id,
                    "action_digest": item.action_digest,
                    "decision_basis_digest": item.decision_basis_digest,
                }
                for item in self.pending_invocations
            ],
        }


class SuspensionStoreError(RuntimeError):
    pass


class UnsupportedSuspensionSchema(SuspensionStoreError):
    pass


class SuspensionStoreUnavailable(SuspensionStoreError):
    pass


class SuspensionNotFound(SuspensionStoreError):
    pass


class StaleSuspension(SuspensionStoreError):
    pass


class SuspensionConflict(SuspensionStoreError):
    pass


class SuspensionStateError(SuspensionStoreError):
    pass
