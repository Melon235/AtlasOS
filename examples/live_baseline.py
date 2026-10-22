"""Stage 1 live baseline: run the frozen workload twice against the real DeepSeek API.

Requires DEEPSEEK_API_KEY (loaded from .env) and an explicit opt-in:

    ATLASOS_RUN_LIVE_TESTS=1 uv run python examples/live_baseline.py

Cache hits are best-effort; this script reports them but never asserts a positive count.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from atlasos.config import load_config
from atlasos.runtime import AtlasRuntime
from atlasos.workflow import baseline_workflow

REPO_ROOT = Path(__file__).resolve().parents[1]


async def main() -> None:
    config = load_config(REPO_ROOT / "experiments" / "configs" / "baseline.yaml")
    runtime = AtlasRuntime(
        config,
        runs_root=REPO_ROOT / "experiments" / "runs",
        repo_root=REPO_ROOT,
    )

    for attempt in (1, 2):
        result = await runtime.run(
            baseline_workflow(),
            experiment="stage1-live",
            input_text=(
                "Two approaches are under consideration for a multi-agent runtime. "
                "Approach 1 aggressively prunes context before every model call. "
                "Approach 2 keeps a stable prefix and reorders context blocks for cache reuse."
            ),
        )

        print(f"\n=== run {attempt}: {result.run_id} ({result.status}) ===")
        for task in result.tasks:
            output = (task.output or "").replace("\n", " ")
            print(f"  {task.task_id:<12} {task.status:<10} {output[:120]!r}")

        usage = result.metrics
        print(
            "tokens: "
            f"prompt={usage.total_prompt_tokens} completion={usage.total_completion_tokens} "
            f"hit={usage.total_cache_hit_tokens} miss={usage.total_cache_miss_tokens} "
            f"ratio={usage.weighted_cache_hit_ratio}"
        )
        print(f"latency_ms={usage.total_latency_ms:.1f} cost={usage.estimated_cost}")
        prefixes = {record.stable_prefix_hash for record in result.inference_records}
        fulls = {record.full_prompt_hash for record in result.inference_records}
        print(
            f"records={len(result.inference_records)} unique_prefixes={len(prefixes)} "
            f"unique_full_prompts={len(fulls)}"
        )


if __name__ == "__main__":
    asyncio.run(main())
