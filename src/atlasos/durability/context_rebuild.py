"""Deterministically rebuild Atlas logical context from durable task state.

Only tasks that upstream recorded as completed with an output produce blocks. Nothing is
inferred, no task is re-run, and no provider cache state is consulted.
"""

from __future__ import annotations

from anycode import CheckpointData

from atlasos.context import ContextBlock, ContextKind, InMemoryContextStore
from atlasos.workflow import AtlasWorkflow


def completed_task_ids(
    checkpoint: CheckpointData,
    workflow: AtlasWorkflow,
) -> frozenset[str]:
    """Atlas task ids that upstream recorded as completed with durable output."""

    title_to_id = {task.title: task.task_id for task in workflow.tasks}
    return frozenset(
        task_id
        for completed in checkpoint.tasks
        if completed.status == "completed" and completed.result is not None
        for task_id in (title_to_id.get(completed.title),)
        if task_id is not None
    )


def task_ids_with_status(
    checkpoint: CheckpointData,
    workflow: AtlasWorkflow,
    status: str,
) -> frozenset[str]:
    """Atlas task ids in one upstream status, used only for recovery observations."""

    title_to_id = {task.title: task.task_id for task in workflow.tasks}
    return frozenset(
        task_id
        for state in checkpoint.tasks
        if state.status == status
        for task_id in (title_to_id.get(state.title),)
        if task_id is not None
    )


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
