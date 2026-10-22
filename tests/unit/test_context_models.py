from __future__ import annotations

import pytest

from atlasos.context import ContextBlock, ContextKind, ContextRef, InMemoryContextStore


def _block(block_id: str = "task-output:a", version: int = 1, content: str = "x") -> ContextBlock:
    return ContextBlock(
        block_id=block_id,
        kind=ContextKind.DEPENDENCY_OUTPUT,
        content=content,
        version=version,
        task_id="a",
    )


def test_block_keeps_identity_and_version() -> None:
    ref = ContextRef(block_id="task-output:a", version=3)
    block = _block(version=3)

    assert ref.block_id == block.block_id
    assert ref.version == block.version == 3


def test_versions_are_preserved_immutably() -> None:
    store = InMemoryContextStore()
    store.put(_block(version=1, content="first"))
    store.put(_block(version=2, content="second"))

    assert store.get("task-output:a").content == "second"
    resolved = store.get_many([ContextRef(block_id="task-output:a", version=1)])
    assert resolved[0].content == "first"


def test_missing_block_raises() -> None:
    store = InMemoryContextStore()

    with pytest.raises(KeyError, match="unknown context block"):
        store.get("nope")

    with pytest.raises(KeyError, match="unknown context block version"):
        store.get_many([ContextRef(block_id="nope", version=1)])


def test_invalid_version_is_rejected() -> None:
    store = InMemoryContextStore()

    with pytest.raises(ValueError, match="version must be >= 1"):
        store.put(_block(version=0))


def test_find_by_task_returns_all_versions_in_order() -> None:
    store = InMemoryContextStore()
    store.put(_block(version=1, content="first"))
    store.put(_block(version=2, content="second"))
    store.put(
        ContextBlock(
            block_id="task-output:b",
            kind=ContextKind.DEPENDENCY_OUTPUT,
            content="other",
            task_id="b",
        )
    )

    blocks = store.find_by_task("a")

    assert [(block.block_id, block.version) for block in blocks] == [
        ("task-output:a", 1),
        ("task-output:a", 2),
    ]
