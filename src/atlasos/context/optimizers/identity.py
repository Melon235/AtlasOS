"""Identity optimizer: the Stage 2 stable baseline."""

from __future__ import annotations

import time
from collections.abc import Sequence

from atlasos.context.models import ContextBlock
from atlasos.context.optimizers.base import OptimizedContext, context_chars


class IdentityContextOptimizer:
    name = "identity"

    def optimize(
        self,
        *,
        blocks: Sequence[ContextBlock],
        query: str,
    ) -> OptimizedContext:
        del query
        started = time.perf_counter()
        ordered = tuple(blocks)
        latency_ms = (time.perf_counter() - started) * 1000.0
        chars = context_chars(ordered)
        return OptimizedContext(
            blocks=ordered,
            original_block_count=len(ordered),
            optimized_block_count=len(ordered),
            reordered=False,
            deduplicated=False,
            content_transformed=False,
            optimizer_name=self.name,
            optimizer_latency_ms=latency_ms,
            original_context_chars=chars,
            optimized_context_chars=chars,
            metadata={"reorder_enabled": False, "deduplicate_enabled": False},
        )
