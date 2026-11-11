"""Thin trajectory recorder: encode evidence and append it to AnyCode RunStore.

It intentionally exposes no execute/authorize/approve/resume/replay API. Recorder failures are
optional evidence failures and never alter runtime semantics.
"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Any

from anycode.runstore.protocol import RunStore
from anycode.security.redaction import safe_exception_message

from atlasos.trajectory.codec import encode_envelope
from atlasos.trajectory.models import (
    ActionDecisionData,
    ActionProposedData,
    ApprovalResolvedData,
    ExecutionOutcomeData,
    ObservationOutcome,
    ResumeStartedData,
    RunTerminalData,
    SandboxLifecycleData,
    SuspensionClearedData,
    SuspensionCreatedData,
    SuspensionPendingRef,
    ToolDisclosedData,
    ToolObservationData,
    TrajectoryCorrelation,
    TrajectoryEnvelope,
    WorkerLifecycleData,
    derive_event_id,
)


def _merge_correlation(base: TrajectoryCorrelation | None, **fields: Any) -> TrajectoryCorrelation:
    values = {key: value for key, value in fields.items() if value is not None}
    if base is None:
        return TrajectoryCorrelation(**values)
    return base.model_copy(update=values)


class TrajectoryRecorder:
    """Best-effort durable evidence recorder bound to one Atlas run id."""

    def __init__(
        self,
        store: RunStore,
        run_id: str,
        *,
        agent_name: str = "atlas-runtime",
        model: str = "unknown",
    ) -> None:
        self.store = store
        self.run_id = run_id
        self._lock = threading.Lock()
        self.recording_failures = 0
        self.last_error: str | None = None
        self._ensure_run(agent_name=agent_name, model=model)

    def _ensure_run(self, *, agent_name: str, model: str) -> None:
        try:
            with self._lock:
                if self.store.read_record(self.run_id) is None:
                    self.store.create_run(
                        self.run_id,
                        agent_name=agent_name,
                        model=model,
                        metadata={"atlas_trajectory": "1"},
                    )
        except Exception as error:  # noqa: BLE001 - evidence must not break runtime
            self._record_failure(error)

    def _record_failure(self, error: BaseException) -> None:
        self.recording_failures += 1
        self.last_error = safe_exception_message(error)

    def _append(self, envelope: TrajectoryEnvelope) -> TrajectoryEnvelope | None:
        try:
            payload = encode_envelope(envelope)
            with self._lock:
                self.store.append_event(self.run_id, "lifecycle", payload)
            return envelope
        except Exception as error:  # noqa: BLE001 - evidence must not break runtime
            self._record_failure(error)
            return None

    # -- model / M1 / M2 ----------------------------------------------------

    def record_action_proposed(
        self,
        *,
        model_response_id: str,
        ordinal: int,
        call_id: str,
        tool_id: str,
        tool_name: str,
        input_digest: str,
        input_shape: str | None = None,
        input_size: int | None = None,
        input_ref: str | None = None,
        input_state: str | None = None,
        correlation: TrajectoryCorrelation | None = None,
    ) -> TrajectoryEnvelope | None:
        identity = {
            "model_response_id": model_response_id,
            "ordinal": ordinal,
            "call_id": call_id,
            "tool_id": tool_id,
            "input_digest": input_digest,
        }
        envelope = TrajectoryEnvelope(
            event_id=derive_event_id(
                event_type="action_proposed", run_id=self.run_id, identity=identity
            ),
            run_id=self.run_id,
            event_type="action_proposed",
            source="model",
            correlation=_merge_correlation(
                correlation,
                model_response_id=model_response_id,
                ordinal=ordinal,
                call_id=call_id,
                tool_id=tool_id,
            ),
            data=ActionProposedData(
                model_response_id=model_response_id,
                ordinal=ordinal,
                call_id=call_id,
                tool_id=tool_id,
                tool_name=tool_name,
                input_digest=input_digest,
                input_shape=input_shape,
                input_size=input_size,
                input_ref=input_ref,
                input_state=input_state,  # type: ignore[arg-type]
            ).model_dump(mode="json"),
        )
        return self._append(envelope)

    def record_tool_disclosed(
        self,
        *,
        tool_id: str,
        tool_name: str,
        disclosure_source: str = "tool_search",
        query_digest: str | None = None,
        correlation: TrajectoryCorrelation | None = None,
    ) -> TrajectoryEnvelope | None:
        envelope = TrajectoryEnvelope(
            event_id=derive_event_id(
                event_type="tool_disclosed",
                run_id=self.run_id,
                identity={"tool_id": tool_id, "transition": "disclosed"},
            ),
            run_id=self.run_id,
            event_type="tool_disclosed",
            source="m1",
            correlation=_merge_correlation(correlation, tool_id=tool_id),
            data=ToolDisclosedData(
                tool_id=tool_id,
                tool_name=tool_name,
                disclosure_source=disclosure_source,
                query_digest=query_digest,
            ).model_dump(mode="json"),
        )
        return self._append(envelope)

    def record_worker_lifecycle(
        self,
        *,
        worker_id: str,
        profile_id: str,
        parent_agent: str | None,
        mode: str,
        context_hash: str,
        transition_id: str,
        stop_code: str | None = None,
        stop_reason: str | None = None,
        occurred_at: datetime | None = None,
        correlation: TrajectoryCorrelation | None = None,
    ) -> TrajectoryEnvelope | None:
        envelope = TrajectoryEnvelope(
            event_id=derive_event_id(
                event_type="worker_lifecycle",
                run_id=self.run_id,
                identity={
                    "worker_id": worker_id,
                    "mode": mode,
                    "transition_id": transition_id,
                },
            ),
            run_id=self.run_id,
            event_type="worker_lifecycle",
            source="m2",
            occurred_at=occurred_at,
            correlation=_merge_correlation(
                correlation,
                worker_id=worker_id,
                profile_id=profile_id,
            ),
            data=WorkerLifecycleData(
                worker_id=worker_id,
                profile_id=profile_id,
                parent_agent=parent_agent,
                mode=mode,  # type: ignore[arg-type]
                context_hash=context_hash,
                stop_code=stop_code,
                stop_reason=stop_reason,
            ).model_dump(mode="json"),
        )
        return self._append(envelope)

    # -- M3 / M4 / observation ---------------------------------------------

    def record_action_decision(
        self,
        *,
        phase: str,
        decision: str,
        decision_source: str,
        tool_id: str,
        call_id: str | None,
        occurrence_id: str,
        policy_rule_id: str | None,
        decision_basis_digest: str,
        action_digest: str,
        input_digest: str,
        tool_binding_digest: str,
        reason_codes: tuple[str, ...],
        scope_alignment: str | None,
        reviewer_verdict: str | None,
        external_policy_outcome: str | None,
        occurred_at: datetime | None = None,
        correlation: TrajectoryCorrelation | None = None,
    ) -> TrajectoryEnvelope | None:
        identity = {
            "call_id": call_id or tool_id,
            "phase": phase,
            "occurrence_id": occurrence_id,
            "decision": decision,
            "decision_source": decision_source,
            "decision_basis_digest": decision_basis_digest,
            "reason_codes": list(reason_codes),
        }
        envelope = TrajectoryEnvelope(
            event_id=derive_event_id(
                event_type="action_decision", run_id=self.run_id, identity=identity
            ),
            run_id=self.run_id,
            event_type="action_decision",
            source="m3",
            occurred_at=occurred_at,
            correlation=_merge_correlation(
                correlation,
                call_id=call_id,
                tool_id=tool_id,
                occurrence_id=occurrence_id,
            ),
            data=ActionDecisionData(
                phase=phase,  # type: ignore[arg-type]
                decision=decision,  # type: ignore[arg-type]
                decision_source=decision_source,  # type: ignore[arg-type]
                tool_id=tool_id,
                call_id=call_id,
                policy_rule_id=policy_rule_id,
                decision_basis_digest=decision_basis_digest,
                action_digest=action_digest,
                input_digest=input_digest,
                tool_binding_digest=tool_binding_digest,
                reason_codes=reason_codes,
                scope_alignment=scope_alignment,
                reviewer_verdict=reviewer_verdict,
                external_policy_outcome=external_policy_outcome,
            ).model_dump(mode="json"),
        )
        return self._append(envelope)

    def record_execution_outcome(
        self,
        *,
        action_id: str,
        attempt_id: str,
        tool_id: str,
        canonical_action: str,
        state: str,
        completion_kind: str | None,
        claim_outcome: str | None,
        retry_safe: bool | None,
        resource_digest: str | None,
        result_digest: str | None,
        result_size: int | None,
        occurred_at: datetime | None = None,
        correlation: TrajectoryCorrelation | None = None,
        backfilled: bool = False,
    ) -> TrajectoryEnvelope | None:
        identity = {
            "action_id": action_id,
            "attempt_id": attempt_id,
            "state": state,
            "completion_kind": completion_kind,
            "result_digest": result_digest,
        }
        envelope = TrajectoryEnvelope(
            event_id=derive_event_id(
                event_type="execution_outcome", run_id=self.run_id, identity=identity
            ),
            run_id=self.run_id,
            event_type="execution_outcome",
            source="m4",
            occurred_at=occurred_at,
            backfilled=backfilled,
            correlation=_merge_correlation(
                correlation,
                tool_id=tool_id,
                action_id=action_id,
                attempt_id=attempt_id,
            ),
            authority_ref=action_id,
            authority_revision=attempt_id,
            data=ExecutionOutcomeData(
                action_id=action_id,
                attempt_id=attempt_id,
                tool_id=tool_id,
                canonical_action=canonical_action,
                state=state,  # type: ignore[arg-type]
                completion_kind=completion_kind,
                claim_outcome=claim_outcome,
                retry_safe=retry_safe,
                resource_digest=resource_digest,
                result_digest=result_digest,
                result_size=result_size,
            ).model_dump(mode="json"),
        )
        return self._append(envelope)

    def record_tool_observation(
        self,
        *,
        call_id: str,
        tool_id: str,
        is_error: bool,
        result_digest: str,
        result_size: int,
        outcome: ObservationOutcome | None = None,
        transcript_seq_ref: int | None = None,
        correlation: TrajectoryCorrelation | None = None,
        backfilled: bool = False,
        occurred_at: datetime | None = None,
    ) -> TrajectoryEnvelope | None:
        identity = {
            "call_id": call_id,
            "is_error": is_error,
            "result_digest": result_digest,
        }
        envelope = TrajectoryEnvelope(
            event_id=derive_event_id(
                event_type="tool_observation", run_id=self.run_id, identity=identity
            ),
            run_id=self.run_id,
            event_type="tool_observation",
            source="runtime",
            occurred_at=occurred_at,
            backfilled=backfilled,
            correlation=_merge_correlation(correlation, call_id=call_id, tool_id=tool_id),
            data=ToolObservationData(
                call_id=call_id,
                tool_id=tool_id,
                is_error=is_error,
                result_digest=result_digest,
                result_size=result_size,
                outcome=outcome,
                transcript_seq_ref=transcript_seq_ref,
            ).model_dump(mode="json"),
        )
        return self._append(envelope)

    # -- M5 / M6 / runtime --------------------------------------------------

    def record_sandbox_lifecycle(
        self,
        *,
        binding_id: str,
        task_id: str,
        worker_id: str | None,
        provider_name: str,
        state: str,
        source_commit: str | None,
        sandbox_id: str | None = None,
        capabilities_digest: str | None = None,
        capabilities: dict[str, Any] | None = None,
        network: str | None = None,
        occurred_at: datetime | None = None,
        correlation: TrajectoryCorrelation | None = None,
    ) -> TrajectoryEnvelope | None:
        envelope = TrajectoryEnvelope(
            event_id=derive_event_id(
                event_type="sandbox_lifecycle",
                run_id=self.run_id,
                identity={"binding_id": binding_id, "state": state},
            ),
            run_id=self.run_id,
            event_type="sandbox_lifecycle",
            source="m5",
            occurred_at=occurred_at,
            correlation=_merge_correlation(
                correlation, worker_id=worker_id, sandbox_binding_id=binding_id
            ),
            data=SandboxLifecycleData(
                binding_id=binding_id,
                task_id=task_id,
                worker_id=worker_id,
                provider_name=provider_name,
                state=state,  # type: ignore[arg-type]
                source_commit=source_commit,
                sandbox_id=sandbox_id,
                capabilities_digest=capabilities_digest,
                capabilities=capabilities,
                network=network,
            ).model_dump(mode="json"),
        )
        return self._append(envelope)

    def record_suspension_created(
        self,
        *,
        suspension_id: str,
        assistant_response_digest: str,
        execution_scope: str | None,
        pending: tuple[SuspensionPendingRef, ...],
        occurred_at: datetime | None = None,
        correlation: TrajectoryCorrelation | None = None,
        backfilled: bool = False,
    ) -> TrajectoryEnvelope | None:
        envelope = TrajectoryEnvelope(
            event_id=derive_event_id(
                event_type="suspension_created",
                run_id=self.run_id,
                identity={"suspension_id": suspension_id, "transition": "created"},
            ),
            run_id=self.run_id,
            event_type="suspension_created",
            source="m6",
            occurred_at=occurred_at,
            backfilled=backfilled,
            correlation=_merge_correlation(correlation, suspension_id=suspension_id),
            data=SuspensionCreatedData(
                suspension_id=suspension_id,
                kind="approval",
                resume_point="before_tool_execution",
                assistant_response_digest=assistant_response_digest,
                execution_scope=execution_scope,
                pending=pending,
            ).model_dump(mode="json"),
        )
        return self._append(envelope)

    def record_approval_resolved(
        self,
        *,
        approval_id: str,
        suspension_id: str,
        call_id: str,
        decision: str,
        version: int,
        reviewer: str | None,
        reason_digest: str | None,
        occurred_at: datetime | None = None,
        correlation: TrajectoryCorrelation | None = None,
        backfilled: bool = False,
    ) -> TrajectoryEnvelope | None:
        envelope = TrajectoryEnvelope(
            event_id=derive_event_id(
                event_type="approval_resolved",
                run_id=self.run_id,
                identity={"approval_id": approval_id, "version": version},
            ),
            run_id=self.run_id,
            event_type="approval_resolved",
            source="m6",
            occurred_at=occurred_at,
            backfilled=backfilled,
            correlation=_merge_correlation(
                correlation,
                approval_id=approval_id,
                suspension_id=suspension_id,
                call_id=call_id,
            ),
            data=ApprovalResolvedData(
                approval_id=approval_id,
                suspension_id=suspension_id,
                call_id=call_id,
                decision=decision,  # type: ignore[arg-type]
                version=version,
                reviewer=reviewer,
                reason_digest=reason_digest,
            ).model_dump(mode="json"),
        )
        return self._append(envelope)

    def record_resume_started(
        self,
        *,
        suspension_id: str,
        revision: int,
        owner: str,
        occurred_at: datetime | None = None,
        correlation: TrajectoryCorrelation | None = None,
    ) -> TrajectoryEnvelope | None:
        envelope = TrajectoryEnvelope(
            event_id=derive_event_id(
                event_type="resume_started",
                run_id=self.run_id,
                identity={
                    "suspension_id": suspension_id,
                    "revision": revision,
                    "transition": "resume_started",
                },
            ),
            run_id=self.run_id,
            event_type="resume_started",
            source="runtime",
            occurred_at=occurred_at,
            correlation=_merge_correlation(correlation, suspension_id=suspension_id),
            data=ResumeStartedData(
                suspension_id=suspension_id, revision=revision, owner=owner
            ).model_dump(mode="json"),
        )
        return self._append(envelope)

    def record_suspension_cleared(
        self,
        *,
        suspension_id: str,
        revision: int,
        result_batch_digest: str | None,
        result_count: int,
        occurred_at: datetime | None = None,
        correlation: TrajectoryCorrelation | None = None,
        backfilled: bool = False,
    ) -> TrajectoryEnvelope | None:
        envelope = TrajectoryEnvelope(
            event_id=derive_event_id(
                event_type="suspension_cleared",
                run_id=self.run_id,
                identity={
                    "suspension_id": suspension_id,
                    "revision": revision,
                    "transition": "cleared",
                },
            ),
            run_id=self.run_id,
            event_type="suspension_cleared",
            source="runtime",
            occurred_at=occurred_at,
            backfilled=backfilled,
            correlation=_merge_correlation(correlation, suspension_id=suspension_id),
            data=SuspensionClearedData(
                suspension_id=suspension_id,
                revision=revision,
                result_batch_digest=result_batch_digest,
                result_count=result_count,
            ).model_dump(mode="json"),
        )
        return self._append(envelope)

    def record_run_terminal(
        self,
        *,
        status: str,
        task_count: int,
        final_output_digest: str | None,
        final_output_size: int | None,
        occurred_at: datetime | None = None,
        correlation: TrajectoryCorrelation | None = None,
    ) -> TrajectoryEnvelope | None:
        envelope = TrajectoryEnvelope(
            event_id=derive_event_id(
                event_type="run_terminal",
                run_id=self.run_id,
                identity={"status": status, "final_output_digest": final_output_digest},
            ),
            run_id=self.run_id,
            event_type="run_terminal",
            source="runtime",
            occurred_at=occurred_at,
            correlation=correlation or TrajectoryCorrelation(),
            data=RunTerminalData(
                status=status,
                task_count=task_count,
                final_output_digest=final_output_digest,
                final_output_size=final_output_size,
            ).model_dump(mode="json"),
        )
        return self._append(envelope)


class NoopTrajectoryRecorder:
    """Disabled M7 recorder. All methods are no-ops and never touch runtime authority."""

    def record_action_proposed(self, **_: Any) -> None:
        return None

    def record_tool_disclosed(self, **_: Any) -> None:
        return None

    def record_worker_lifecycle(self, **_: Any) -> None:
        return None

    def record_action_decision(self, **_: Any) -> None:
        return None

    def record_execution_outcome(self, **_: Any) -> None:
        return None

    def record_tool_observation(self, **_: Any) -> None:
        return None

    def record_sandbox_lifecycle(self, **_: Any) -> None:
        return None

    def record_suspension_created(self, **_: Any) -> None:
        return None

    def record_approval_resolved(self, **_: Any) -> None:
        return None

    def record_resume_started(self, **_: Any) -> None:
        return None

    def record_suspension_cleared(self, **_: Any) -> None:
        return None

    def record_run_terminal(self, **_: Any) -> None:
        return None
