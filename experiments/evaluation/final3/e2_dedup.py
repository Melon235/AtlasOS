"""Final-3 E2: measure the real ContextPilot dedup transform in an isolated environment.

Run with the E2 interpreter (never the main ``.venv``):

    <E2_VENV>\\Scripts\\python.exe -m experiments.evaluation.final3.e2_dedup

Baseline is the identity transform, treatment is the shipped
``ContextPilotDedupTransform``. The workload is new (not the historical Stage 6 one): dependency
outputs of realistic size with a controlled exact-duplicate ratio plus one unique tail fact that
must survive.
"""

from __future__ import annotations

import json

from atlasos.context.models import ContextBlock, ContextKind
from atlasos.context.transform import (
    ContextPilotDedupTransform,
    IdentityContextTransform,
    context_chars,
)


def _blocks(duplicate_ratio: float, *, total: int = 12, size: int = 900) -> list[ContextBlock]:
    unique_count = max(1, int(round(total * (1.0 - duplicate_ratio))))
    unique = [f"UNIQUE-{index}: " + ("a" * size) for index in range(unique_count)]
    duplicated = unique[0]
    blocks: list[ContextBlock] = []
    for index in range(total):
        content = unique[index] if index < unique_count else duplicated
        blocks.append(
            ContextBlock(
                block_id=f"task-output:t{index}",
                kind=ContextKind.DEPENDENCY_OUTPUT,
                content=content,
                version=1,
                source="final3-e2",
                task_id=f"t{index}",
            )
        )
    return blocks


def measure() -> dict[str, object]:
    identity = IdentityContextTransform()
    dedup = ContextPilotDedupTransform()
    cells: list[dict[str, object]] = []
    for ratio in (0.0, 0.25, 0.5):
        blocks = _blocks(ratio)
        baseline = identity.transform(blocks=blocks, query="dependency summary")
        treatment = dedup.transform(blocks=blocks, query="dependency summary")
        cells.append(
            {
                "duplicate_ratio": ratio,
                "blocks": len(blocks),
                "baseline_blocks": baseline.optimized_block_count,
                "treatment_blocks": treatment.optimized_block_count,
                "baseline_chars": context_chars(baseline.blocks),
                "treatment_chars": context_chars(treatment.blocks),
                "char_reduction": round(
                    1.0 - context_chars(treatment.blocks) / max(1, context_chars(baseline.blocks)),
                    4,
                ),
                "transform_name": treatment.transform_name,
                "deduplicated": treatment.deduplicated,
                "latency_ms": treatment.transform_latency_ms,
                "unique_fact_preserved": any(
                    "UNIQUE-0" in block.content for block in treatment.blocks
                ),
            }
        )
    return {"transform": dedup.name, "cells": cells}


if __name__ == "__main__":
    print(json.dumps(measure(), indent=2, sort_keys=True))
