"""Final-4 D0–D3: bounded ContextPilot diagnosis (no algorithm change).

Run with the isolated E2 interpreter:

    <E2_VENV>\\Scripts\\python.exe -m experiments.evaluation.final4.contextpilot_diagnosis

Four cells separate *payload shape* from *adapter behaviour*:

* D0 — historical-like multi-line payload, raw ``dedup_chat_completions``
* D1 — the same payload through ``ContextPilotDedupTransform``
* D2 — Final-3 single-line payload, raw API
* D3 — Final-3 single-line payload, adapter

The API's contract (read from the installed 0.5.0 source, not modified): each message is split by a
line-based content-defined chunker; a message with fewer than two chunks takes a "record only" path,
and only chunks of at least ``MIN_BLOCK_CHARS = 40`` are hashed. A single very long line can
never be deduplicated, no matter how many copies of it exist.
"""

from __future__ import annotations

import json

from atlasos.context.models import ContextBlock, ContextKind
from atlasos.context.transform import ContextPilotDedupTransform, context_chars


def _multiline(index: int) -> str:
    """A realistic tool-output block: many lines, each long enough to be chunked."""

    header = f"[task-output t{index}] pytest run {index}"
    body = [
        f"      {line:03d} passed test_module_{index}_{line} in 0.{line:03d}s"
        for line in range(1, 13)
    ]
    return "\n".join([header, *body])


def _singleline(index: int) -> str:
    return f"UNIQUE-{index}: " + ("a" * 880)


def _blocks(shape: str, count: int, duplicate_ratio: float) -> list[ContextBlock]:
    unique_count = max(1, int(round(count * (1.0 - duplicate_ratio))))
    make = _multiline if shape == "multiline" else _singleline
    unique = [make(i) for i in range(unique_count)]
    blocks: list[ContextBlock] = []
    for index in range(count):
        content = unique[index] if index < unique_count else unique[0]
        blocks.append(
            ContextBlock(
                block_id=f"task-output:t{index}",
                kind=ContextKind.DEPENDENCY_OUTPUT,
                content=content,
                version=1,
                source="final4-diagnosis",
                task_id=f"t{index}",
            )
        )
    return blocks


def _raw(blocks: list[ContextBlock]) -> dict[str, object]:
    from contextpilot import dedup_chat_completions

    body = {
        "model": "atlasos-context",
        "messages": [{"role": "user", "content": block.content} for block in blocks],
    }
    before = sum(len(message["content"]) for message in body["messages"])
    result = dedup_chat_completions(body)
    after = sum(len(message["content"]) for message in body["messages"])
    return {
        "messages_in": len(blocks),
        "blocks_deduped": result.blocks_deduped,
        "blocks_total": result.blocks_total,
        "chars_before_reported": result.chars_before,
        "chars_saved_reported": result.chars_saved,
        "chars_before_measured": before,
        "chars_after_measured": after,
        "content_changed": before != after,
    }


def _unique_markers(blocks: list[ContextBlock]) -> dict[str, object]:
    """Every distinct source block must still be reachable after the transform."""

    headers = {
        line
        for block in blocks
        for line in block.content.split("\n")
        if line.startswith("[task-output") or line.startswith("UNIQUE-")
    }
    joined = "\n".join(block.content for block in blocks)
    present = {header for header in headers if header in joined}
    return {"unique_markers_expected": len(headers), "unique_markers_present": len(present)}


def _adapter(blocks: list[ContextBlock]) -> dict[str, object]:
    transform = ContextPilotDedupTransform()
    result = transform.transform(blocks=blocks, query="dependency summary")
    preserved = _unique_markers(list(result.blocks))
    return {
        "blocks_in": result.original_block_count,
        "blocks_out": result.optimized_block_count,
        "deduplicated": result.deduplicated,
        "chars_before": context_chars(blocks),
        "chars_after": result.optimized_context_chars,
        "char_reduction": round(
            1.0 - result.optimized_context_chars / max(1, context_chars(blocks)), 4
        ),
        "latency_ms": round(result.transform_latency_ms, 3),
        "transform_name": result.transform_name,
        **preserved,
    }


def run() -> dict[str, object]:
    historical = _blocks("multiline", 12, 0.5)
    final3 = _blocks("singleline", 12, 0.5)
    return {
        "environment": "E2",
        "cells": {
            "D0_raw_multiline": _raw(historical),
            "D1_adapter_multiline": _adapter(historical),
            "D2_raw_singleline": _raw(final3),
            "D3_adapter_singleline": _adapter(final3),
        },
    }


if __name__ == "__main__":
    print(json.dumps(run(), indent=2, sort_keys=True))
