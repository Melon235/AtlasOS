"""Stage 3 closure: formal T0/T1 cache-stress runs plus functional checks.

    ATLASOS_RUN_LIVE_TESTS=1 uv run python examples/stage3_contextpilot.py

Writes one run-level record per repetition to experiments/runs/stage3-formal.jsonl.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from pathlib import Path

from anycode.types import LLMChatOptions, LLMMessage, TextBlock
from pydantic import BaseModel, ConfigDict

from atlasos.context import StablePromptRenderer
from atlasos.context.transform import ContextPilotDedupTransform, IdentityContextTransform
from atlasos.experiments import (
    ExperimentStore,
    git_commit,
    git_dirty,
    package_version,
)
from atlasos.integrations.deepseek import DeepSeekAdapter
from atlasos.runtime import AtlasRuntime
from atlasos.stress import REQUESTS, cache_stress_blocks
from atlasos.workflow import AtlasTaskSpec, baseline_workflow

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIGS = REPO_ROOT / "experiments" / "configs"
REPETITIONS = 3
SCALE = "small"
MODEL = "deepseek-chat"

FUNCTIONAL_INPUT = (
    "Two approaches are under consideration for a multi-agent runtime. "
    "Approach 1 aggressively prunes context before every model call. "
    "Approach 2 keeps a stable prefix and reorders context blocks for cache reuse."
)

SYSTEM = (
    "You are a cache-stress analyst. Answer only from the supplied documents, cite document "
    "ids, and keep the answer under one hundred words."
)

VARIANTS = (
    ("T0", "stage3-t0-identity.yaml", "identity"),
    ("T1", "stage3-t1-dedup.yaml", "contextpilot_dedup"),
)


class StressRunRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    timestamp: datetime
    variant: str
    repetition: int

    git_commit: str | None
    git_dirty: bool
    model: str
    anycode_version: str | None
    contextpilot_version: str | None

    transform_name: str
    scale: str
    request_sequence: list[str]

    prompt_tokens: int
    completion_tokens: int
    cache_hit_tokens: int | None
    cache_miss_tokens: int | None
    cache_hit_ratio: float | None

    provider_latency_ms: float
    transform_latency_ms: float
    original_context_chars: int
    optimized_context_chars: int
    task_success: bool


def _transform(name: str):
    return IdentityContextTransform() if name == "identity" else ContextPilotDedupTransform()


async def run_functional() -> None:
    for label, filename, _ in VARIANTS:
        runtime = AtlasRuntime.from_config(
            CONFIGS / filename,
            runs_root=REPO_ROOT / "experiments" / "runs",
            repo_root=REPO_ROOT,
        )
        result = await runtime.run(
            baseline_workflow(),
            experiment=f"stage3-closure-functional-{label.lower()}",
            input_text=FUNCTIONAL_INPUT,
        )
        metrics = result.metrics
        print(
            f"functional {label}: {result.status} prompt={metrics.total_prompt_tokens} "
            f"hit={metrics.total_cache_hit_tokens} miss={metrics.total_cache_miss_tokens} "
            f"transform_ms={result.total_transform_latency_ms:.2f} "
            f"transform={result.transform_name} deduplicated={result.deduplicated}"
        )


async def run_stress() -> None:
    blocks = cache_stress_blocks(SCALE)
    store = ExperimentStore(REPO_ROOT / "experiments" / "runs" / "stage3-formal.jsonl")
    renderer = StablePromptRenderer(preserve_input_order=True)

    for label, _, transform_name in VARIANTS:
        for repetition in range(1, REPETITIONS + 1):
            observations: list = []
            adapter = DeepSeekAdapter.from_env(on_call=observations.append)
            transform = _transform(transform_name)
            contexts = [
                [blocks[block_id] for block_id in request.block_ids] for request in REQUESTS
            ]
            total_prompt = total_completion = total_hit = total_miss = 0
            provider_latency = 0.0
            transform_latency = 0.0
            original_chars = optimized_chars = 0
            success = True

            for request, context in zip(REQUESTS, contexts, strict=True):
                transformed = transform.transform(blocks=context, query=request.query)
                task = AtlasTaskSpec(
                    task_id=f"stress-{request.request_id}",
                    title="Cache Stress",
                    description=request.query,
                    assignee="stress-agent",
                )
                rendered = renderer.render(
                    stable_system_prefix=SYSTEM,
                    agent_role="cache-stress analyst",
                    task=task,
                    context_blocks=transformed.blocks,
                )
                response = await adapter.chat(
                    [
                        LLMMessage(
                            role="user",
                            content=[TextBlock(text=rendered.messages[1]["content"])],
                        )
                    ],
                    LLMChatOptions(
                        model=MODEL,
                        system_prompt=rendered.messages[0]["content"],
                        max_tokens=200,
                        temperature=0,
                    ),
                )
                success = success and bool(response.content)
                usage = observations[-1].usage
                total_prompt += usage.prompt_tokens
                total_completion += usage.completion_tokens
                total_hit += usage.cache_hit_tokens or 0
                total_miss += usage.cache_miss_tokens or 0
                provider_latency += usage.latency_ms
                transform_latency += transformed.transform_latency_ms
                original_chars += transformed.original_context_chars
                optimized_chars += transformed.optimized_context_chars

            denominator = total_hit + total_miss
            ratio = total_hit / denominator if denominator else None
            store.append(
                StressRunRecord(
                    run_id=uuid.uuid4().hex,
                    timestamp=datetime.now(UTC),
                    variant=label,
                    repetition=repetition,
                    git_commit=git_commit(REPO_ROOT),
                    git_dirty=git_dirty(REPO_ROOT),
                    model=MODEL,
                    anycode_version=package_version("anycode-py"),
                    contextpilot_version=package_version("contextpilot"),
                    transform_name=transform_name,
                    scale=SCALE,
                    request_sequence=[request.request_id for request in REQUESTS],
                    prompt_tokens=total_prompt,
                    completion_tokens=total_completion,
                    cache_hit_tokens=total_hit,
                    cache_miss_tokens=total_miss,
                    cache_hit_ratio=ratio,
                    provider_latency_ms=provider_latency,
                    transform_latency_ms=transform_latency,
                    original_context_chars=original_chars,
                    optimized_context_chars=optimized_chars,
                    task_success=success,
                )
            )
            print(
                f"stress {label} rep={repetition}: prompt={total_prompt} "
                f"hit={total_hit} miss={total_miss} ratio={ratio} "
                f"provider_ms={provider_latency:.1f} transform_ms={transform_latency:.2f} "
                f"chars={original_chars}->{optimized_chars} success={success}"
            )


async def main() -> None:
    import os

    if os.environ.get("ATLASOS_RUN_LIVE_TESTS") != "1":
        raise SystemExit("set ATLASOS_RUN_LIVE_TESTS=1 to run live Stage 3 experiments")
    await run_functional()
    await run_stress()


if __name__ == "__main__":
    asyncio.run(main())
