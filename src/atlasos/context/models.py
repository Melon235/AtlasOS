"""Explicit, typed context objects.

Stage 2 keeps the model deliberately small: no embeddings, TTL, residency, scores, or
transaction state. Blocks are logical context; they are not the durable task database.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ContextKind(StrEnum):
    SYSTEM = "system"
    TASK = "task"
    DEPENDENCY_OUTPUT = "dependency_output"
    MESSAGE = "message"
    TOOL_RESULT = "tool_result"
    ARTIFACT = "artifact"
    MEMORY = "memory"
    SUMMARY = "summary"


@dataclass(frozen=True)
class ContextBlock:
    """One logical piece of context. ``version`` identifies immutable content revisions."""

    block_id: str
    kind: ContextKind
    content: str
    version: int = 1
    source: str | None = None
    task_id: str | None = None
    owner_agent_id: str | None = None


@dataclass(frozen=True)
class ContextRef:
    """A resolvable pointer to one version of a block."""

    block_id: str
    version: int
