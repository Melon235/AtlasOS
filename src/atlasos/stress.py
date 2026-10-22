"""Deterministic cache-stress workload for Stage 3.

No network, no LLM generation: the same blocks and queries are produced on every call. Block E
is an exact copy of block A so exact-block deduplication has something measurable to remove.
"""

from __future__ import annotations

from dataclasses import dataclass

from atlasos.context import ContextBlock, ContextKind

SCALES: dict[str, int] = {"small": 45, "medium": 90, "large": 180}

_SHARED_HEADER = (
    "ATLAS-STRESS SHARED POLICY: every answer must be grounded in the supplied documents, "
    "cite the document id, avoid speculation, and keep the response under one hundred words. "
    "This header is identical across documents so prefix reuse is observable."
)


@dataclass(frozen=True)
class StressRequest:
    request_id: str
    block_ids: tuple[str, ...]
    query: str


REQUESTS: tuple[StressRequest, ...] = (
    StressRequest("r1", ("A", "B", "C"), "Summarize the caching guidance in these documents."),
    StressRequest("r2", ("D", "A", "B"), "Which document discusses scheduling and why?"),
    StressRequest("r3", ("C", "A", "B"), "Compare the eviction guidance across documents."),
    StressRequest("r4", ("E", "A", "B"), "What does the repeated document add?"),
)


def _document(block_id: str, lines: int) -> str:
    body = "\n".join(
        f"[{block_id}-{index:03d}] "
        f"Document {block_id} states that stable prefixes reduce repeated prefill cost, "
        f"that context selection must stay explicit, and that measurement {index} is "
        f"recorded without changing model prompts."
        for index in range(lines)
    )
    return f"{_SHARED_HEADER}\n\nDocument {block_id}:\n{body}"


def cache_stress_blocks(scale: str = "small") -> dict[str, ContextBlock]:
    """Five deterministic blocks, each roughly ``lines`` sentences long."""

    if scale not in SCALES:
        raise ValueError(f"unknown stress scale {scale!r}; choose from {sorted(SCALES)}")
    line_count = SCALES[scale]

    blocks: dict[str, ContextBlock] = {}
    for block_id in ("A", "B", "C", "D", "E"):
        content = _document(block_id, line_count)
        blocks[block_id] = ContextBlock(
            block_id=f"stress:{block_id}",
            kind=ContextKind.DEPENDENCY_OUTPUT,
            content=content,
            version=1,
            source="atlasos_cache_stress",
            task_id=block_id,
            owner_agent_id="stress-agent",
        )
    # E is an exact copy of A: deduplication should shorten the later copy.
    blocks["E"] = ContextBlock(
        block_id="stress:E",
        kind=ContextKind.DEPENDENCY_OUTPUT,
        content=blocks["A"].content,
        version=1,
        source="atlasos_cache_stress",
        task_id="E",
        owner_agent_id="stress-agent",
    )
    return blocks
