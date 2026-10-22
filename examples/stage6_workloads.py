"""Deterministic Stage 6 benchmark workloads, aggregations, and result models.

Experiment-only code: no runtime feature is added here. Workloads are constructed from the
existing Atlas workflow/context/placement models so the frozen Stage 0-5 paths are unchanged.
"""

from __future__ import annotations

import hashlib
import statistics
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict

from atlasos.context import ContextBlock, ContextKind, ContextRef, context_layout_hash
from atlasos.context.requirements import ContextRequirement
from atlasos.workflow import AtlasAgentSpec, AtlasTaskSpec, AtlasWorkflow

SCALE_CHARS: dict[str, int] = {"small": 30_000, "medium": 110_000, "large": 250_000}
DUPLICATE_TARGETS: dict[str, float] = {"D0": 0.0, "D1": 0.15, "D2": 0.30, "D3": 0.50}
UNIQUE_BLOCKS = 20

SYSTEM = (
    "You are a cache-stress analyst. Answer only from the supplied documents, cite document "
    "ids, and keep the answer under one hundred words."
)
QUERY = "Summarize the caching guidance in the supplied documents."


@dataclass(frozen=True)
class DedupWorkload:
    scale: str
    duplicate_label: str
    target_duplicate_ratio: float
    actual_duplicate_ratio: float
    total_chars: int
    unique_block_count: int
    duplicate_block_count: int
    blocks: tuple[ContextBlock, ...]

    @property
    def duplicate_chars(self) -> int:
        return int(round(self.actual_duplicate_ratio * self.total_chars))


