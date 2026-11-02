"""M6 approval coordinator: one durable challenge per held human decision."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from atlasos.approvals.models import (
    ApprovalChallenge,
    ApprovalCreateResult,
    ApprovalDecisionValue,
    ApprovalReceipt,
    ApprovalResolution,
    ApprovalStoreError,
    ReviewerIdentity,
    derive_approval_id,
)
from atlasos.approvals.store import SQLiteApprovalStore
from atlasos.suspension.metrics import M6Metrics
from atlasos.suspension.models import ExecutionSuspension, PendingInvocation
from atlasos.trajectory.models import TrajectoryCorrelation, canonical_digest

if TYPE_CHECKING:
    from atlasos.trajectory.recorder import TrajectoryRecorder


@dataclass(frozen=True)
class ApprovalBatchState:
    all_terminal: bool
    ready: bool
    pending: tuple[str, ...] = ()
    rejected: tuple[str, ...] = ()
    superseded: tuple[str, ...] = ()
    challenges: tuple[ApprovalChallenge, ...] = ()


class ApprovalCoordinator:
    def __init__(
        self,
        store: SQLiteApprovalStore,
        *,
        clock: Callable[[], datetime] | None = None,
        metrics: M6Metrics | None = None,
        trajectory_recorder: TrajectoryRecorder | None = None,
    ) -> None:
        self.store = store
        self._clock = clock or (lambda: datetime.now(UTC))
        self.metrics = metrics or M6Metrics()
        self.trajectory_recorder = trajectory_recorder

    def now(self) -> datetime:
        return self._clock()

    def ensure_for_suspension(
        self, suspension: ExecutionSuspension
    ) -> tuple[ApprovalChallenge, ...]:
        """Create missing PENDING challenges; safe to repeat after a crash.

        This is the M6 "suspension exists, approval missing" reconciliation path. It derives the
        deterministic approval id from durable suspension facts and never reconstructs execution
        from the approval store.
        """

        created: list[ApprovalChallenge] = []
        for invocation in suspension.pending_invocations:
            if invocation.preflight_outcome != "REQUIRE_HUMAN":
                continue
            if invocation.approval_id is None:
                raise ApprovalStoreError(
                    f"REQUIRE_HUMAN invocation {invocation.call_id!r} has no approval id"
                )
            expected = derive_approval_id(
                suspension_id=suspension.suspension_id,
                call_id=invocation.call_id,
                action_digest=invocation.action_digest,
                decision_basis_digest=invocation.decision_basis_digest,
            )
            if invocation.approval_id != expected:
                raise ApprovalStoreError(
                    "approval id does not match deterministic suspension identity"
                )
            challenge = ApprovalChallenge(
                approval_id=invocation.approval_id,
                suspension_id=suspension.suspension_id,
                run_id=suspension.run_id,
                task_id=suspension.task_id,
                worker_id=suspension.worker_id,
                profile_id=suspension.profile_id,
                call_id=invocation.call_id,
                ordinal=invocation.ordinal,
                action_digest=invocation.action_digest,
                decision_basis_digest=invocation.decision_basis_digest,
                tool_name=invocation.tool_name,
                trusted_review_projection=dict(invocation.review_projection),
                created_at=self.now(),
            )
            result = self.store.create_if_absent(challenge)
            self._count_create(result)
            created.append(result.challenge)
        return tuple(created)

    def _count_create(self, result: ApprovalCreateResult) -> None:
        if result.created:
            self.metrics.approval_challenges_created += 1
        else:
            self.metrics.approval_create_dedupes += 1
        if result.challenge.state == "APPROVED":
            self.metrics.approval_approved += 1
        elif result.challenge.state == "REJECTED":
            self.metrics.approval_rejected += 1

    def batch_state(self, suspension: ExecutionSuspension) -> ApprovalBatchState:
        required = tuple(
            invocation
            for invocation in suspension.pending_invocations
            if invocation.preflight_outcome == "REQUIRE_HUMAN"
        )
        if not required:
            return ApprovalBatchState(all_terminal=True, ready=True)
        challenges = {
            challenge.call_id: challenge
            for challenge in self.store.list_for_suspension(suspension.suspension_id)
        }
        pending: list[str] = []
        rejected: list[str] = []
        superseded: list[str] = []
        selected: list[ApprovalChallenge] = []
        for invocation in required:
            challenge = challenges.get(invocation.call_id)
            if challenge is None:
                pending.append(invocation.call_id)
                continue
            selected.append(challenge)
            if challenge.state == "PENDING":
                pending.append(invocation.call_id)
            elif challenge.state == "REJECTED":
                rejected.append(invocation.call_id)
            elif challenge.state == "SUPERSEDED":
                superseded.append(invocation.call_id)
        all_terminal = not pending and not superseded and len(selected) == len(required)
        return ApprovalBatchState(
            all_terminal=all_terminal,
            ready=all_terminal,
            pending=tuple(pending),
            rejected=tuple(rejected),
            superseded=tuple(superseded),
            challenges=tuple(selected),
        )

    def receipt_for(
        self,
        suspension: ExecutionSuspension,
        invocation: PendingInvocation,
        *,
        current_action_digest: str,
        current_decision_basis_digest: str,
    ) -> ApprovalReceipt | None:
        """Return a receipt only when it matches the *current* action and security basis."""

        if invocation.approval_id is None:
            return None
        challenge = self.store.get(invocation.approval_id)
        if challenge is None:
            return None
        if challenge.suspension_id != suspension.suspension_id:
            return None
        if challenge.call_id != invocation.call_id:
            return None
        if challenge.action_digest != current_action_digest:
            return None
        if challenge.decision_basis_digest != current_decision_basis_digest:
            return None
        return ApprovalReceipt.from_challenge(challenge)

    def resolve(
        self,
        approval_id: str,
        *,
        expected_version: int,
        decision: ApprovalDecisionValue,
        reviewer: ReviewerIdentity,
        reason: str | None = None,
    ) -> ApprovalResolution:
        resolution = self.store.resolve(
            approval_id,
            expected_version,
            decision,
            reviewer,
            reason,
        )
        if resolution.status == "resolved":
            if decision == "APPROVED":
                self.metrics.approval_approved += 1
            else:
                self.metrics.approval_rejected += 1
            self._record_resolution(resolution)
        elif resolution.status in ("version_conflict", "already_resolved"):
            self.metrics.approval_resolution_conflicts += 1
        return resolution

    def _record_resolution(self, resolution: ApprovalResolution) -> None:
        recorder = self.trajectory_recorder
        challenge = resolution.challenge
        if recorder is None or challenge is None or challenge.actor is None:
            return
        try:  # noqa: SIM105
            recorder.record_approval_resolved(
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
            )
        except Exception:  # noqa: BLE001 - evidence is optional
            pass

    def list_orphans(self, suspension_lookup: object) -> tuple[ApprovalChallenge, ...]:
        """Approvals whose suspension is missing are audit facts, never executable.

        ``suspension_lookup`` is a ``SQLiteSuspensionStore``-compatible object with
        ``get(suspension_id)``.
        """

        orphans: list[ApprovalChallenge] = []
        for challenge in self.store.list_all():
            suspension = suspension_lookup.get(challenge.suspension_id)  # type: ignore[attr-defined]
            if suspension is None:
                orphans.append(challenge)
        self.metrics.orphan_approvals = len(orphans)
        return tuple(orphans)

    def supersede(
        self,
        approval_id: str,
        *,
        expected_version: int,
        reviewer: ReviewerIdentity,
        reason: str,
    ) -> ApprovalResolution:
        resolution = self.store.supersede(approval_id, expected_version, reviewer, reason)
        if resolution.status == "resolved":
            self.metrics.approval_superseded += 1
        elif resolution.status in ("version_conflict", "already_resolved"):
            self.metrics.approval_resolution_conflicts += 1
        return resolution


@dataclass
class ApprovalBatchResolution:
    """Convenience container used by the resume path."""

    state: ApprovalBatchState
    receipts: dict[str, ApprovalReceipt] = field(default_factory=dict)
