"""M1 end-to-end demo: stable core surface -> tool.search -> dispatch_action -> real executor.

Deterministic fake model; no network. Run from the repository root:

    python examples/m1_demo.py
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from collections.abc import AsyncIterable
from pathlib import Path
from typing import Any

from anycode.types import (
    LLMChatOptions,
    LLMMessage,
    LLMResponse,
    LLMStreamOptions,
    StreamEvent,
    TextBlock,
    TokenUsage,
    ToolDefinition,
    ToolResult,
    ToolUseBlock,
    ToolUseContext,
)
from pydantic import BaseModel

from atlasos.config import AtlasConfig
from atlasos.runtime import AtlasRuntime
from atlasos.workflow import AtlasAgentSpec, AtlasTaskSpec, AtlasWorkflow

TOOL_ID = "atlas_rag.search"


class QueryInput(BaseModel):
    query: str = ""


async def _execute(validated: QueryInput, context: ToolUseContext) -> ToolResult:
    del context
    return ToolResult(
        data=f"Found policy for {validated.query}: stable prefix, explicit context.",
        is_error=False,
    )


class DemoAdapter:
    def __init__(self, calls: list[tuple[str, ...]]) -> None:
        self._calls = calls
        self._turn = 0

    @property
    def name(self) -> str:
        return "m1-demo"

    async def chat(self, messages: list[LLMMessage], options: LLMChatOptions) -> LLMResponse:
        del messages
        self._calls.append(tuple(tool.name for tool in options.tools or ()))
        self._turn += 1
        if self._turn == 1:
            content = [
                ToolUseBlock(
                    id="search-1",
                    name="tool_search",
                    input={"query": "internal company policy"},
                )
            ]
            stop = "tool_use"
        elif self._turn == 2:
            content = [
                ToolUseBlock(
                    id="dispatch-1",
                    name="dispatch_action",
                    input={"tool_id": TOOL_ID, "arguments": {"query": "travel"}},
                )
            ]
            stop = "tool_use"
        else:
            content = [TextBlock(text="Policy: prefer stable prefixes.")]
            stop = "end_turn"
        return LLMResponse(
            id=f"demo-{self._turn}",
            content=content,
            model=options.model or "fake",
            stop_reason=stop,
            usage=TokenUsage(input_tokens=10, output_tokens=5),
        )

    def stream(
        self,
        messages: list[LLMMessage],
        options: LLMStreamOptions,
    ) -> AsyncIterable[StreamEvent]:
        async def _generate() -> AsyncIterable[StreamEvent]:
            yield StreamEvent(type="done", data=await self.chat(messages, options))

        return _generate()


def workflow() -> AtlasWorkflow:
    return AtlasWorkflow(
        name="m1-demo",
        agents=(AtlasAgentSpec("worker", "You are a policy agent."),),
        tasks=(
            AtlasTaskSpec(
                task_id="lookup",
                title="Lookup",
                description="Find the travel policy.",
                assignee="worker",
            ),
        ),
    )


async def main() -> None:
    calls: list[tuple[str, ...]] = []

    def factory(agent_name: str, on_call: Any) -> DemoAdapter:
        del agent_name, on_call
        return DemoAdapter(calls)

    config = AtlasConfig.model_validate(
        {"tools": {"mode": "progressive", "search_backend": "keyword"}}
    )
    with tempfile.TemporaryDirectory(prefix="atlas-m1-demo-") as directory:
        runtime = AtlasRuntime(
            config,
            runs_root=Path(directory),
            adapter_factory=factory,
            tool_definitions=(
                ToolDefinition(
                    name=TOOL_ID,
                    description="Search internal company policy documents by topic.",
                    input_model=QueryInput,
                    execute=_execute,
                    side_effecting=False,
                ),
            ),
        )
        result = await runtime.run(workflow(), persist=False)
    runtime_metrics = result.tool_runtime
    assert runtime_metrics is not None

    print(f"native tools per model turn: {calls}")
    print(f"native surface stable: {len(set(calls)) == 1}")
    print(f"searches: {[s.returned_tool_ids for s in runtime_metrics.searches]}")
    print(f"dispatches: {[d.tool_id for d in runtime_metrics.dispatches]}")
    print(f"disclosed: {runtime_metrics.disclosed_tool_names}")
    print(f"final output: {result.final_output}")
    print(
        "tool runtime metrics: "
        + json.dumps(
            {
                "native_tool_names": runtime_metrics.native_tool_names,
                "native_provider_tool_names": runtime_metrics.native_provider_tool_names,
                "search_count": runtime_metrics.search_count,
                "dispatch_count": runtime_metrics.dispatch_count,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
