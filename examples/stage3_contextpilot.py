"""Stage 3: ContextPilot P0-P3 on the functional and cache-stress workloads.

    ATLASOS_RUN_LIVE_TESTS=1 uv run python examples/stage3_contextpilot.py

Functional runs go through AtlasRuntime (AnyCode tasks). The cache-stress harness calls the
DeepSeek adapter directly with Atlas-rendered prompts so request sequence, block layout, and
optimizer flags stay explicit; it does not introduce a second task runtime.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from anycode.types import LLMChatOptions, LLMMessage, TextBlock
from pydantic import BaseModel, ConfigDict

from atlasos.context import ContextRef, StablePromptRenderer, context_layout_hash
from atlasos.context.optimizers import ContextPilotOptimizer, IdentityContextOptimizer
from atlasos.experiments import ExperimentStore
from atlasos.integrations.deepseek import DeepSeekAdapter
from atlasos.runtime import AtlasRuntime
from atlasos.stress import REQUESTS, cache_stress_blocks
from atlasos.workflow import AtlasTaskSpec, baseline_workflow

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIGS = REPO_ROOT / "experiments" / "configs"

VARIANTS = (
    ("p0", "stage3-p0-identity.yaml", "identity", False, False),
    ("p1", "stage3-p1-reorder.yaml", "contextpilot", True, False),
    ("p2", "stage3-p2-dedup.yaml", "contextpilot", False, True),
    ("p3", "stage3-p3-full.yaml", "contextpilot", True, True),
)

INPUT_TEXT = (
    "Two approaches are under consideration for a multi-agent runtime. "
    "Approach 1 aggressively prunes context before every model call. "
    "Approach 2 keeps a stable prefix and reorders context blocks for cache reuse."
)

STRESS_SYSTEM = (
    "You are a cache-stress analyst. Answer only from the supplied documents, cite document "
    "ids, and keep the answer under one hundred words."
)
REPETITIONS = 2


class StressCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    variant: str
    rep: int
    request_id: str
    optimizer_name: str
    reorder_enabled: bool
    deduplicate_enabled: bool

    prompt_tokens: int
    completion_tokens: int
    cache_hit_tokens: int | None
    cache_miss_tokens: int | None
    cache_hit_ratio: float | None
    latency_ms: float

    optimizer_latency_ms: float
    original_context_chars: int
    optimized_context_chars: int
    pre_optimizer_layout_hash: str
    post_optimizer_layout_hash: str
    stable_prefix_hash: str
    full_prompt_hash: str


async def run_functional() -> None:
    for label, filename, _, _, _ in VARIANTS:
        runtime = AtlasRuntime.from_config(
            CONFIGS / filename,
            runs_root=REPO_ROOT / "experiments" / "runs",
            repo_root=REPO_ROOT,
        )
        result = await runtime.run(
            baseline_workflow(),
            experiment=f"stage3-functional-{label}",
            input_text=INPUT_TEXT,
        )
        metrics = result.metrics
        print(
            f"functional {label}: {result.status} "
            f"prompt={metrics.total_prompt_tokens} hit={metrics.total_cache_hit_tokens} "
            f"miss={metrics.total_cache_miss_tokens} "
            f"opt_ms={result.total_optimizer_latency_ms:.1f} "
            f"optimizer={result.optimizer_name} reorder={result.reorder_enabled} "
            f"dedup={result.deduplicate_enabled}"
        )


async def run_stress() -> None:
    blocks = cache_stress_blocks("small")
    store = ExperimentStore(REPO_ROOT / "experiments" / "runs" / "stage3-stress.jsonl")
    summaries: dict[str, dict[str, float]] = {}

    for label, _, name, reorder, dedup in VARIANTS:
        totals = {"prompt": 0, "hit": 0, "miss": 0, "calls": 0, "opt_ms": 0.0}
        for rep in range(REPETITIONS):
            observations: list = []
            adapter = DeepSeekAdapter.from_env(on_call=observations.append)
            optimizer = (
                IdentityContextOptimizer()
                if name == "identity"
                else ContextPilotOptimizer(
                    reorder=reorder,
                    deduplicate=dedup,
                    conversation_id=f"stress-{label}-{rep}",
                )
            )
            contexts = [
                [blocks[block_id] for block_id in request.block_ids] for request in REQUESTS
            ]
            queries = [request.query for request in REQUESTS]
            pre_hashes = [
                context_layout_hash(
                    [ContextRef(block.block_id, block.version) for block in context]
                )
                for context in contexts
            ]
            if name == "identity":
                optimized = [
                    optimizer.optimize(blocks=context, query=query)
                    for context, query in zip(contexts, queries, strict=True)
                ]
            else:
                optimized = optimizer.optimize_batch(contexts=contexts, queries=queries)

            renderer = StablePromptRenderer(preserve_input_order=True)
            for request, context, pre_hash, item in zip(
                REQUESTS, contexts, pre_hashes, optimized, strict=True
            ):
                del context
                task = AtlasTaskSpec(
                    task_id=f"stress-{request.request_id}",
                    title="Cache Stress",
                    description=request.query,
                    assignee="stress-agent",
                )
                rendered = renderer.render(
                    stable_system_prefix=STRESS_SYSTEM,
                    agent_role="cache-stress analyst",
                    task=task,
                    context_blocks=item.blocks,
                )
                await adapter.chat(
                    [
                        LLMMessage(
                            role="user",
                            content=[TextBlock(text=rendered.messages[1]["content"])],
                        )
                    ],
                    LLMChatOptions(
                        model="deepseek-chat",
                        system_prompt=rendered.messages[0]["content"],
                        max_tokens=200,
                        temperature=0,
                    ),
                )
                observation = observations[-1]
                usage = observation.usage
                store.append(
                    StressCall(
                        variant=label,
                        rep=rep,
                        request_id=request.request_id,
                        optimizer_name=item.optimizer_name,
                        reorder_enabled=bool(item.metadata.get("reorder_enabled")),
                        deduplicate_enabled=bool(item.metadata.get("deduplicate_enabled")),
                        prompt_tokens=usage.prompt_tokens,
                        completion_tokens=usage.completion_tokens,
                        cache_hit_tokens=usage.cache_hit_tokens,
                        cache_miss_tokens=usage.cache_miss_tokens,
                        cache_hit_ratio=usage.cache_hit_ratio,
                        latency_ms=usage.latency_ms,
                        optimizer_latency_ms=item.optimizer_latency_ms,
                        original_context_chars=item.original_context_chars,
                        optimized_context_chars=item.optimized_context_chars,
                        pre_optimizer_layout_hash=pre_hash,
                        post_optimizer_layout_hash=rendered.context_layout_hash,
                        stable_prefix_hash=rendered.stable_prefix_hash,
                        full_prompt_hash=rendered.full_prompt_hash,
                    )
                )
                totals["prompt"] += usage.prompt_tokens
                totals["hit"] += usage.cache_hit_tokens or 0
                totals["miss"] += usage.cache_miss_tokens or 0
                totals["opt_ms"] += item.optimizer_latency_ms
                totals["calls"] += 1
        summaries[label] = totals
        print(
            f"stress {label}: calls={int(totals['calls'])} prompt={int(totals['prompt'])} "
            f"hit={int(totals['hit'])} miss={int(totals['miss'])} "
            f"opt_ms={totals['opt_ms']:.1f}"
        )


async def main() -> None:
    import os

    if os.environ.get("ATLASOS_RUN_LIVE_TESTS") != "1":
        raise SystemExit("set ATLASOS_RUN_LIVE_TESTS=1 to run live Stage 3 experiments")
    await run_functional()
    await run_stress()


if __name__ == "__main__":
    asyncio.run(main())
