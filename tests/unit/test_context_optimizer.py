"""Optimizer boundary tests: identity and the ContextPilot adapter."""

from __future__ import annotations

import importlib.util
import sys
import types
from typing import Any

import pytest

from atlasos.context import ContextBlock, ContextKind
from atlasos.context.optimizers import (
    ContextPilotOptimizer,
    ContextPilotUnavailable,
    IdentityContextOptimizer,
)


def _block(name: str, content: str | None = None) -> ContextBlock:
    return ContextBlock(
        block_id=f"task-output:{name}",
        kind=ContextKind.DEPENDENCY_OUTPUT,
        content=content or f"content-{name}",
        task_id=name,
        version=1,
    )


def test_identity_returns_the_same_blocks_in_order() -> None:
    blocks = [_block("a"), _block("b")]

    result = IdentityContextOptimizer().optimize(blocks=blocks, query="q")

    assert result.optimizer_name == "identity"
    assert result.blocks == tuple(blocks)
    assert result.original_block_count == result.optimized_block_count == 2
    assert result.reordered is False
    assert result.deduplicated is False
    assert result.original_context_chars == result.optimized_context_chars
    assert result.optimizer_latency_ms >= 0


class _FakeContextPilot:
    """Minimal stand-in for the official reorder API shape."""

    def __init__(self, **_: Any) -> None:
        pass

    def reorder(self, contexts: list[list[str]], conversation_id: str | None = None):
        del conversation_id
        reordered = [list(reversed(context)) for context in contexts]
        return reordered, list(range(len(contexts)))


def test_reorder_maps_official_output_back_to_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = types.SimpleNamespace(ContextPilot=_FakeContextPilot)
    monkeypatch.setitem(sys.modules, "contextpilot", fake)
    optimizer = ContextPilotOptimizer(reorder=True, deduplicate=False, conversation_id="test")

    result = optimizer.optimize(blocks=[_block("a"), _block("b")], query="q")

    assert [block.block_id for block in result.blocks] == [
        "task-output:b",
        "task-output:a",
    ]
    assert result.reordered is True
    assert result.deduplicated is False
    assert result.optimized_block_count == 2
    assert result.metadata["reorder_enabled"] is True
    assert result.metadata["deduplicate_enabled"] is False


def test_missing_contextpilot_raises_instead_of_falling_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "contextpilot", None)
    optimizer = ContextPilotOptimizer(reorder=True, deduplicate=False)

    with pytest.raises(ContextPilotUnavailable, match="not installed"):
        optimizer.optimize(blocks=[_block("a")], query="q")


def test_contextpilot_requires_at_least_one_feature() -> None:
    with pytest.raises(ValueError, match="reorder=True or deduplicate=True"):
        ContextPilotOptimizer(reorder=False, deduplicate=False)


def _duplicate_content() -> str:
    return "\n".join(
        f"paragraph {i}: the quick brown fox jumps over the lazy dog in a stable line"
        for i in range(60)
    )


@pytest.mark.skipif(
    importlib.util.find_spec("contextpilot") is None,
    reason="contextpilot extra not installed",
)
def test_deduplicate_replaces_exact_duplicate_content() -> None:
    content = _duplicate_content()
    blocks = [_block("a", content), _block("b", content)]

    result = ContextPilotOptimizer(reorder=False, deduplicate=True).optimize(
        blocks=blocks, query="q"
    )

    assert result.optimized_block_count == 2
    assert result.content_transformed is True
    assert result.deduplicated is True
    assert result.optimized_context_chars < result.original_context_chars
    assert result.blocks[0].content == content  # first copy kept
    assert result.blocks[1].content != content  # second replaced by reference hint
    assert "identical to earlier" in result.blocks[1].content
    # Block identity is preserved even though content changed.
    assert result.blocks[1].block_id == "task-output:b"


def test_deduplicate_keeps_distinct_content() -> None:
    first = "\n".join(f"alpha line {i} " + "x" * 60 for i in range(60))
    second = "\n".join(f"beta line {i} " + "y" * 60 for i in range(60))
    result = ContextPilotOptimizer(reorder=False, deduplicate=True).optimize(
        blocks=[_block("a", first), _block("b", second)], query="q"
    )

    assert result.content_transformed is False
    assert [block.content for block in result.blocks] == [first, second]
