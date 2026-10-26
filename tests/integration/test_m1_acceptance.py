"""Explicit V2 M1 acceptance checks (T01-T17 matrix plus final scenario)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterable
from pathlib import Path
from typing import Any

from anycode.types import (
    AgentConfig,
    LLMChatOptions,
    LLMMessage,
    LLMResponse,
    LLMStreamOptions,
    OrchestratorConfig,
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
from atlasos.integrations.anycode_engine import AtlasContextEngine
from atlasos.runtime import AtlasRuntime
from atlasos.tools import AtlasToolRuntime, KeywordToolSearchIndex
from atlasos.tools.catalog import DEFAULT_CORE_TOOL_NAMES
from atlasos.tools.registry import provider_tool_name
from atlasos.tools.search import build_entries
from atlasos.workflow import AtlasAgentSpec, AtlasTaskSpec, AtlasWorkflow

CORE_NAMES = tuple(sorted(DEFAULT_CORE_TOOL_NAMES))
PROVIDER_CORE_NAMES = tuple(provider_tool_name(name) for name in CORE_NAMES)
ATLAS_RAG = "atlas_rag.search"
EMAIL = "email.send"
GITHUB = "github.issue.get"


class QueryInput(BaseModel):
    text: str = ""


def make_tool(name: str, description: str, calls: list[str]) -> ToolDefinition:
    async def execute(validated: QueryInput, context: ToolUseContext) -> ToolResult:
        del context
        calls.append(name)
        return ToolResult(data=f"{name}:{validated.text}", is_error=False)

    return ToolDefinition(
        name=name,
        description=description,
        input_model=QueryInput,
        execute=execute,
        side_effecting=False,
    )


def tool_use(name: str, payload: dict[str, Any], call_id: str) -> list[Any]:
    return [ToolUseBlock(id=call_id, name=name, input=payload)]


class ScriptedAdapter:
    def __init__(
        self,
        script: list[list[Any]],
        *,
        seen_tools: list[tuple[str, ...]],
        seen_messages: list[list[LLMMessage]],
    ) -> None:
        self._script = script
        self._seen_tools = seen_tools
        self._seen_messages = seen_messages
        self._cursor = 0

    @property
    def name(self) -> str:
        return "acceptance-scripted"

    async def chat(self, messages: list[LLMMessage], options: LLMChatOptions) -> LLMResponse:
        self._seen_tools.append(tuple(definition.name for definition in options.tools or ()))
        self._seen_messages.append(list(messages))
        step = self._script[min(self._cursor, len(self._script) - 1)]
        self._cursor += 1
        return LLMResponse(
            id=f"acceptance-{self._cursor}",
            content=step,
            model=options.model or "fake",
            stop_reason="tool_use"
            if any(isinstance(b, ToolUseBlock) for b in step)
            else "end_turn",
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


async def test_t01_one_shared_registry_across_agents(tmp_path: Path) -> None:
    calls: list[str] = []
    tool_runtime = AtlasToolRuntime(
        "t01",
        external_tools=(make_tool(ATLAS_RAG, "Search internal knowledge.", calls),),
    )
    engine = AtlasContextEngine(OrchestratorConfig(), tool_runtime=tool_runtime)
    try:
        first = engine.build_agent(AgentConfig(name="a", model="fake", provider="fake"))
        second = engine.build_agent(AgentConfig(name="b", model="fake", provider="fake"))
        assert first._registry is tool_runtime.registry  # type: ignore[attr-defined]
        assert second._registry is tool_runtime.registry  # type: ignore[attr-defined]
        assert first.config.tools == list(PROVIDER_CORE_NAMES)
        assert second.config.tools == list(PROVIDER_CORE_NAMES)
        assert tool_runtime.registry.has("file_read")
        assert tool_runtime.registry.has("tool.search")
        assert tool_runtime.registry.has(ATLAS_RAG)
    finally:
        await engine.close()


def test_t02_t03_core_visible_and_searchable_hidden() -> None:
    calls: list[str] = []
    runtime = AtlasToolRuntime(
        "t02",
        external_tools=(make_tool(EMAIL, "Send an email message.", calls),),
    )
    assert runtime.native_tool_names == CORE_NAMES
    assert runtime.native_provider_tool_names == PROVIDER_CORE_NAMES
    assert runtime.agent_tools == PROVIDER_CORE_NAMES
    assert runtime.registry.has(EMAIL)
    assert EMAIL not in runtime.native_tool_names
    assert EMAIL not in runtime.native_provider_tool_names
    assert EMAIL not in runtime.agent_tools
    assert all(
        match.tool_id not in CORE_NAMES for match in runtime.search_matches("file read bash")
    )


def test_t17_deterministic_core_order_and_tied_search_order() -> None:
    calls: list[str] = []
    entries = build_entries(
        AtlasToolRuntime(
            "t17",
            external_tools=(
                make_tool(GITHUB, "Get a GitHub policy issue.", calls),
                make_tool(EMAIL, "Send an email policy message.", calls),
            ),
        ).registry,
        _catalog_with_tools(),
    )
    index = KeywordToolSearchIndex(entries)
    # Both descriptions contain "policy"; the scores tie and the ids must decide.
    first = index.search("policy", top_k=3)
    second = index.search("policy", top_k=3)
    assert [match.tool_id for match in first] == ["email.send", "github.issue.get"]
    assert first == second

    runtime = AtlasToolRuntime(
        "t17b",
        external_tools=(make_tool(EMAIL, "Send an email message.", calls),),
    )
    assert runtime.native_tool_names == CORE_NAMES
    assert runtime.native_provider_tool_names == tuple(sorted(runtime.native_provider_tool_names))
    before = runtime.native_surface_hash()
    asyncio.run(runtime.search("email"))
    assert runtime.native_surface_hash() == before


def _catalog_with_tools():
    from atlasos.tools.catalog import ToolCatalog

    return ToolCatalog(
        core_tool_names=DEFAULT_CORE_TOOL_NAMES,
        searchable_tool_names=frozenset({EMAIL, GITHUB}),
    )


async def test_final_acceptance_scenario(tmp_path: Path) -> None:
    """Core only initially; search -> dispatch; guessed email rejected; search -> dispatch."""

    calls: list[str] = []
    seen_tools: list[tuple[str, ...]] = []
    seen_messages: list[list[LLMMessage]] = []
    script = [
        tool_use("tool_search", {"query": "internal company knowledge"}, "search-rag"),
        tool_use(
            "dispatch_action",
            {"tool_id": ATLAS_RAG, "arguments": {"text": "travel"}},
            "dispatch-rag",
        ),
        tool_use(
            "dispatch_action",
            {"tool_id": EMAIL, "arguments": {"text": "guess"}},
            "guess-email",
        ),
        tool_use("tool_search", {"query": "send email message"}, "search-email"),
        tool_use(
            "dispatch_action",
            {"tool_id": EMAIL, "arguments": {"text": "hello"}},
            "dispatch-email",
        ),
        [TextBlock(text="done")],
    ]

    def factory(agent_name: str, on_call: Any) -> ScriptedAdapter:
        del agent_name, on_call
        return ScriptedAdapter(script, seen_tools=seen_tools, seen_messages=seen_messages)

    config = AtlasConfig.model_validate(
        {"tools": {"mode": "progressive", "search_backend": "keyword"}}
    )
    runtime = AtlasRuntime(
        config,
        runs_root=tmp_path,
        adapter_factory=factory,
        tool_definitions=(
            make_tool(ATLAS_RAG, "Search internal company knowledge and policy.", calls),
            make_tool(EMAIL, "Send an email message to a recipient.", calls),
            make_tool(GITHUB, "Get a GitHub issue by number.", calls),
        ),
    )
    workflow = AtlasWorkflow(
        name="acceptance",
        agents=(AtlasAgentSpec("worker", "worker"),),
        tasks=(
            AtlasTaskSpec(
                task_id="task",
                title="Task",
                description="Find internal knowledge, then email it.",
                assignee="worker",
            ),
        ),
    )

    result = await runtime.run(workflow, persist=False)
    assert result.status == "succeeded"
    assert result.final_output == "done"
    assert all(names == PROVIDER_CORE_NAMES for names in seen_tools)
    assert calls == [ATLAS_RAG, EMAIL]
    assert result.tool_runtime is not None
    assert result.tool_runtime.disclosed_tool_names == (ATLAS_RAG, EMAIL)
    assert result.tool_runtime.dispatch_failure_count == 1
    transcript = json.dumps(
        [message.model_dump(mode="json") for messages in seen_messages for message in messages]
    )
    assert "tool_not_disclosed" in transcript
    assert GITHUB not in result.tool_runtime.disclosed_tool_names
