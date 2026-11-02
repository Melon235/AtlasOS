"""Read-only projection of AnyCode RunStore transcript into a typed trajectory."""

from __future__ import annotations

from collections import defaultdict

from anycode.runstore.protocol import RunStore
from anycode.types import TranscriptEvent

from atlasos.trajectory.codec import (
    UnsupportedTrajectorySchema,
    decode_envelope,
    native_message_projection,
    native_tool_result_projection,
)
from atlasos.trajectory.models import (
    TrajectoryConflict,
    TrajectoryCorrelation,
    TrajectoryDecodeFailure,
    TrajectoryEvent,
    TrajectoryGap,
    TrajectoryMetrics,
    TrajectoryView,
    canonical_digest,
    canonical_json,
    derive_event_id,
)


class TrajectoryReader:
    """Projects durable transcript evidence. It has no runtime mutation or execution API."""

    def __init__(self, store: RunStore) -> None:
        self.store = store

    def read(self, run_id: str) -> TrajectoryView:
        transcript = self.store.read_events(run_id)
        decode_failures: list[TrajectoryDecodeFailure] = []
        candidates: list[TrajectoryEvent] = []
        physical_count = 0

        for event in transcript:
            projected = self._project_event(run_id, event, decode_failures)
            if projected is not None:
                candidates.append(projected)
                physical_count += 1

        logical, conflicts = self._dedupe(candidates)
        native_observation_calls = {
            event.correlation.call_id
            for event in logical
            if event.source == "anycode"
            and event.event_type == "tool_observation"
            and event.correlation.call_id
        }
        duplicated_native = 0
        filtered: list[TrajectoryEvent] = []
        for event in logical:
            if (
                event.event_type == "tool_observation"
                and event.source == "runtime"
                and event.correlation.call_id in native_observation_calls
            ):
                duplicated_native += 1
                continue
            filtered.append(event)

        gaps = self._find_gaps(filtered)
        pending = self._pending_suspensions(filtered)
        metrics = TrajectoryMetrics(
            physical_events=physical_count,
            logical_events=len(filtered),
            backfilled_events=sum(1 for event in filtered if event.backfilled),
            evidence_gaps=len(gaps),
            evidence_conflicts=len(conflicts),
            decode_failures=len(decode_failures),
            action_proposed_count=sum(
                1 for event in filtered if event.event_type == "action_proposed"
            ),
            action_decision_count=sum(
                1 for event in filtered if event.event_type == "action_decision"
            ),
            execution_outcome_count=sum(
                1 for event in filtered if event.event_type == "execution_outcome"
            ),
            approval_event_count=sum(
                1 for event in filtered if event.event_type == "approval_resolved"
            ),
            suspension_event_count=sum(
                1
                for event in filtered
                if event.event_type
                in ("suspension_created", "resume_started", "suspension_cleared")
            ),
            native_tool_observations_duplicated=duplicated_native,
        )
        return TrajectoryView(
            run_id=run_id,
            events=tuple(filtered),
            gaps=tuple(gaps),
            conflicts=tuple(conflicts),
            decode_failures=tuple(decode_failures),
            pending_suspensions=tuple(pending),
            metrics=metrics,
        )

    def _project_event(
        self,
        run_id: str,
        event: TranscriptEvent,
        decode_failures: list[TrajectoryDecodeFailure],
    ) -> TrajectoryEvent | None:
        if event.kind == "lifecycle":
            try:
                envelope = decode_envelope(event.payload, seq=event.seq)
            except (UnsupportedTrajectorySchema, ValueError) as error:
                decode_failures.append(TrajectoryDecodeFailure(seq=event.seq, reason=str(error)))
                return None
            if envelope is None:
                return None
            return TrajectoryEvent(
                event_id=envelope.event_id,
                run_id=envelope.run_id,
                event_type=envelope.event_type,
                source=envelope.source,
                occurred_at=envelope.occurred_at,
                recorded_at=event.ts,
                seq=event.seq,
                backfilled=envelope.backfilled,
                correlation=envelope.correlation,
                data=envelope.data,
                authority_ref=envelope.authority_ref,
                authority_revision=envelope.authority_revision,
                physical_refs=(event.seq,),
            )

        message = native_message_projection(event)
        if message is not None:
            role = message["role"]
            event_type = "user_message" if role == "user" else "model_message"
            return TrajectoryEvent(
                event_id=derive_event_id(
                    event_type=event_type,
                    run_id=run_id,
                    identity={"native_seq": event.seq},
                ),
                run_id=run_id,
                event_type=event_type,
                source="anycode",
                recorded_at=event.ts,
                seq=event.seq,
                correlation=TrajectoryCorrelation(),
                data=message,
                physical_refs=(event.seq,),
            )

        native_result = native_tool_result_projection(event)
        if native_result is not None:
            return TrajectoryEvent(
                event_id=derive_event_id(
                    event_type="tool_observation",
                    run_id=run_id,
                    identity={
                        "native_seq": event.seq,
                        "call_id": native_result["call_id"],
                    },
                ),
                run_id=run_id,
                event_type="tool_observation",
                source="anycode",
                recorded_at=event.ts,
                seq=event.seq,
                correlation=TrajectoryCorrelation(
                    call_id=native_result["call_id"],
                    tool_id=native_result["tool_id"],
                ),
                data=native_result,
                physical_refs=(event.seq,),
            )
        return None

    def _dedupe(
        self, candidates: list[TrajectoryEvent]
    ) -> tuple[list[TrajectoryEvent], list[TrajectoryConflict]]:
        groups: dict[str, list[TrajectoryEvent]] = defaultdict(list)
        order: list[str] = []
        for event in candidates:
            if event.event_id not in groups:
                order.append(event.event_id)
            groups[event.event_id].append(event)

        selected: list[TrajectoryEvent] = []
        conflicts: list[TrajectoryConflict] = []
        for event_id in order:
            group = groups[event_id]
            ordered = sorted(group, key=lambda item: (item.backfilled, item.seq))
            semantic = [
                canonical_json(
                    event.model_dump(
                        mode="json",
                        exclude={
                            "seq",
                            "recorded_at",
                            "occurred_at",
                            "backfilled",
                            "physical_refs",
                        },
                    )
                )
                for event in ordered
            ]
            if len(set(semantic)) != 1:
                conflicts.append(
                    TrajectoryConflict(
                        event_id=event_id,
                        seqs=tuple(sorted(event.seq for event in group)),
                        payload_digests=tuple(canonical_digest(digest) for digest in semantic),
                    )
                )
                continue
            chosen = ordered[0]
            selected.append(
                chosen.model_copy(
                    update={"physical_refs": tuple(sorted(event.seq for event in group))}
                )
            )
        selected.sort(key=lambda item: item.seq)
        return selected, conflicts

    @staticmethod
    def _find_gaps(events: list[TrajectoryEvent]) -> list[TrajectoryGap]:
        gaps: list[TrajectoryGap] = []
        proposed = {
            event.correlation.call_id
            for event in events
            if event.event_type == "action_proposed" and event.correlation.call_id
        }
        decided = {
            event.correlation.call_id
            for event in events
            if event.event_type == "action_decision" and event.correlation.call_id
        }
        for call_id in sorted(proposed - decided):
            gaps.append(
                TrajectoryGap(
                    kind="missing_action_decision",
                    event_type="action_decision",
                    call_id=call_id,
                    detail="ACTION_PROPOSED has no recorded M3 decision and no durable M3 "
                    "history exists to reconstruct it",
                )
            )
        created = {
            event.correlation.suspension_id
            for event in events
            if event.event_type == "suspension_created" and event.correlation.suspension_id
        }
        cleared = {
            event.correlation.suspension_id
            for event in events
            if event.event_type == "suspension_cleared" and event.correlation.suspension_id
        }
        approvals = {
            event.correlation.suspension_id
            for event in events
            if event.event_type == "approval_resolved" and event.correlation.suspension_id
        }
        for suspension_id in sorted(approvals - created):
            gaps.append(
                TrajectoryGap(
                    kind="approval_without_suspension",
                    event_type="suspension_created",
                    suspension_id=suspension_id,
                )
            )
        for suspension_id in sorted(cleared - created):
            gaps.append(
                TrajectoryGap(
                    kind="clear_without_suspension",
                    event_type="suspension_created",
                    suspension_id=suspension_id,
                )
            )
        return gaps

    @staticmethod
    def _pending_suspensions(events: list[TrajectoryEvent]) -> list[str]:
        created = {
            event.correlation.suspension_id
            for event in events
            if event.event_type == "suspension_created" and event.correlation.suspension_id
        }
        cleared = {
            event.correlation.suspension_id
            for event in events
            if event.event_type == "suspension_cleared" and event.correlation.suspension_id
        }
        return sorted(created - cleared)

    def read_event_ids(self, run_id: str) -> set[str]:
        return {event.event_id for event in self.read(run_id).events}
