"""Per-async-task correlation context for trajectory evidence.

The runtime sets this context around model-call execution; the recorder reads it to correlate
evidence without changing AnyCode's ToolExecutor or ToolUseContext contracts.
"""

from __future__ import annotations

import contextvars
from contextvars import ContextVar, Token
from dataclasses import dataclass


@dataclass(frozen=True)
class CallTrajectoryContext:
    model_response_id: str | None
    ordinal: int | None
    call_id: str | None
    tool_id: str | None
    execution_scope: str | None


CURRENT_CALL_TRAJECTORY: ContextVar[CallTrajectoryContext | None] = contextvars.ContextVar(
    "atlas_call_trajectory", default=None
)


def set_current_call_trajectory(
    value: CallTrajectoryContext | None,
) -> Token[CallTrajectoryContext | None]:
    return CURRENT_CALL_TRAJECTORY.set(value)


def reset_current_call_trajectory(
    token: Token[CallTrajectoryContext | None],
) -> None:
    CURRENT_CALL_TRAJECTORY.reset(token)


def current_call_trajectory() -> CallTrajectoryContext | None:
    return CURRENT_CALL_TRAJECTORY.get()