def _payload(tag: str, chars: int) -> str:
    """Multi-line deterministic document.

    ContextPilot's official deduplication chunks text on line boundaries; a single-line block
    would bypass its block table, so the workload is line-structured. Exact duplicate blocks
    remain byte-identical and therefore produce exact duplicate chunks.
    """

    line_count = max(8, chars // 110)
    line_width = max(60, chars // line_count)
    lines: list[str] = []
    for index in range(line_count):
        prefix = f"[{tag}-{index:03d}] "
        sentence = (
            "Stable prefixes reduce repeated prefill cost; context selection stays explicit; "
            "measurements never change the model prompt. "
        )
        repeats = max(1, (line_width - len(prefix)) // len(sentence) + 1)
        lines.append((prefix + sentence * repeats)[:line_width])
    return "\n".join(lines)[:chars]


def dedup_workload(scale: str, duplicate_label: str) -> DedupWorkload:
    """Exact-duplicate blocks at a requested scale and duplicate ratio.

    Duplicates are byte-identical copies of distinct blocks, so ContextPilot's official exact
    deduplication is the only mechanism that can remove them. The actual ratio is reported per
    workload instead of pretending the requested ratio is exact.
    """

    if scale not in SCALE_CHARS:
        raise ValueError(f"unknown scale {scale!r}")
    if duplicate_label not in DUPLICATE_TARGETS:
        raise ValueError(f"unknown duplicate ratio {duplicate_label!r}")

    target_ratio = DUPLICATE_TARGETS[duplicate_label]
    duplicate_count = round(UNIQUE_BLOCKS * target_ratio / (1.0 - target_ratio))
    block_count = UNIQUE_BLOCKS + duplicate_count
    block_chars = SCALE_CHARS[scale] // block_count

    unique_blocks = [
        ContextBlock(
            block_id=f"stress:{scale}:{index:02d}",
            kind=ContextKind.DEPENDENCY_OUTPUT,
            content=_payload(f"{scale}-{index:02d}", block_chars),
            version=1,
            source="atlasos_stage6_stress",
            task_id=f"stress-{scale}-{index:02d}",
            owner_agent_id="stress-agent",
        )
        for index in range(UNIQUE_BLOCKS)
    ]
    blocks = list(unique_blocks)
    for index in range(duplicate_count):
        source = unique_blocks[index % UNIQUE_BLOCKS]
        blocks.append(
            ContextBlock(
                block_id=f"stress:{scale}:dup{index:02d}",
                kind=ContextKind.DEPENDENCY_OUTPUT,
                content=source.content,
                version=1,
                source="atlasos_stage6_stress",
                task_id=f"stress-{scale}-dup{index:02d}",
                owner_agent_id="stress-agent",
            )
        )

    total_chars = sum(len(block.content) for block in blocks)
    duplicate_chars = sum(
        len(blocks[UNIQUE_BLOCKS + index].content) for index in range(duplicate_count)
    )
    return DedupWorkload(
        scale=scale,
        duplicate_label=duplicate_label,
        target_duplicate_ratio=target_ratio,
        actual_duplicate_ratio=duplicate_chars / total_chars if total_chars else 0.0,
        total_chars=total_chars,
        unique_block_count=UNIQUE_BLOCKS,
        duplicate_block_count=duplicate_count,
        blocks=tuple(blocks),
    )


def layout_hash(blocks: tuple[ContextBlock, ...] | list[ContextBlock]) -> str:
    refs = tuple(ContextRef(block_id=block.block_id, version=block.version) for block in blocks)
    return context_layout_hash(refs)


def content_hashes(blocks: tuple[ContextBlock, ...] | list[ContextBlock]) -> list[str]:
    return [hashlib.sha256(block.content.encode("utf-8")).hexdigest() for block in blocks]


def describe_stats(values: list[float]) -> dict[str, float]:
    if not values:
        return {"mean": 0.0, "median": 0.0, "min": 0.0, "max": 0.0, "std": 0.0}
    return {
        "mean": statistics.fmean(values),
        "median": statistics.median(values),
        "min": min(values),
        "max": max(values),
        "std": statistics.pstdev(values) if len(values) > 1 else 0.0,
    }


def weighted_cache_hit_ratio(hit_tokens: int, prompt_tokens: int) -> float | None:
    if prompt_tokens <= 0:
        return None
    return hit_tokens / prompt_tokens


def paired_miss_reduction(identity_miss: int, dedup_miss: int) -> float | None:
    if identity_miss <= 0:
        return None
    return (identity_miss - dedup_miss) / identity_miss


def task_context_requirement(source_task_id: str) -> ContextRequirement:
    return ContextRequirement(f"task-output:{source_task_id}", source_task_id)


def parallel_chain_workflow(chain_length: int) -> AtlasWorkflow:
    """Two interchangeable research chains of ``chain_length`` tasks plus a merge."""

    workers = ("worker_a", "worker_b", "worker_c")
    tasks: list[AtlasTaskSpec] = []
    for prefix, offset in (("a", 0), ("b", 1)):
        previous: str | None = None
        for index in range(1, chain_length + 1):
            task_id = f"{prefix}{index}"
            tasks.append(
                AtlasTaskSpec(
                    task_id=task_id,
                    title=f"{prefix.upper()}{index}",
                    description=f"Chain {prefix.upper()} step {index}.",
                    assignee=workers[(index - 1 + offset) % len(workers)],
                    depends_on=() if previous is None else (previous,),
                    required_capabilities=frozenset({"research"}),
                    required_context=(
                        () if previous is None else (task_context_requirement(previous),)
                    ),
                )
            )
            previous = task_id

    tasks.append(
        AtlasTaskSpec(
            task_id="merge",
            title="Merge",
            description="Merge both chains.",
            assignee="reviewer",
            depends_on=(f"a{chain_length}", f"b{chain_length}"),
            required_capabilities=frozenset({"review"}),
            required_context=(
                task_context_requirement(f"a{chain_length}"),
                task_context_requirement(f"b{chain_length}"),
            ),
        )
    )
    return AtlasWorkflow(
        name=f"placement-p{chain_length + 1}",
        agents=(
            AtlasAgentSpec("worker_a", "worker", "worker", frozenset({"research"})),
            AtlasAgentSpec("worker_b", "worker", "worker", frozenset({"research"})),
            AtlasAgentSpec("worker_c", "worker", "worker", frozenset({"research"})),
            AtlasAgentSpec("reviewer", "reviewer", "reviewer", frozenset({"review"})),
        ),
        tasks=tuple(tasks),
    )


class FormalMeta(BaseModel):
    """Metadata every formal Stage 6 record must carry."""

    model_config = ConfigDict(extra="forbid")

    experiment: str
    variant: str
    workload: str
    scale: str | None = None
    duplicate_ratio: float | None = None

    run_id: str
    git_commit: str | None
    git_dirty: bool

    model: str
    provider: str
    placement_policy: str
    context_policy: str
    renderer: str
    context_transform: str

    success: bool


class DeterministicRecord(FormalMeta):
    pass


class LiveObservationRecord(FormalMeta):
    prompt_tokens: int
    completion_tokens: int
    cache_hit_tokens: int | None = None
    cache_miss_tokens: int | None = None
    provider_latency_ms: float
    transform_latency_ms: float
    original_context_chars: int
    optimized_context_chars: int
    deduplicated: bool
    repetition: int


def record_meta(
    *,
    experiment: str,
    variant: str,
    workload: str,
    run_id: str,
    git_commit: str | None,
    git_dirty: bool,
    model: str,
    placement_policy: str,
    context_policy: str,
    renderer: str,
    context_transform: str,
    success: bool,
    scale: str | None = None,
    duplicate_ratio: float | None = None,
    provider: str = "deepseek",
) -> dict[str, Any]:
    return {
        "experiment": experiment,
        "variant": variant,
        "workload": workload,
        "scale": scale,
        "duplicate_ratio": duplicate_ratio,
        "run_id": run_id,
        "git_commit": git_commit,
        "git_dirty": git_dirty,
        "model": model,
        "provider": provider,
        "placement_policy": placement_policy,
        "context_policy": context_policy,
        "renderer": renderer,
        "context_transform": context_transform,
        "success": success,
    }
