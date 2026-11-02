"""Evidence-only reconciliation from durable M4/M6 authority state.

This module reads authority and appends missing trajectory evidence. It has no execution,
authorization, approval, resume, sandbox, or LLM path.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from atlasos.trajectory.models import (
    TrajectoryConflict,
    TrajectoryCorrelation,
    TrajectoryGap,
    TrajectoryView,
    canonical_digest,
)
from atlasos.trajectory.reader import TrajectoryReader
from atlasos.trajectory.recorder import TrajectoryRecorder

ConsistencyStatus = Literal["PASS", "FAIL", "UNKNOWN"]


class ConsistencyCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: str
    status: ConsistencyStatus
    event_id: str | None = None
    authority_ref: str | None = None
    detail: str = ""


class ConsistencyReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    checks: int = 0
    passes: int = 0
    failures: int = 0
    unknown: int = 0
    details: tuple[ConsistencyCheck, ...] = ()


class ReconcileReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    appended_event_ids: tuple[str, ...] = ()
    appended_count: int = 0
    gaps: tuple[TrajectoryGap, ...] = ()
    conflicts: tuple[TrajectoryConflict, ...] = ()
    consistency: ConsistencyReport = Field(default_factory=ConsistencyReport)
    recording_failures: int = 0
    last_error: str | None = None


class TrajectoryReconciler:
    def __init__(
        self,
        *,
        reader: TrajectoryReader,
        recorder: TrajectoryRecorder,
        ledger: Any | None = None,
        approvals: Any | None = None,
        suspensions: Any | None = None,
    ) -> None:
        self.reader = reader
        self.recorder = recorder
        self.ledger = ledger
        self.approvals = approvals
        self.suspensions = suspensions

    def reconcile(self, run_id: str) -> ReconcileReport:
        view = self.reader.read(run_id)
        existing = {event.event_id for event in view.events}
        existing.update(conflict.event_id for conflict in view.conflicts)
        appended: list[str] = []

        ledger_records = self._ledger_records(run_id)
        for record in ledger_records:
            event_id = self._execution_event_id(run_id, record)
            if event_id in existing:
                continue
            envelope = self.recorder.record_execution_outcome(
                action_id=record.action_id,
                attempt_id=record.attempt_id,
                tool_id=record.tool_name,
                canonical_action=record.canonical_action,
                state=record.state,
                completion_kind=record.completion_kind,
                claim_outcome=record.claim_outcome,
                retry_safe=record.retry_safe,
                resource_digest=canonical_digest(record.resource),
                result_digest=record.result_digest,
                result_size=None,
                occurred_at=record.completed_at or record.updated_at,
                correlation=TrajectoryCorrelation(
                    tool_id=record.tool_name,
                    action_id=record.action_id,
                    attempt_id=record.attempt_id,
                    call_id=record.m3_call_id,
                ),
                backfilled=True,
            )
            if envelope is not None:
                appended.append(envelope.event_id)
                existing.add(envelope.event_id)

        for challenge in self._approval_challenges(run_id):
            if challenge.state not in ("APPROVED", "REJECTED"):
                continue
            event_id = self._approval_event_id(run_id, challenge)
            if event_id in existing:
                continue
            envelope = self.recorder.record_approval_resolved(
                approval_id=challenge.approval_id,
                suspension_id=challenge.suspension_id,
                call_id=challenge.call_id,
                decision=challenge.state,
                version=challenge.version,
                reviewer=challenge.actor,
                reason_digest=(canonical_digest(challenge.reason) if challenge.reason else None),
                occurred_at=challenge.resolved_at,
                correlation=TrajectoryCorrelation(
                    approval_id=challenge.approval_id,
                    suspension_id=challenge.suspension_id,
                    call_id=challenge.call_id,
                    tool_id=challenge.tool_name,
                ),
                backfilled=True,
            )
            if envelope is not None:
                appended.append(envelope.event_id)
                existing.add(envelope.event_id)

        for suspension in self._suspensions(run_id):
            created_id = self._suspension_created_event_id(run_id, suspension)
            if created_id not in existing:
                created = self.recorder.record_suspension_created(
                    suspension_id=suspension.suspension_id,
                    assistant_response_digest=suspension.assistant_response_digest,
                    execution_scope=suspension.execution_scope,
                    pending=tuple(
                        self._pending_ref(item) for item in suspension.pending_invocations
                    ),
                    occurred_at=suspension.created_at,
                    correlation=TrajectoryCorrelation(
                        suspension_id=suspension.suspension_id,
                        model_response_id=getattr(suspension, "model_response_id", None),
                        worker_id=suspension.worker_id,
                        profile_id=suspension.profile_id,
                    ),
                    backfilled=True,
                )
                if created is not None:
                    appended.append(created.event_id)
                    existing.add(created.event_id)
            if suspension.state == "CLEARED" and suspension.settlement is not None:
                cleared_id = self._suspension_cleared_event_id(run_id, suspension)
                if cleared_id in existing:
                    continue
                cleared = self.recorder.record_suspension_cleared(
                    suspension_id=suspension.suspension_id,
                    revision=suspension.revision,
                    result_batch_digest=suspension.settlement.result_batch_digest,
                    result_count=len(suspension.settlement.results),
                    occurred_at=suspension.updated_at,
                    correlation=TrajectoryCorrelation(
                        suspension_id=suspension.suspension_id,
                        model_response_id=getattr(suspension, "model_response_id", None),
                    ),
                    backfilled=True,
                )
                if cleared is not None:
                    appended.append(cleared.event_id)
                    existing.add(cleared.event_id)

        view_after = self.reader.read(run_id)
        gaps = list(view_after.gaps)
        started = {
            event.correlation.suspension_id
            for event in view_after.events
            if event.event_type == "resume_started"
        }
        for suspension in self._suspensions(run_id):
            if (
                suspension.state in ("SETTLED", "CLEARED")
                and suspension.suspension_id not in started
            ):
                gaps.append(
                    TrajectoryGap(
                        kind="resume_started_unreconstructable",
                        event_type="resume_started",
                        suspension_id=suspension.suspension_id,
                        detail="no durable resume-start timestamp exists for this "
                        "suspension; trajectory does not invent one",
                    )
                )
        consistency = self._consistency(view_after)
        return ReconcileReport(
            run_id=run_id,
            appended_event_ids=tuple(appended),
            appended_count=len(appended),
            gaps=tuple(gaps),
            conflicts=view_after.conflicts,
            consistency=consistency,
            recording_failures=self.recorder.recording_failures,
            last_error=self.recorder.last_error,
        )

    @staticmethod
    def _pending_ref(invocation: Any) -> Any:
        from atlasos.trajectory.models import SuspensionPendingRef

        return SuspensionPendingRef(
            ordinal=invocation.ordinal,
            call_id=invocation.call_id,
            tool_id=invocation.effective_tool_id,
            preflight_outcome=invocation.preflight_outcome,
            approval_id=invocation.approval_id,
        )

    def _ledger_records(self, run_id: str) -> list[Any]:
        if self.ledger is None:
            return []
        return [
            record for record in self.ledger.list() if getattr(record, "run_id", None) == run_id
        ]

    def _approval_challenges(self, run_id: str) -> list[Any]:
        if self.approvals is None:
            return []
        return [challenge for challenge in self.approvals.list_all() if challenge.run_id == run_id]

    def _suspensions(self, run_id: str) -> list[Any]:
        if self.suspensions is None:
            return []
        return list(self.suspensions.list_for_run(run_id))

    @staticmethod
    def _execution_event_id(run_id: str, record: Any) -> str:
        from atlasos.trajectory.models import derive_event_id

        return derive_event_id(
            event_type="execution_outcome",
            run_id=run_id,
            identity={
                "action_id": record.action_id,
                "attempt_id": record.attempt_id,
                "state": record.state,
                "completion_kind": record.completion_kind,
                "result_digest": record.result_digest,
            },
        )

    @staticmethod
    def _approval_event_id(run_id: str, challenge: Any) -> str:
        from atlasos.trajectory.models import derive_event_id

        return derive_event_id(
            event_type="approval_resolved",
            run_id=run_id,
            identity={
                "approval_id": challenge.approval_id,
                "version": challenge.version,
            },
        )

    @staticmethod
    def _suspension_created_event_id(run_id: str, suspension: Any) -> str:
        from atlasos.trajectory.models import derive_event_id

        return derive_event_id(
            event_type="suspension_created",
            run_id=run_id,
            identity={
                "suspension_id": suspension.suspension_id,
                "transition": "created",
            },
        )

    @staticmethod
    def _suspension_cleared_event_id(run_id: str, suspension: Any) -> str:
        from atlasos.trajectory.models import derive_event_id

        return derive_event_id(
            event_type="suspension_cleared",
            run_id=run_id,
            identity={
                "suspension_id": suspension.suspension_id,
                "revision": suspension.revision,
                "transition": "cleared",
            },
        )

    def _consistency(self, view: TrajectoryView) -> ConsistencyReport:
        checks: list[ConsistencyCheck] = []
        ledger = {
            (record.action_id, record.attempt_id): record
            for record in (self.ledger.list() if self.ledger is not None else [])
        }
        approvals = {
            challenge.approval_id: challenge
            for challenge in (self.approvals.list_all() if self.approvals is not None else [])
        }
        suspensions = {
            suspension.suspension_id: suspension
            for suspension in (
                self.suspensions.list_for_run(view.run_id) if self.suspensions is not None else []
            )
        }
        for event in view.events:
            if event.event_type == "execution_outcome":
                key = (
                    event.correlation.action_id,
                    event.correlation.attempt_id,
                )
                record = ledger.get(key)
                if record is None:
                    checks.append(
                        ConsistencyCheck(
                            kind="execution_outcome",
                            status="UNKNOWN",
                            event_id=event.event_id,
                            authority_ref=event.correlation.action_id,
                            detail="M4 ledger record is unavailable",
                        )
                    )
                    continue
                data = event.data
                matches = (
                    record.state == data.get("state")
                    and record.completion_kind == data.get("completion_kind")
                    and record.result_digest == data.get("result_digest")
                )
                checks.append(
                    ConsistencyCheck(
                        kind="execution_outcome",
                        status="PASS" if matches else "FAIL",
                        event_id=event.event_id,
                        authority_ref=event.correlation.action_id,
                        detail="" if matches else "trajectory differs from M4 ledger",
                    )
                )
            elif event.event_type == "approval_resolved":
                challenge = approvals.get(event.correlation.approval_id or "")
                if challenge is None:
                    checks.append(
                        ConsistencyCheck(
                            kind="approval_resolved",
                            status="UNKNOWN",
                            event_id=event.event_id,
                            authority_ref=event.correlation.approval_id,
                            detail="approval store record is unavailable",
                        )
                    )
                    continue
                expected = event.data.get("decision")
                matches = challenge.state == expected and challenge.version == event.data.get(
                    "version"
                )
                checks.append(
                    ConsistencyCheck(
                        kind="approval_resolved",
                        status="PASS" if matches else "FAIL",
                        event_id=event.event_id,
                        authority_ref=event.correlation.approval_id,
                        detail="" if matches else "trajectory differs from ApprovalStore",
                    )
                )
            elif event.event_type == "suspension_cleared":
                suspension = suspensions.get(event.correlation.suspension_id or "")
                if suspension is None:
                    checks.append(
                        ConsistencyCheck(
                            kind="suspension_cleared",
                            status="UNKNOWN",
                            event_id=event.event_id,
                            authority_ref=event.correlation.suspension_id,
                            detail="Runtime suspension record is unavailable",
                        )
                    )
                    continue
                matches = suspension.state == "CLEARED" and suspension.revision == event.data.get(
                    "revision"
                )
                checks.append(
                    ConsistencyCheck(
                        kind="suspension_cleared",
                        status="PASS" if matches else "FAIL",
                        event_id=event.event_id,
                        authority_ref=event.correlation.suspension_id,
                        detail="" if matches else "trajectory differs from Runtime state",
                    )
                )
            elif event.event_type == "suspension_created":
                suspension = suspensions.get(event.correlation.suspension_id or "")
                if suspension is None:
                    checks.append(
                        ConsistencyCheck(
                            kind="suspension_created",
                            status="UNKNOWN",
                            event_id=event.event_id,
                            authority_ref=event.correlation.suspension_id,
                            detail="Runtime suspension record is unavailable",
                        )
                    )
                    continue
                matches = suspension.assistant_response_digest == event.data.get(
                    "assistant_response_digest"
                )
                checks.append(
                    ConsistencyCheck(
                        kind="suspension_created",
                        status="PASS" if matches else "FAIL",
                        event_id=event.event_id,
                        authority_ref=event.correlation.suspension_id,
                        detail="" if matches else "trajectory differs from Runtime state",
                    )
                )
        failures = sum(1 for check in checks if check.status == "FAIL")
        unknown = sum(1 for check in checks if check.status == "UNKNOWN")
        passes = sum(1 for check in checks if check.status == "PASS")
        return ConsistencyReport(
            checks=len(checks),
            passes=passes,
            failures=failures,
            unknown=unknown,
            details=tuple(checks),
        )
