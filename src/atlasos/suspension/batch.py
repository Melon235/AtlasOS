"""Whole-response M3 preflight plan and the suspension interrupt."""

from __future__ import annotations

from dataclasses import dataclass

from atlasos.suspension.models import PendingInvocation


@dataclass(frozen=True)
class BatchCall:
    """One model-visible tool call in original ordinal order."""

    ordinal: int
    call_id: str
    tool_name: str
    raw_input: dict
    execution_key: str | None = None


@dataclass(frozen=True)
class BatchPreflight:
    requires_human: bool
    invocations: tuple[PendingInvocation, ...]
    all_denied: bool = False


class ExecutionSuspended(BaseException):
    """Control-flow interrupt: a whole model response was durably suspended.

    This intentionally derives from ``BaseException`` so AnyCode's generic per-task exception
    handlers cannot convert a planned pause into a failed task. The Atlas Runtime catches it at
    its own boundary and records the paused run.
    """

    def __init__(self, suspension_id: str, run_id: str) -> None:
        super().__init__(suspension_id)
        self.suspension_id = suspension_id
        self.run_id = run_id
