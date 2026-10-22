"""Per-request transform and batch-policy semantics."""

from __future__ import annotations

import sys

import pytest

from atlasos.context import ContextBlock, ContextKind
from atlasos.context.transform import (
    ContextPilotUnavailable,
    IdentityContextTransform,
    TransformedContext,
)
from atlasos.experimental.batch import (
    BatchPolicyUnavailable,
    ContextPilotReorderPolicy,
    IdentityBatchRequestPolicy,
    RenderedRequest,
)


def _block(name: str, content: str | None = None) -> ContextBlock:
    return ContextBlock(
        block_id=f"task-output:{name}",
        kind=ContextKind.DEPENDENCY_OUTPUT,
        content=content or f"content-{name}",
        task_id=name,
        version=1,
    )


def test_identity_transform_preserves_blocks() -> None:
    blocks = [_block("a"), _block("b")]

    result = IdentityContextTransform().transform(blocks=blocks, query="q")

    assert isinstance(result, TransformedContext)
    assert result.blocks == tuple(blocks)
    assert result.transform_name == "identity"
    assert result.deduplicated is False
    assert result.original_context_chars == result.optimized_context_chars


def _duplicate_content() -> str:
    return "\n".join(
        f"paragraph {i}: the quick brown fox jumps over the lazy dog in a stable line"
        for i in range(60)
    )


def test_contextpilot_dedup_transform_uses_official_api() -> None:
    content = _duplicate_content()
    result = (
        __import__("atlasos.context.transform", fromlist=["ContextPilotDedupTransform"])
        .ContextPilotDedupTransform()
        .transform(blocks=[_block("a", content), _block("b", content)], query="q")
    )

    assert result.transform_name == "contextpilot_dedup"
    assert result.metadata["official_api"] == "contextpilot.dedup_chat_completions"
    assert result.optimized_block_count == result.original_block_count == 2
    assert result.content_transformed is True
    assert result.optimized_context_chars < result.original_context_chars
    assert result.blocks[0].content == content
    assert "identical to earlier" in result.blocks[1].content


def test_dedup_transform_keeps_distinct_content() -> None:
    first = "\n".join(f"alpha line {i} " + "x" * 60 for i in range(60))
    second = "\n".join(f"beta line {i} " + "y" * 60 for i in range(60))

    from atlasos.context.transform import ContextPilotDedupTransform

    result = ContextPilotDedupTransform().transform(
        blocks=[_block("a", first), _block("b", second)], query="q"
    )

    assert result.content_transformed is False


def test_missing_contextpilot_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    from atlasos.context.transform import ContextPilotDedupTransform

    monkeypatch.setitem(sys.modules, "contextpilot", None)

    with pytest.raises(ContextPilotUnavailable, match="not installed"):
        ContextPilotDedupTransform().transform(blocks=[_block("a")], query="q")


def _request(request_id: str) -> RenderedRequest:
    return RenderedRequest(
        request_id=request_id,
        blocks=(_block(request_id),),
        messages=({"role": "user", "content": request_id},),
        prompt_hash=request_id,
    )


def test_identity_batch_policy_preserves_request_order() -> None:
    requests = [_request("r1"), _request("r2")]

    result = IdentityBatchRequestPolicy().order(requests)

    assert result.order == ("r1", "r2")
    assert result.reordered is False


def test_missing_contextpilot_batch_policy_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "contextpilot", None)

    with pytest.raises(BatchPolicyUnavailable, match="not installed"):
        ContextPilotReorderPolicy().order([_request("r1")])
