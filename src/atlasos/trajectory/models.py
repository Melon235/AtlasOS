"""M7 durable agent-trajectory schema.

Trajectory events are immutable evidence. They are never execution truth, never approval truth,
and never runtime authority.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

TRAJECTORY_SCHEMA_VERSION = 1
TRAJECTORY_PROTOCOL_VERSION = 1

EventType = Literal[
    "action_proposed",
    "tool_disclosed",
    "worker_lifecycle",
    "action_decision",
    "execution_outcome",
    "tool_observation",
    "sandbox_lifecycle",
    "suspension_created",
    "approval_resolved",
    "resume_started",
    "suspension_cleared",
    "run_terminal",
    "user_message",
    "model_message",
]
Source = Literal["model", "m1", "m2", "m3", "m4", "m5", "m6", "runtime", "anycode"]
DecisionPhase = Literal["preflight", "execution", "resume_revalidation"]
DecisionValue = Literal["ALLOW", "DENY", "REQUIRE_HUMAN"]
ObservationOutcome = Literal[
    "EXECUTED",
    "REPLAYED",
    "DENIED",
    "REJECTED",
    "UNCERTAIN",
    "FAILED",
]


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonical_digest(value: Any) -> str:
    return sha256_hex(canonical_json(value))


def derive_event_id(*, event_type: str, run_id: str, identity: dict[str, Any]) -> str:
    """Deterministic logical identity for one immutable semantic transition."""

    return canonical_digest(
        {
            "protocol_version": TRAJECTORY_PROTOCOL_VERSION,
            "run_id": run_id,
            "event_type": event_type,
            "identity": identity,
        }
    )


def derive_model_response_id(
    *,
    run_id: str,
    turn_identity: str,
    assistant_response_digest: str,
) -> str:
    """Fallback model-response identity for providers without a stable response id."""

    return canonical_digest(
        {
            "protocol_version": TRAJECTORY_PROTOCOL_VERSION,
            "run_id": run_id,
            "turn_identity": turn_identity,
            "assistant_response_digest": assistant_response_digest,
        }
    )


def derive_sandbox_binding_id(
    *,
    run_id: str,
    task_id: str,
    worker_id: str | None,
    provider_name: str,
    sandbox_handle_id: str,
) -> str:
    return canonical_digest(
        {
            "run_id": run_id,
            "task_id": task_id,
            "worker_id": worker_id,
            "provider_name": provider_name,
            "sandbox_handle_id": sandbox_handle_id,
        }
    )


class TrajectoryCorrelation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    worker_id: str | None = None
    profile_id: str | None = None
    model_response_id: str | None = None
    occurrence_id: str | None = None
    ordinal: int | None = Field(default=None, ge=0)
    call_id: str | None = None
    tool_id: str | None = None
    action_id: str | None = None
    attempt_id: str | None = None
    sandbox_binding_id: str | None = None
    suspension_id: str | None = None
    approval_id: str | None = None


class TrajectoryEnvelope(BaseModel):
    """Namespaced payload stored in a ``TranscriptEvent(kind='lifecycle')``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = TRAJECTORY_SCHEMA_VERSION
    protocol_version: int = TRAJECTORY_PROTOCOL_VERSION
    event_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    event_type: EventType
    source: Source
    occurred_at: datetime | None = None
    backfilled: bool = False
    correlation: TrajectoryCorrelation = Field(default_factory=TrajectoryCorrelation)
    authority_ref: str | None = None
    authority_revision: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class ActionProposedData(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    model_response_id: str
    ordinal: int = Field(ge=0)
    call_id: str
    tool_id: str
    tool_name: str
    input_digest: str
    input_shape: str | None = None
    input_size: int | None = Field(default=None, ge=0)


class ToolDisclosedData(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_id: str
    tool_name: str
    disclosure_source: str = "tool_search"
    query_digest: str | None = None


class WorkerLifecycleData(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    worker_id: str
    profile_id: str
    parent_agent: str | None = None
    mode: Literal["create", "restore"]
    context_hash: str


class ActionDecisionData(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    phase: DecisionPhase
    decision: DecisionValue
    decision_source: Literal[
        "policy",
        "reviewer",
        "external_policy",
        "hard_security",
        "input_validation",
        "registry",
        "identity",
        "unknown",
    ]
    tool_id: str
    call_id: str | None = None
    policy_rule_id: str | None = None
    decision_basis_digest: str
    action_digest: str
    input_digest: str
    tool_binding_digest: str
    reason_codes: tuple[str, ...] = ()
    scope_alignment: str | None = None
    reviewer_verdict: str | None = None
    external_policy_outcome: str | None = None


class ExecutionOutcomeData(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    action_id: str
    attempt_id: str
    tool_id: str
    canonical_action: str
    state: Literal["IN_PROGRESS", "COMPLETED", "UNCERTAIN"]
    completion_kind: str | None = None
    claim_outcome: str | None = None
    retry_safe: bool | None = None
    resource_digest: str | None = None
    result_digest: str | None = None
    result_size: int | None = Field(default=None, ge=0)


class ToolObservationData(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    call_id: str
    tool_id: str
    is_error: bool
    result_digest: str
    result_size: int = Field(ge=0)
    outcome: ObservationOutcome | None = None
    transcript_seq_ref: int | None = None


class SandboxLifecycleData(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    binding_id: str
    task_id: str
    worker_id: str | None = None
    provider_name: str
    state: Literal["READY", "LOST", "CLOSED"]
    source_commit: str | None = None
    sandbox_id: str | None = None
    capabilities_digest: str | None = None
    capabilities: dict[str, Any] | None = None
    network: str | None = None


class SuspensionPendingRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ordinal: int = Field(ge=0)
    call_id: str
    tool_id: str
    preflight_outcome: DecisionValue
    approval_id: str | None = None


class SuspensionCreatedData(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    suspension_id: str
    kind: Literal["approval"]
    resume_point: Literal["before_tool_execution"]
    assistant_response_digest: str
    execution_scope: str | None = None
    pending: tuple[SuspensionPendingRef, ...] = ()


class ApprovalResolvedData(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    approval_id: str
    suspension_id: str
    call_id: str
    decision: Literal["APPROVED", "REJECTED"]
    version: int = Field(ge=0)
    reviewer: str | None = None
    reason_digest: str | None = None


class ResumeStartedData(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    suspension_id: str
    revision: int = Field(ge=0)
    owner: str
    previous_state: str = "ACTIVE"


class SuspensionClearedData(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    suspension_id: str
    revision: int = Field(ge=0)
    result_batch_digest: str | None = None
    result_count: int = Field(default=0, ge=0)


class RunTerminalData(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: str
    task_count: int = Field(default=0, ge=0)
    final_output_digest: str | None = None
    final_output_size: int | None = Field(default=None, ge=0)


class MessageProjectionData(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    role: str
    content_digest: str
    content_size: int = Field(ge=0)
    transcript_seq_ref: int


DATA_MODELS: dict[str, type[BaseModel]] = {
    "action_proposed": ActionProposedData,
    "tool_disclosed": ToolDisclosedData,
    "worker_lifecycle": WorkerLifecycleData,
    "action_decision": ActionDecisionData,
    "execution_outcome": ExecutionOutcomeData,
    "tool_observation": ToolObservationData,
    "sandbox_lifecycle": SandboxLifecycleData,
    "suspension_created": SuspensionCreatedData,
    "approval_resolved": ApprovalResolvedData,
    "resume_started": ResumeStartedData,
    "suspension_cleared": SuspensionClearedData,
    "run_terminal": RunTerminalData,
    "user_message": MessageProjectionData,
    "model_message": MessageProjectionData,
}


def validate_data(envelope: TrajectoryEnvelope) -> TrajectoryEnvelope:
    model = DATA_MODELS.get(envelope.event_type)
    if model is None:
        raise ValueError(f"unknown trajectory event type {envelope.event_type!r}")
    model.model_validate(envelope.data)
    return envelope


class TrajectoryEvent(BaseModel):
    """Read-only logical event selected from the durable transcript."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str
    run_id: str
    event_type: EventType
    source: Source
    occurred_at: datetime | None = None
    recorded_at: datetime
    seq: int
    backfilled: bool = False
    correlation: TrajectoryCorrelation
    data: dict[str, Any]
    authority_ref: str | None = None
    authority_revision: str | None = None
    physical_refs: tuple[int, ...] = ()


class TrajectoryGap(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: str
    event_type: str | None = None
    call_id: str | None = None
    suspension_id: str | None = None
    detail: str = ""


class TrajectoryConflict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str
    seqs: tuple[int, ...]
    reason: str = "same event_id with different semantic payload"
    payload_digests: tuple[str, ...] = ()


class TrajectoryDecodeFailure(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    seq: int
    reason: str


class TrajectoryMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    physical_events: int = 0
    logical_events: int = 0
    backfilled_events: int = 0
    evidence_gaps: int = 0
    evidence_conflicts: int = 0
    decode_failures: int = 0
    action_proposed_count: int = 0
    action_decision_count: int = 0
    execution_outcome_count: int = 0
    approval_event_count: int = 0
    suspension_event_count: int = 0
    native_tool_observations_duplicated: int = 0


class TrajectoryView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    events: tuple[TrajectoryEvent, ...]
    gaps: tuple[TrajectoryGap, ...] = ()
    conflicts: tuple[TrajectoryConflict, ...] = ()
    decode_failures: tuple[TrajectoryDecodeFailure, ...] = ()
    pending_suspensions: tuple[str, ...] = ()
    metrics: TrajectoryMetrics = Field(default_factory=TrajectoryMetrics)
