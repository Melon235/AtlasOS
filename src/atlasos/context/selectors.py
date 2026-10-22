"""Context selectors: WHAT an agent logically receives.

Selectors decide membership only. They never order blocks for cache reuse, render text, call a
provider, or inspect cache state. The order they return is the projection order supplied by the
caller (completion order in Stage 2), and the renderer owns serialization order.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from atlasos.context.models import ContextBlock, ContextKind, ContextRef
from atlasos.context.store import ContextStore
from atlasos.workflow import AtlasTaskSpec


class ContextSelector(Protocol):
    def select(
        self,
        *,
        task: AtlasTaskSpec,
        agent_id: str,
        store: ContextStore,
        available_refs: Sequence[ContextRef],
    ) -> list[ContextBlock]: ...


class TaskScopedSelector:
    """Current task plus the outputs of its direct dependencies."""

    def select(
        self,
        *,
        task: AtlasTaskSpec,
        agent_id: str,
        store: ContextStore,
        available_refs: Sequence[ContextRef],
    ) -> list[ContextBlock]:
        task_block = ContextBlock(
            block_id=f"task:{task.task_id}",
            kind=ContextKind.TASK,
            content=task.description,
            version=1,
            source="atlas_task",
            task_id=task.task_id,
            owner_agent_id=agent_id,
        )
        dependencies = set(task.depends_on)
        selected: list[ContextBlock] = [task_block]
        for block in store.get_many(available_refs):
            if block.kind is ContextKind.DEPENDENCY_OUTPUT and block.task_id in dependencies:
                selected.append(block)
        return selected
