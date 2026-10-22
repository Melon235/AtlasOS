"""Optimizer boundary: WHAT survives selection is fixed; HOW it is organized may change.

Optimizers see the stable-ordered selected blocks and may reorder them and/or replace exact
duplicate content. They never own the stable system/task prefix, never render, never call a
provider, and never select blocks.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from atlasos.context.models import ContextBlock


@dataclass(frozen=True)
class OptimizedContext:
    """Result of one optimizer invocation.

    ``blocks`` may carry transformed content (deduplication replaces repeated text with the
    official reference hints). Block ids and versions are preserved so AtlasOS can still render
    and fingerprint the layout honestly; ``content_transformed`` says whether text changed.
    """

    blocks: tuple[ContextBlock, ...]

    original_block_count: int
    optimized_block_count: int

    reordered: bool
    deduplicated: bool
    content_transformed: bool

    optimizer_name: str
    optimizer_latency_ms: float

    original_context_chars: int
    optimized_context_chars: int

    metadata: Mapping[str, Any] = field(default_factory=dict)


class ContextOptimizer(Protocol):
    name: str

    def optimize(
        self,
        *,
        blocks: Sequence[ContextBlock],
        query: str,
    ) -> OptimizedContext: ...


def context_chars(blocks: Sequence[ContextBlock]) -> int:
    return sum(len(block.content) for block in blocks)
