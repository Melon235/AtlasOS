from __future__ import annotations

from atlasos.context import (
    ContextBlock,
    ContextKind,
    ContextRef,
    InMemoryContextStore,
    TaskScopedSelector,
)
from atlasos.workflow import AtlasTaskSpec


def _output(block_id: str, task_id: str, content: str) -> ContextBlock:
    return ContextBlock(
        block_id=block_id,
        kind=ContextKind.DEPENDENCY_OUTPUT,
        content=content,
        task_id=task_id,
        owner_agent_id=f"agent-{task_id}",
    )


def _task(depends_on: tuple[str, ...] = ("a", "b")) -> AtlasTaskSpec:
    return AtlasTaskSpec(
        task_id="review",
        title="Review",
        description="compare outputs",
        assignee="reviewer",
        depends_on=depends_on,
    )


def test_selects_task_and_direct_dependencies_only() -> None:
    store = InMemoryContextStore()
    store.put(_output("task-output:a", "a", "alpha"))
    store.put(_output("task-output:b", "b", "beta"))
    store.put(_output("task-output:c", "c", "unrelated"))
    refs = [
        ContextRef(block_id="task-output:c", version=1),
        ContextRef(block_id="task-output:a", version=1),
        ContextRef(block_id="task-output:b", version=1),
    ]

    selected = TaskScopedSelector().select(
        task=_task(("a", "b")),
        agent_id="reviewer",
        store=store,
        available_refs=refs,
    )

    assert [block.block_id for block in selected] == [
        "task:review",
        "task-output:a",
        "task-output:b",
    ]
    assert selected[0].kind is ContextKind.TASK
    assert selected[0].content == "compare outputs"
    assert selected[0].owner_agent_id == "reviewer"
    assert {block.task_id for block in selected[1:]} == {"a", "b"}


def test_reversed_arrival_order_selects_the_same_logical_blocks() -> None:
    store = InMemoryContextStore()
    store.put(_output("task-output:a", "a", "alpha"))
    store.put(_output("task-output:b", "b", "beta"))
    refs = [
        ContextRef(block_id="task-output:a", version=1),
        ContextRef(block_id="task-output:b", version=1),
    ]

    forward = TaskScopedSelector().select(
        task=_task(), agent_id="reviewer", store=store, available_refs=refs
    )
    backward = TaskScopedSelector().select(
        task=_task(),
        agent_id="reviewer",
        store=store,
        available_refs=list(reversed(refs)),
    )

    assert {block.block_id for block in forward} == {block.block_id for block in backward}
    assert forward[0] == backward[0]  # the task block is identical


def test_selector_does_not_invent_blocks_for_missing_dependencies() -> None:
    store = InMemoryContextStore()

    selected = TaskScopedSelector().select(
        task=_task(("a", "b")),
        agent_id="reviewer",
        store=store,
        available_refs=[],
    )

    assert [block.kind for block in selected] == [ContextKind.TASK]
