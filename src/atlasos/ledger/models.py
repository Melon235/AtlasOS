"""M4 durable action ledger models and recovery contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

LedgerState = Literal["IN_PROGRESS", "COMPLETED", "UNCERTAIN"]
CompletionKind = Literal[
    "EXECUTED_SUCCESS",
    "EXECUTED_ERROR",
    "REPLAYED",
    "CONFLICT",
    "BLOCKED_IN_PROGRESS",
    "RECONCILED_SUCCESS",
    "RECONCILED_NOT_EXECUTED",
]
InspectionStatus = Literal["absent", "unresolved", "completed"]
RecoveryState = Literal["COMPLETED", "ACTIVE", "UNCERTAIN", "RECONCILABLE"]
ReconcileStatus = Literal["CONFIRMED_SUCCESS", "CONFIRMED_NOT_EXECUTED", "STILL_UNKNOWN"]


class ActionLedgerRecord(BaseModel):
    """Minimal durable execution-lifecycle record. No raw arguments or results."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    action_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    runtime_instance_id: str = Field(min_length=1)

    run_id: str | None = None
    task_id: str | None = None
    worker_id: str | None = None
    agent: str = Field(min_length=1)

    tool_name: str = Field(min_length=1)
    canonical_action: str = Field(min_length=1)
    resource: str = Field(min_length=1)

    m3_call_id: str | None = None
    m3_request_digest: str | None = None

    request_digest: str = Field(min_length=1)
    digest_version: str = "1"
    idempotency_key_hash: str | None = None

    state: LedgerState
    claim_outcome: str | None = None
    completion_kind: CompletionKind | None = None
    result_digest: str | None = None
    retry_safe: bool | None = None

    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None


class IdempotencyInspection(BaseModel):
    """Read-only observation supplied by an upstream-safe idempotency adapter."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: InspectionStatus
    result_digest: str | None = None
    retry_safe: bool | None = None


class RecoveryClassification(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    state: RecoveryState
    completion_kind: CompletionKind | None = None
    reason: str
    retry_allowed: bool = False


class ReconcileResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: ReconcileStatus
    reason_codes: tuple[str, ...] = ()


class ActionLedgerStore(Protocol):
    def create(self, record: ActionLedgerRecord) -> None: ...
    def update(self, record: ActionLedgerRecord) -> None: ...
    def get(self, action_id: str, attempt_id: str) -> ActionLedgerRecord | None: ...
    def latest_for_key_hash(self, key_hash: str) -> ActionLedgerRecord | None: ...
    def list(self, *, state: LedgerState | None = None) -> tuple[ActionLedgerRecord, ...]: ...


class IdempotencyInspector(Protocol):
    async def inspect(self, tool_name: str, idempotency_key: str) -> IdempotencyInspection: ...


class ActionReconciler(Protocol):
    async def reconcile(self, record: ActionLedgerRecord) -> ReconcileResult: ...
