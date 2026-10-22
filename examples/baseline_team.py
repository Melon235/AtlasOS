"""Stage 1 offline baseline: the frozen workload on AnyCode with deterministic fake adapters.

Run with: uv run python examples/baseline_team.py
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

from anycode.providers.fake import FakeAdapter, FakeResponse
from anycode.types import LLMAdapter

from atlasos.config import load_config
from atlasos.metrics import InferenceObservation
from atlasos.runtime import AtlasRuntime
from atlasos.workflow import baseline_workflow

REPLIES = {
    "researcher-a": "Research thread A: fact A.",
    "researcher-b": "Research thread B: fact B.",
    "reviewer": "Recommendation: combine fact A and fact B.",
}

REPO_ROOT = Path(__file__).resolve().parents[1]


def fake_adapter_factory(
    agent_name: str,
    on_call: Callable[[InferenceObservation], None],
) -> LLMAdapter:
    del on_call  # deterministic fakes measure no provider usage
    return FakeAdapter(responses=[FakeResponse(text=REPLIES[agent_name])])


async def main() -> None:
    config = load_config(REPO_ROOT / "experiments" / "configs" / "baseline.yaml")
    runtime = AtlasRuntime(
        config,
        runs_root=REPO_ROOT / "experiments" / "runs",
        adapter_factory=fake_adapter_factory,
        repo_root=REPO_ROOT,
    )

    result = await runtime.run(
        baseline_workflow(),
        experiment="stage1-offline",
        input_text=(
            "Two approaches are under consideration for a multi-agent runtime. "
            "Approach 1 aggressively prunes context before every model call. "
            "Approach 2 keeps a stable prefix and reorders context blocks for cache reuse."
        ),
        persist=False,
    )

    print(f"run: {result.run_id} status={result.status}")
    for task in result.tasks:
        print(f"  {task.task_id:<12} {task.status:<10} {task.output!r}")
    metrics: dict[str, Any] = result.metrics.model_dump()
    print("metrics:", metrics)
    print(f"final output: {result.final_output!r}")


if __name__ == "__main__":
    asyncio.run(main())
