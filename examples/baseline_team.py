"""Stage 0 spike: Planner -> (Research A, Research B) -> Review, on AnyCode, offline.

Run with: uv run python examples/baseline_team.py
"""

from __future__ import annotations

import asyncio

from anycode import AnyCode, OrchestratorConfig

from atlasos.integrations.anycode_runtime import (
    build_agent,
    build_task,
    build_team,
    register_fake_provider,
)


async def main() -> None:
    register_fake_provider("atlasos-fake-research-a", "fact A")
    register_fake_provider("atlasos-fake-research-b", "fact B")
    register_fake_provider("atlasos-fake-reviewer", "merged review")

    engine = AnyCode(OrchestratorConfig(max_concurrency=3))
    team = build_team(
        engine,
        name="stage0-baseline",
        agents=[
            build_agent(
                name="researcher-a",
                provider="atlasos-fake-research-a",
                system_prompt="You are a careful researcher.",
            ),
            build_agent(
                name="researcher-b",
                provider="atlasos-fake-research-b",
                system_prompt="You are a careful researcher.",
            ),
            build_agent(
                name="reviewer",
                provider="atlasos-fake-reviewer",
                system_prompt="You merge research into a decision.",
            ),
        ],
    )

    result = await engine.run_tasks(
        team,
        [
            build_task(
                title="Merge review",
                description="Merge both research outputs into a recommendation.",
                assignee="reviewer",
                depends_on=["Research A", "Research B"],
            ),
            build_task(title="Research A", description="Collect fact A.", assignee="researcher-a"),
            build_task(title="Research B", description="Collect fact B.", assignee="researcher-b"),
        ],
    )

    print(f"success={result.success}")
    for agent, agent_result in sorted(result.agent_results.items()):
        print(f"  {agent}: {agent_result.output!r}")
    usage = result.total_token_usage
    print(
        "tokens: "
        f"in={usage.input_tokens} out={usage.output_tokens} "
        f"cache_read={usage.cache_read_input_tokens}"
    )


if __name__ == "__main__":
    asyncio.run(main())
