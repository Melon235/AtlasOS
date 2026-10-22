"""Logical context storage.

The store is an in-memory projection of what AtlasOS knows; AnyCode checkpoints remain the
source of truth for task execution. Versions are immutable: writing a new version never
mutates an older one, so a ``ContextRef`` always resolves to the content it named.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from atlasos.context.models import ContextBlock, ContextRef


class ContextStore(Protocol):
    def put(self, block: ContextBlock) -> None: ...

    def get(self, block_id: str) -> ContextBlock: ...

    def get_many(self, refs: Sequence[ContextRef]) -> list[ContextBlock]: ...

    def find_by_task(self, task_id: str) -> list[ContextBlock]: ...


class InMemoryContextStore:
    """Smallest store that preserves version and insertion semantics."""

    def __init__(self) -> None:
        self._blocks: dict[str, dict[int, ContextBlock]] = {}

    def put(self, block: ContextBlock) -> None:
        if block.version < 1:
            raise ValueError(f"context block version must be >= 1: {block.block_id!r}")
        self._blocks.setdefault(block.block_id, {})[block.version] = block

    def get(self, block_id: str) -> ContextBlock:
        versions = self._blocks.get(block_id)
        if not versions:
            raise KeyError(f"unknown context block: {block_id!r}")
        return versions[max(versions)]

    def get_many(self, refs: Sequence[ContextRef]) -> list[ContextBlock]:
        resolved: list[ContextBlock] = []
        for ref in refs:
            versions = self._blocks.get(ref.block_id)
            if versions is None or ref.version not in versions:
                raise KeyError(f"unknown context block version: {ref.block_id!r}@{ref.version}")
            resolved.append(versions[ref.version])
        return resolved

    def find_by_task(self, task_id: str) -> list[ContextBlock]:
        matches: list[ContextBlock] = []
        for versions in self._blocks.values():
            for version in sorted(versions):
                block = versions[version]
                if block.task_id == task_id:
                    matches.append(block)
        return matches
