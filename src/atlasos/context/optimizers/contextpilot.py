"""Official ContextPilot 0.5.0 adapter.

Only reorder and exact-block deduplication are used; ContextPilot's request/batch scheduling is
deliberately not applied. The backend is imported lazily so the identity baseline runs without
the optional heavy dependency, and initialization failures are surfaced instead of silently
falling back.
"""

from __future__ import annotations

import io
import time
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, redirect_stdout
from dataclasses import replace
from typing import Any

from atlasos.context.models import ContextBlock
from atlasos.context.optimizers.base import OptimizedContext, context_chars


class ContextPilotUnavailable(RuntimeError):
    """ContextPilot is not installed or could not initialize."""


class ContextPilotIntegrationError(RuntimeError):
    """The installed ContextPilot API returned a shape AtlasOS cannot map safely."""


@contextmanager
def _silence_vendor_output() -> Iterator[None]:
    """ContextPilot prints progress banners; keep experiment stdout readable."""

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        yield


class ContextPilotOptimizer:
    """Reorder and/or deduplicate Atlas context blocks with the official implementation."""

    name = "contextpilot"

    def __init__(
        self,
        *,
        reorder: bool,
        deduplicate: bool,
        conversation_id: str | None = None,
        index_mode: str = "warm_within_run",
    ) -> None:
        if not reorder and not deduplicate:
            raise ValueError("ContextPilotOptimizer needs reorder=True or deduplicate=True")
        self._reorder = reorder
        self._deduplicate = deduplicate
        self._conversation_id = conversation_id or f"atlasos-{uuid.uuid4().hex}"
        self._index_mode = index_mode
        self._backend: Any = None

    @property
    def conversation_id(self) -> str:
        return self._conversation_id

    @property
    def index_mode(self) -> str:
        return self._index_mode

    def _backend_instance(self) -> Any:
        if self._backend is not None:
            return self._backend
        try:
            import contextpilot
        except ImportError as exc:  # pragma: no cover - exercised via monkeypatch in tests
            raise ContextPilotUnavailable(
                "contextpilot==0.5.0 is not installed; install the 'contextpilot' extra"
            ) from exc
        try:
            with _silence_vendor_output():
                self._backend = contextpilot.ContextPilot(use_gpu=False)
        except Exception as exc:  # noqa: BLE001 - surface vendor init failure
            raise ContextPilotUnavailable(f"ContextPilot initialization failed: {exc}") from exc
        return self._backend

    def reset(self) -> None:
        """Drop the backend so the next call starts with a fresh context index."""

        self._backend = None

    def optimize(
        self,
        *,
        blocks: Sequence[ContextBlock],
        query: str,
    ) -> OptimizedContext:
        return self.optimize_batch(contexts=[blocks], queries=[query])[0]

    def optimize_batch(
        self,
        *,
        contexts: Sequence[Sequence[ContextBlock]],
        queries: Sequence[str],
    ) -> list[OptimizedContext]:
        """Optimize several request contexts in one official reorder call.

        ContextPilot's reorder is only meaningful across a batch of requests; a single context
        usually comes back unchanged. The returned execution order (``original_indices``) is
        intentionally ignored: request sequencing belongs to AtlasOS, not to ContextPilot.
        """

        if len(contexts) != len(queries):
            raise ValueError("contexts and queries must have the same length")
        backend = self._backend_instance()
        started = time.perf_counter()

        texts_per_context = [[block.content for block in context] for context in contexts]

        ordered_contents = texts_per_context
        reordered = False
        reorder_indices: list[Any] = []
        if self._reorder:
            with _silence_vendor_output():
                result, reorder_indices = backend.reorder(
                    texts_per_context,
                    conversation_id=self._conversation_id,
                )
            if len(result) != len(contexts):
                raise ContextPilotIntegrationError(
                    f"reorder returned {len(result)} contexts for {len(contexts)} inputs"
                )
            ordered_contents = [list(texts) for texts in result]
            reordered = ordered_contents != texts_per_context

        optimized: list[OptimizedContext] = []
        dedup_stats: dict[str, int] = {
            "blocks_total": 0,
            "blocks_deduped": 0,
            "chars_saved": 0,
        }
        for original, contents in zip(contexts, ordered_contents, strict=True):
            blocks = _remap_blocks(original, contents)
            transformed = False
            if self._deduplicate:
                blocks, stats = _deduplicate_blocks(blocks)
                transformed = any(
                    before.content != after.content
                    for before, after in zip(original, blocks, strict=True)
                )
                for key in dedup_stats:
                    dedup_stats[key] += stats[key]

            optimized.append(
                OptimizedContext(
                    blocks=blocks,
                    original_block_count=len(original),
                    optimized_block_count=len(blocks),
                    reordered=reordered,
                    deduplicated=transformed,
                    content_transformed=transformed,
                    optimizer_name=self.name,
                    optimizer_latency_ms=0.0,
                    original_context_chars=context_chars(original),
                    optimized_context_chars=context_chars(blocks),
                    metadata={
                        "reorder_enabled": self._reorder,
                        "deduplicate_enabled": self._deduplicate,
                        "index_mode": self._index_mode,
                        "conversation_id": self._conversation_id,
                        "batch_size": len(contexts),
                        "reorder_indices": reorder_indices,
                        "dedup": dedup_stats,
                    },
                )
            )

        elapsed_ms = (time.perf_counter() - started) * 1000.0
        per_context_ms = elapsed_ms / max(len(optimized), 1)
        return [replace(item, optimizer_latency_ms=per_context_ms) for item in optimized]


def _remap_blocks(
    original: Sequence[ContextBlock],
    reordered_contents: Sequence[str],
) -> tuple[ContextBlock, ...]:
    """Map reordered text back to original blocks by exact content.

    ContextPilot 0.5.0 does not return a per-document permutation, and its `original_indices`
    are request-order indices. Exact content matching is therefore the only faithful mapping;
    duplicate contents are consumed in first-seen order.
    """

    if len(original) != len(reordered_contents):
        raise ContextPilotIntegrationError(
            "reorder changed the number of documents; AtlasOS cannot map blocks safely"
        )
    by_content: dict[str, list[int]] = {}
    for index, block in enumerate(original):
        by_content.setdefault(block.content, []).append(index)

    mapped: list[ContextBlock] = []
    for content in reordered_contents:
        candidates = by_content.get(content)
        if not candidates:
            raise ContextPilotIntegrationError(
                "reorder returned content that does not match any input block"
            )
        block = original[candidates.pop(0)]
        mapped.append(replace(block, content=content))
    return tuple(mapped)


def _deduplicate_blocks(
    blocks: Sequence[ContextBlock],
) -> tuple[tuple[ContextBlock, ...], dict[str, int]]:
    """Run the official exact-block deduplicator over the selected blocks."""

    from contextpilot import dedup_chat_completions

    body = {
        "model": "atlasos-context",
        "messages": [{"role": "user", "content": block.content} for block in blocks],
    }
    result = dedup_chat_completions(body)
    updated = tuple(
        replace(block, content=message["content"])
        for block, message in zip(blocks, body["messages"], strict=True)
    )
    stats = {
        "blocks_total": int(getattr(result, "blocks_total", 0)),
        "blocks_deduped": int(getattr(result, "blocks_deduped", 0)),
        "chars_saved": int(getattr(result, "chars_saved", 0)),
    }
    return updated, stats
