from __future__ import annotations

from atlasos.context import ContextBlock, ContextKind, ContextRef, InMemoryContextStore


def _output(block_id: str, task_id: str, content: str) -> ContextBlock:
    return ContextBlock(
        block_id=block_id,
        kind=ContextKind.DEPENDENCY_OUTPUT,
        content=content,
        task_id=task_id,
    )


def test_put_get_roundtrip() -> None:
    store = InMemoryContextStore()
    block = _output("task-output:a", "a", "alpha")
    store.put(block)

    assert store.get("task-output:a") == block


def test_get_many_preserves_reference_order() -> None:
    store = InMemoryContextStore()
    store.put(_output("task-output:a", "a", "alpha"))
    store.put(_output("task-output:b", "b", "beta"))

    resolved = store.get_many(
        [
            ContextRef(block_id="task-output:b", version=1),
            ContextRef(block_id="task-output:a", version=1),
        ]
    )

    assert [block.content for block in resolved] == ["beta", "alpha"]


def test_find_by_task_filters_other_tasks() -> None:
    store = InMemoryContextStore()
    store.put(_output("task-output:a", "a", "alpha"))
    store.put(_output("task-output:b", "b", "beta"))

    assert [block.content for block in store.find_by_task("a")] == ["alpha"]
    assert store.find_by_task("missing") == []


def test_putting_the_same_version_replaces_it() -> None:
    store = InMemoryContextStore()
    store.put(_output("task-output:a", "a", "first"))
    store.put(_output("task-output:a", "a", "second"))

    assert store.get("task-output:a").content == "second"
