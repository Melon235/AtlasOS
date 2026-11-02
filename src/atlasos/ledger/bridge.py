"""Thin recording delegate: M3-authorized call -> ledger -> AnyCode executor."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from anycode.tools.executor import ToolExecutor
from anycode.tools.registry import ToolRegistry
from anycode.types import ToolDefinition, ToolResult, ToolUseContext

from atlasos.actions.catalog import ActionCatalog
from atlasos.ledger.identity import ActionIdentityProvider
from atlasos.ledger.models import (
    ActionLedgerRecord,
    ActionLedgerStore,
    CompletionKind,
    LedgerState,
)
from atlasos.trajectory.context import current_call_trajectory
from atlasos.trajectory.models import TrajectoryCorrelation, canonical_digest

if TYPE_CHECKING:
    from atlasos.trajectory.recorder import TrajectoryRecorder


class DurableActionBridge:
    """Delegates real execution to AnyCode; never claims idempotency itself."""

    def __init__(
        self,
        executor: ToolExecutor,
        registry: ToolRegistry,
        ledger: ActionLedgerStore,
        *,
        runtime_instance_id: str,
        catalog: ActionCatalog | None = None,
        identity_provider: ActionIdentityProvider | None = None,
        before_record: Callable[[], None] | None = None,
        after_record: Callable[[], None] | None = None,
        after_execute: Callable[[], None] | None = None,
        trajectory_recorder: TrajectoryRecorder | None = None,
    ) -> None:
        self._executor = executor
        self._registry = registry
        self.ledger = ledger
        self.runtime_instance_id = runtime_instance_id
        self.catalog = catalog or ActionCatalog()
        self.identity_provider = identity_provider or ActionIdentityProvider(ledger)
        self._before_record = before_record
        self._after_record = after_record
        self._after_execute = after_execute
        self.trajectory_recorder = trajectory_recorder

    async def execute(
        self,
        tool_name: str,
        raw_input: dict[str, Any],
        context: ToolUseContext,
        *,
        idempotency_key: str | None = None,
    ) -> ToolResult:
        tool = self._registry.get(tool_name)
        if tool is None or not tool.side_effecting:
            # Read/non-side-effecting tools are never result-cached by M4.
            return await self._executor.execute(
                tool_name, raw_input, context, idempotency_key=idempotency_key
            )

        identity = self.identity_provider.for_key(idempotency_key)
        if identity is None:
            return _failure(
                "m4_trusted_idempotency_identity_required: side-effecting execution "
                "requires a trusted runtime idempotency key"
            )

        trusted_input = _trusted_input(tool, raw_input, identity.idempotency_key)
        if trusted_input is None:
            return _failure(
                "m4_model_controlled_idempotency_key_rejected: the model-facing "
                "idempotency field must be absent or equal to the trusted runtime key"
            )
        resolution = self.catalog.resolve(tool_name, trusted_input)
        request_digest = _digest(trusted_input)
        prior = self.ledger.latest_for_key_hash(identity.idempotency_key_hash)
        now = datetime.now(UTC)
        call_context = current_call_trajectory()
        record = ActionLedgerRecord(
            action_id=identity.action_id,
            attempt_id=str(uuid.uuid4()),
            runtime_instance_id=self.runtime_instance_id,
            run_id=_context_attr(context, "run_id"),
            task_id=_context_attr(context, "task_id"),
            worker_id=_context_attr(context, "worker_id"),
            agent=context.agent.name,
            tool_name=tool_name,
            canonical_action=resolution.action,
            resource=resolution.resource,
            m3_call_id=(
                call_context.call_id
                if call_context is not None and self.trajectory_recorder is not None
                else None
            ),
            request_digest=request_digest,
            digest_version="1",
            idempotency_key_hash=identity.idempotency_key_hash,
            state="IN_PROGRESS",
            created_at=now,
            updated_at=now,
        )
        if self._before_record is not None:
            self._before_record()
        self.ledger.create(record)
        if self._after_record is not None:
            self._after_record()

        try:
            result = await self._executor.execute(
                tool_name,
                trusted_input,
                context,
                idempotency_key=identity.idempotency_key,
            )
        except BaseException:
            finished = self._finish(
                record,
                state="UNCERTAIN",
                completion_kind=None,
                result=None,
                prior=prior,
            )
            self._record_outcome(finished, result=None)
            raise
        if self._after_execute is not None:
            self._after_execute()

        state, kind, retry_safe = _classify_result(result, prior, request_digest)
        finished = self._finish(
            record,
            state=state,
            completion_kind=kind,
            result=result,
            prior=prior,
            retry_safe=retry_safe,
        )
        self._record_outcome(finished, result=result)
        return result

    def _record_outcome(self, record: ActionLedgerRecord, *, result: ToolResult | None) -> None:
        recorder = self.trajectory_recorder
        if recorder is None:
            return
        try:  # noqa: SIM105
            recorder.record_execution_outcome(
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
                result_size=(len(str(result.data).encode("utf-8")) if result is not None else None),
                occurred_at=record.completed_at or record.updated_at,
                correlation=TrajectoryCorrelation(
                    tool_id=record.tool_name,
                    action_id=record.action_id,
                    attempt_id=record.attempt_id,
                    call_id=record.m3_call_id,
                    worker_id=record.worker_id,
                    profile_id=None,
                ),
            )
        except Exception:  # noqa: BLE001 - evidence is optional
            pass

    def _finish(
        self,
        record: ActionLedgerRecord,
        *,
        state: LedgerState,
        completion_kind: CompletionKind | None,
        result: ToolResult | None,
        prior: ActionLedgerRecord | None,
        retry_safe: bool | None = None,
    ) -> ActionLedgerRecord:
        now = datetime.now(UTC)
        if result is not None:
            retry_safe = result.retry_safe if retry_safe is None else retry_safe
        completed = state == "COMPLETED"
        updated = record.model_copy(
            update={
                "state": state,
                "completion_kind": completion_kind,
                "claim_outcome": _claim_outcome(state, completion_kind),
                "result_digest": _result_digest(result) if result is not None else None,
                "retry_safe": retry_safe,
                "updated_at": now,
                "completed_at": now if completed else None,
            }
        )
        self.ledger.update(updated)
        return updated


def _trusted_input(
    tool: ToolDefinition, raw_input: dict[str, Any], trusted_key: str
) -> dict[str, Any] | None:
    """Reject model-controlled keys; AnyCode receives only the trusted fallback."""

    trusted = dict(raw_input)
    field = tool.idempotency_key_field
    if field is None or field not in trusted or trusted[field] is None:
        return trusted
    if str(trusted[field]) != trusted_key:
        return None
    return trusted


def _classify_result(
    result: ToolResult,
    prior: ActionLedgerRecord | None,
    request_digest: str,
) -> tuple[LedgerState, CompletionKind | None, bool | None]:
    text = str(result.data)
    if "Idempotency key conflict" in text:
        return "COMPLETED", "CONFLICT", False
    if "already in progress" in text:
        return "UNCERTAIN", "BLOCKED_IN_PROGRESS", False
    if "idempotency outcome could not be recorded" in text:
        return "UNCERTAIN", None, False
    if result.is_error:
        if _pre_execution_failure(text):
            return "COMPLETED", "EXECUTED_ERROR", bool(result.retry_safe)
        # Side-effecting failures may occur after the external effect.
        return "UNCERTAIN", "EXECUTED_ERROR", False
    if prior is not None and prior.request_digest == request_digest:
        return "COMPLETED", "REPLAYED", True
    return "COMPLETED", "EXECUTED_SUCCESS", True


def _pre_execution_failure(text: str) -> bool:
    markers = (
        "Invalid input for tool",
        "is not registered",
        "requires a non-empty idempotency key",
        "Idempotency store unavailable",
        "action_denied",
        "action_hard_denied",
    )
    return any(marker in text for marker in markers)


def _claim_outcome(state: LedgerState, kind: CompletionKind | None) -> str | None:
    if kind == "REPLAYED":
        return "replay"
    if kind == "CONFLICT":
        return "conflict"
    if kind == "BLOCKED_IN_PROGRESS":
        return "in_progress"
    if kind in ("EXECUTED_SUCCESS", "EXECUTED_ERROR"):
        return "execute"
    return None


def _context_attr(context: ToolUseContext, key: str) -> str | None:
    if context.execution_context is None:
        return None
    value = context.execution_context.attributes.get(key)
    if value is None:
        nested = context.execution_context.attributes.get("atlas_worker")
        if isinstance(nested, dict):
            value = nested.get(key)
    return str(value) if value is not None else None


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _digest(arguments: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(arguments).encode("utf-8")).hexdigest()


def _result_digest(result: ToolResult) -> str:
    return hashlib.sha256(_canonical(result.model_dump(mode="json")).encode("utf-8")).hexdigest()


def _failure(message: str) -> ToolResult:
    return ToolResult(data=message, is_error=True, retry_safe=False)
