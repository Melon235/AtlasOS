"""Recovery classification and optional reconciliation; never executes tools."""

from __future__ import annotations

from atlasos.ledger.models import (
    ActionLedgerRecord,
    ActionLedgerStore,
    ActionReconciler,
    IdempotencyInspector,
    RecoveryClassification,
)


class RecoveryClassifier:
    async def classify(
        self,
        record: ActionLedgerRecord,
        *,
        idempotency_key: str | None,
        inspector: IdempotencyInspector | None = None,
        reconciler: ActionReconciler | None = None,
        current_runtime_instance_id: str | None = None,
    ) -> RecoveryClassification:
        if record.state == "COMPLETED":
            return RecoveryClassification(
                state="COMPLETED",
                completion_kind=record.completion_kind,
                reason="already_completed",
            )
        if (
            record.state == "IN_PROGRESS"
            and current_runtime_instance_id is not None
            and current_runtime_instance_id == record.runtime_instance_id
        ):
            return RecoveryClassification(state="ACTIVE", reason="current_runtime_instance")

        unresolved_seen = False
        if inspector is not None and idempotency_key is not None:
            try:
                inspection = await inspector.inspect(record.tool_name, idempotency_key)
            except Exception:
                inspection = None
            if inspection is not None:
                if inspection.status == "completed":
                    return RecoveryClassification(
                        state="COMPLETED",
                        completion_kind="RECONCILED_SUCCESS",
                        reason="anycode_completed_result",
                    )
                if inspection.status == "unresolved":
                    unresolved_seen = True
                else:
                    return RecoveryClassification(
                        state="RECONCILABLE",
                        reason="no_anycode_claim",
                        retry_allowed=True,
                    )

        if reconciler is not None:
            result = await reconciler.reconcile(record)
            if result.status == "CONFIRMED_SUCCESS":
                return RecoveryClassification(
                    state="COMPLETED",
                    completion_kind="RECONCILED_SUCCESS",
                    reason="reconciler_confirmed_success",
                )
            if result.status == "CONFIRMED_NOT_EXECUTED":
                return RecoveryClassification(
                    state="RECONCILABLE",
                    reason="reconciler_confirmed_not_executed",
                    retry_allowed=True,
                )
            return RecoveryClassification(state="UNCERTAIN", reason="reconciler_still_unknown")

        if unresolved_seen:
            return RecoveryClassification(
                state="UNCERTAIN",
                completion_kind="BLOCKED_IN_PROGRESS",
                reason="anycode_unresolved_claim",
            )
        return RecoveryClassification(state="UNCERTAIN", reason="no_safe_observation")


class ActionLedgerRecovery:
    def __init__(
        self,
        store: ActionLedgerStore,
        *,
        classifier: RecoveryClassifier | None = None,
    ) -> None:
        self.store = store
        self.classifier = classifier or RecoveryClassifier()

    async def recover(
        self,
        record: ActionLedgerRecord,
        *,
        idempotency_key: str | None,
        inspector: IdempotencyInspector | None = None,
        reconciler: ActionReconciler | None = None,
        current_runtime_instance_id: str | None = None,
    ) -> RecoveryClassification:
        classification = await self.classifier.classify(
            record,
            idempotency_key=idempotency_key,
            inspector=inspector,
            reconciler=reconciler,
            current_runtime_instance_id=current_runtime_instance_id,
        )
        if classification.state == "COMPLETED":
            from datetime import UTC, datetime

            now = datetime.now(UTC)
            completed = record.model_copy(
                update={
                    "state": "COMPLETED",
                    "completion_kind": classification.completion_kind or "RECONCILED_SUCCESS",
                    "updated_at": now,
                    "completed_at": now,
                }
            )
            self.store.update(completed)
        elif classification.state == "UNCERTAIN" and record.state != "UNCERTAIN":
            from datetime import UTC, datetime

            now = datetime.now(UTC)
            uncertain = record.model_copy(
                update={
                    "state": "UNCERTAIN",
                    "completion_kind": classification.completion_kind,
                    "retry_safe": False,
                    "updated_at": now,
                }
            )
            self.store.update(uncertain)
        return classification
