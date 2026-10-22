"""Stage 2 ablation: U0 vs C1 (naive) vs C2 (stable) under inverted completion order.

Runs each variant twice and inverts research completion order by injecting a client-side delay.
The delay is a controlled experiment knob, not part of the runtime; provider latency metrics come
from the adapter and exclude it.

    ATLASOS_RUN_LIVE_TESTS=1 uv run python examples/stage2_ablation.py
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from anycode.types import LLMChatOptions, LLMMessage, LLMResponse, LLMStreamOptions, StreamEvent

from atlasos.config import load_config
from atlasos.integrations.deepseek import DeepSeekAdapter
from atlasos.runtime import AtlasRuntime
from atlasos.workflow import baseline_workflow

REPO_ROOT = Path(__file__).resolve().parents[1]
INPUT_TEXT = (
    "Two approaches are under consideration for a multi-agent runtime. "
    "Approach 1 aggressively prunes context before every model call. "
    "Approach 2 keeps a stable prefix and reorders context blocks for cache reuse."
)

VARIANTS = (
    ("u0", "stage2-upstream.yaml"),
    ("c1-naive", "stage2-naive.yaml"),
    ("c2-stable", "stage2-stable.yaml"),
)

DELAYS = {
    "a_first": {"researcher-b": 0.9},
    "b_first": {"researcher-a": 0.9},
}


class _DelayedAdapter:
    """Delegates to the real adapter after a fixed delay (test-only timing knob)."""

    def __init__(self, inner: DeepSeekAdapter, delay_s: float) -> None:
        self._inner = inner
        self._delay_s = delay_s

    @property
    def name(self) -> str:
        return self._inner.name

    async def chat(
        self,
        messages: list[LLMMessage],
        options: LLMChatOptions,
        **kwargs: Any,
    ) -> LLMResponse:
        await asyncio.sleep(self._delay_s)
        return await self._inner.chat(messages, options, **kwargs)

    def stream(
        self,
        messages: list[LLMMessage],
        options: LLMStreamOptions,
    ) -> AsyncIterator[StreamEvent]:
        async def _generate() -> AsyncIterator[StreamEvent]:
            await asyncio.sleep(self._delay_s)
            async for event in self._inner.stream(messages, options):
                yield event

        return _generate()


def _factory(delays: dict[str, float]):
    def factory(agent_name: str, on_call: Any) -> _DelayedAdapter:
        inner = DeepSeekAdapter.from_env(on_call=on_call)
        return _DelayedAdapter(inner, delays.get(agent_name, 0.0))

    return factory


async def main() -> None:
    configs = REPO_ROOT / "experiments" / "configs"
    for variant, filename in VARIANTS:
        for order, delays in DELAYS.items():
            config = load_config(configs / filename)
            runtime = AtlasRuntime(
                config,
                runs_root=REPO_ROOT / "experiments" / "runs",
                adapter_factory=_factory(delays),
                repo_root=REPO_ROOT,
            )
            result = await runtime.run(
                baseline_workflow(),
                experiment=f"stage2-{variant}-{order}",
                input_text=INPUT_TEXT,
            )
            review = next(
                record for record in result.inference_records if record.task_id == "review"
            )
            metrics = result.metrics
            print(
                f"{variant:<9} {order:<7} {result.status:<9} "
                f"prompt={metrics.total_prompt_tokens:<4} "
                f"hit={metrics.total_cache_hit_tokens} miss={metrics.total_cache_miss_tokens}\n"
                f"          completion={review.dependency_completion_order} "
                f"rendered={review.rendered_dependency_order} "
                f"layout={(review.context_layout_hash or '-')[:12]} "
                f"full={(review.full_prompt_hash or '-')[:12]}"
            )


if __name__ == "__main__":
    asyncio.run(main())
