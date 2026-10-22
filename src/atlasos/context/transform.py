"""Per-request context transformation.

A transform sees the stable-ordered selected blocks of one request and may change their content
(deduplication) or leave them untouched. It must not reorder blocks for cache reuse, change the
Atlas stable prefix, render prompts, call providers, or schedule requests.

Batch-level reordering lives in ``atlasos.experimental.batch``; ContextPilot's ``reorder()`` is a
cross-request primitive and is deliberately not part of this interface.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from atlasos.context.models import ContextBlock


class ContextPilotUnavailable(RuntimeError):
    """ContextPilot is not installed or could not be used."""


@dataclass(frozen=True)
class TransformedContext:
    """Result of one per-request transform."""

    blocks: tuple[ContextBlock, ...]

    original_block_count: int
    optimized_block_count: int

    deduplicated: bool
    content_transformed: bool

    transform_name: str
    transform_latency_ms: float

    original_context_chars: int
    optimized_context_chars: int

    metadata: Mapping[str, Any] = field(default_factory=dict)


class ContextTransform(Protocol):
    name: str

    def transform(
        self,
        *,
        blocks: Sequence[ContextBlock],
        query: str,
    ) -> TransformedContext: ...


def context_chars(blocks: Sequence[ContextBlock]) -> int:
    return sum(len(block.content) for block in blocks)


class IdentityContextTransform:
    name = "identity"

    def transform(
        self,
        *,
        blocks: Sequence[ContextBlock],
        query: str,
    ) -> TransformedContext:
        del query
        started = time.perf_counter()
        ordered = tuple(blocks)
        latency_ms = (time.perf_counter() - started) * 1000.0
        chars = context_chars(ordered)
        return TransformedContext(
            blocks=ordered,
            original_block_count=len(ordered),
            optimized_block_count=len(ordered),
            deduplicated=False,
            content_transformed=False,
            transform_name=self.name,
            transform_latency_ms=latency_ms,
            original_context_chars=chars,
            optimized_context_chars=chars,
            metadata={"official_api": None},
        )


class ContextPilotDedupTransform:
    """Official exact-block deduplication over one request's context blocks."""

    name = "contextpilot_dedup"

    def transform(
        self,
        *,
        blocks: Sequence[ContextBlock],
        query: str,
    ) -> TransformedContext:
        del query
        started = time.perf_counter()
        try:
            from contextpilot import dedup_chat_completions
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise ContextPilotUnavailable(
                "contextpilot==0.5.0 is not installed; install the 'contextpilot' extra"
            ) from exc

        body = {
            "model": "atlasos-context",
            "messages": [{"role": "user", "content": block.content} for block in blocks],
        }
        result = dedup_chat_completions(body)
        updated = tuple(
            replace(block, content=message["content"])
            for block, message in zip(blocks, body["messages"], strict=True)
        )
        latency_ms = (time.perf_counter() - started) * 1000.0
        transformed = any(
            before.content != after.content for before, after in zip(blocks, updated, strict=True)
        )
        return TransformedContext(
            blocks=updated,
            original_block_count=len(blocks),
            optimized_block_count=len(updated),
            deduplicated=transformed,
            content_transformed=transformed,
            transform_name=self.name,
            transform_latency_ms=latency_ms,
            original_context_chars=context_chars(blocks),
            optimized_context_chars=context_chars(updated),
            metadata={
                "official_api": "contextpilot.dedup_chat_completions",
                "blocks_total": int(getattr(result, "blocks_total", 0)),
                "blocks_deduped": int(getattr(result, "blocks_deduped", 0)),
                "chars_saved": int(getattr(result, "chars_saved", 0)),
            },
        )
