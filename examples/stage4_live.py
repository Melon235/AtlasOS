"""Stage 4 live functional validation under S0/S1/S2 (secondary cache metrics only)."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from atlasos.runtime import AtlasRuntime
from atlasos.workflow import baseline_workflow

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIGS = REPO_ROOT / "experiments" / "configs"
VARIANTS = (
    ("S0", "stage4-s0-static.yaml"),
    ("S1", "stage4-s1-capability.yaml"),
    ("S2", "stage4-s2-locality.yaml"),
)


async def main() -> None:
    if os.environ.get("ATLASOS_RUN_LIVE_TESTS") != "1":
        raise SystemExit("set ATLASOS_RUN_LIVE_TESTS=1 to run live validation")
    for label, filename in VARIANTS:
        runtime = AtlasRuntime.from_config(
            CONFIGS / filename,
            runs_root=REPO_ROOT / "experiments" / "runs",
            repo_root=REPO_ROOT,
        )
        result = await runtime.run(
            baseline_workflow(),
            experiment=f"stage4-live-{label.lower()}",
            input_text=(
                "Two approaches are under consideration for a multi-agent runtime. "
                "Approach 1 aggressively prunes context before every model call. "
                "Approach 2 keeps a stable prefix and reorders context blocks for cache reuse."
            ),
        )
        metrics = result.metrics
        print(
            f"{label} {result.status} placement={result.placement_policy} "
            f"switches={result.dependency_agent_switches} "
            f"locality={result.mean_assignment_locality} "
            f"handoff={result.total_logical_handoff_refs} "
            f"prompt={metrics.total_prompt_tokens} hit={metrics.total_cache_hit_tokens} "
            f"miss={metrics.total_cache_miss_tokens} "
            f"ratio={metrics.weighted_cache_hit_ratio} "
            f"latency={metrics.total_latency_ms:.1f}"
        )


if __name__ == "__main__":
    asyncio.run(main())
