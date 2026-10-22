"""Deterministically rebuild Atlas logical context from durable task state.

Only tasks that upstream recorded as completed with an output produce blocks. Nothing is
inferred, no task is re-run, and no provider cache state is consulted.
"""

from __future__ import annotations

from anycode import CheckpointData

from atlasos.context import ContextBlock, ContextKind, InMemoryContextStore
from atlasos.workflow import AtlasWorkflow


def rebuild_context_store(
    checkpoint: CheckpointData,
    workflow: AtlasWorkflow,
) -> InMemoryContextStore:
    title_to_id = {task.title: task.task_id for task in workflow.tasks}
    store = InMemoryContextStore()
    for completed in sorted(checkpoint.tasks, key=lambda task: task.title):
        if completed.status != "completed" or completed.result is None:
            continue
        task_id = title_to_id.get(completed.title)
        if task_id is None:
            continue
        store.put(
            ContextBlock(
                block_id=f"task-output:{task_id}",
                kind=ContextKind.DEPENDENCY_OUTPUT,
                content=completed.result,
                version=1,
                source=f"anycode_task:{task_id}",
                task_id=task_id,
                owner_agent_id=completed.assignee,
            )
        )
    return store
