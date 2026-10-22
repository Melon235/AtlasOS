"""Logical context requirements, independent of concrete future context refs."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ContextRequirement:
    """What a task logically needs before it runs.

    ``logical_key`` identifies the context family; ``source_task_id`` ties it to the producing
    task. ``required_version`` is set only when an exact version is known; otherwise matching is
    by logical identity.
    """

    logical_key: str
    source_task_id: str | None = None
    required_version: int | None = None
