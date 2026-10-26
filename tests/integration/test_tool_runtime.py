"""V2 M1 integration: stable native surface, search -> disclosure -> dispatch, resume state."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterable
from pathlib import Path
from typing import Any

import pytest
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
from atlasos.durability import InjectedCrash
from atlasos.runtime import AtlasRuntime
from atlasos.tools.catalog import DEFAULT_CORE_TOOL_NAMES
from atlasos.tools.registry import provider_tool_name
from atlasos.workflow import AtlasAgentSpec, AtlasTaskSpec, AtlasWorkflow

EXTERNAL_TOOL = "atlas_rag.search"
CORE_NAMES = tuple(sorted(DEFAULT_CORE_TOOL_NAMES))
PROVIDER_CORE_NAMES = tuple(provider_tool_name(name) for name in CORE_NAMES)


class QueryInput(BaseModel):
    text: str = ""


def make_external_tool(calls: list[dict[str, Any]]) -> ToolDefinition:
    async def execute(validated: QueryInput, context: ToolUseContext) -> ToolResult:
        del context
        calls.append({"tool": EXTERNAL_TOOL, "text": validated.text})
        return ToolResult(data=f"policy result for {validated.text}", is_error=False)

    return ToolDefinition(
        name=EXTERNAL_TOOL,
        description="Search internal company policy documents by capability.",
        input_model=QueryInput,
        execute=execute,
        side_effecting=False,
    )


class ScriptedAdapter:
    """Returns a scripted sequence of tool calls / final text for one agent."""

    def __init__(
        self,
        script: list[Any],
        *,
        seen_tools: list[tuple[str, ...]],
        seen_messages: list[list[LLMMessage]],
        wait_before_raise: asyncio.Event | None = None,
    ) -> None:
        self._script = script
        self._seen_tools = seen_tools
        self._seen_messages = seen_messages
        self._wait_before_raise = wait_before_raise
        self._cursor = 0

    @property
    def name(self) -> str:
        return "scripted"

    async def chat(self, messages: list[LLMMessage], options: LLMChatOptions) -> LLMResponse:
        self._seen_tools.append(tuple(tool.name for tool in options.tools or ()))
        self._seen_messages.append(list(messages))
        step = self._script[min(self._cursor, len(self._script) - 1)]
        self._cursor += 1
        if isinstance(step, BaseException):
            if self._wait_before_raise is not None:
                await self._wait_before_raise.wait()
            raise step
        return LLMResponse(
            id=f"scripted-{self._cursor}",
            content=list(step),
            model=options.model or "fake-model",
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


def _tool_use(name: str, payload: dict[str, Any], *, call_id: str) -> list[Any]:
    return [ToolUseBlock(id=call_id, name=name, input=payload)]


def _workflow() -> AtlasWorkflow:
    return AtlasWorkflow(
        name="m1-tools",
        agents=(
            AtlasAgentSpec("a", "agent a"),
            AtlasAgentSpec("b", "agent b"),
            AtlasAgentSpec("c", "agent c"),
        ),
        tasks=(
            AtlasTaskSpec("a1", "A1", "first root", "a"),
            AtlasTaskSpec("b1", "B1", "second root", "b"),
            AtlasTaskSpec("merge", "Merge", "merge roots", "c", ("a1", "b1")),
        ),
    )


def _config(*, semantic: bool = False) -> AtlasConfig:
    return AtlasConfig.model_validate(
        {
            "tools": {
                "mode": "progressive",
                "search_backend": "semantic" if semantic else "keyword",
                "max_results": 3,
            }
        }
    )


def _runtime(
    tmp_path: Path,
    *,
    scripts: dict[str, list[Any]],
    calls: list[dict[str, Any]],
    seen_tools: list[tuple[str, ...]],
    seen_messages: list[list[LLMMessage]],
    external_tools: tuple[ToolDefinition, ...] | None = None,
    semantic: bool = False,
    event_hook: Any = None,
    wait_before_raise: asyncio.Event | None = None,
) -> AtlasRuntime:
    def factory(agent_name: str, on_call: Any) -> ScriptedAdapter:
        del on_call
        return ScriptedAdapter(
            scripts[agent_name],
            seen_tools=seen_tools,
            seen_messages=seen_messages,
            wait_before_raise=wait_before_raise,
        )

    return AtlasRuntime(
        _config(semantic=semantic),
        runs_root=tmp_path,
        adapter_factory=factory,
        event_hook=event_hook,
        tool_definitions=(make_external_tool(calls),) if external_tools is None else external_tools,
        repo_root=tmp_path,
    )


def _run_dir(tmp_path: Path) -> Path:
    return next(path for path in tmp_path.iterdir() if (path / "atlas_manifest.json").exists())


async def test_native_surface_is_core_only_stable_and_search_dispatch_works(tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []
    seen_tools: list[tuple[str, ...]] = []
    seen_messages: list[list[LLMMessage]] = []
    scripts = {
        "a": [[TextBlock(text="a-out")]],
        "b": [
            _tool_use("tool_search", {"query": "policy"}, call_id="call-search"),
            _tool_use(
                "dispatch_action",
                {"tool_id": EXTERNAL_TOOL, "arguments": {"text": "benefits"}},
                call_id="call-dispatch",
            ),
            [TextBlock(text="b-out")],
        ],
        "c": [[TextBlock(text="merged")]],
    }
    runtime = _runtime(
        tmp_path,
        scripts=scripts,
        calls=calls,
        seen_tools=seen_tools,
        seen_messages=seen_messages,
    )

    result = await runtime.run(_workflow(), persist=False)

    assert result.status == "succeeded"
    assert result.final_output == "merged"
    assert calls == [{"tool": EXTERNAL_TOOL, "text": "benefits"}]
    assert all(names == PROVIDER_CORE_NAMES for names in seen_tools)
    assert EXTERNAL_TOOL not in {name for names in seen_tools for name in names}
    assert result.tool_runtime is not None
    assert result.tool_runtime.search_count == 1
    assert result.tool_runtime.dispatch_count == 1
    assert result.tool_runtime.dispatch_failure_count == 0
    assert result.tool_runtime.disclosed_tool_names == (EXTERNAL_TOOL,)
    assert result.tool_runtime.native_tool_names == CORE_NAMES

    # Search schema appears only in the conversation tail (tool result), never in native tools.
    transcript = json.dumps(
        [message.model_dump(mode="json") for messages in seen_messages for message in messages]
    )
    assert EXTERNAL_TOOL in transcript
    assert "input_schema" in transcript


async def test_undisclosed_guess_is_rejected_by_the_executor(tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []
    seen_tools: list[tuple[str, ...]] = []
    seen_messages: list[list[LLMMessage]] = []
    scripts = {
        "a": [[TextBlock(text="a-out")]],
        "b": [
            _tool_use(
                "dispatch_action",
                {"tool_id": EXTERNAL_TOOL, "arguments": {"text": "guess"}},
                call_id="call-guess",
            ),
            [TextBlock(text="b-out")],
        ],
        "c": [[TextBlock(text="merged")]],
    }
    runtime = _runtime(
        tmp_path,
        scripts=scripts,
        calls=calls,
        seen_tools=seen_tools,
        seen_messages=seen_messages,
    )

    result = await runtime.run(_workflow(), persist=False)

    assert result.status == "succeeded"
    assert calls == []
    assert result.tool_runtime is not None
    assert result.tool_runtime.dispatch_failure_count == 1
    assert result.tool_runtime.dispatches[0].error_code == "tool_not_disclosed"
    assert result.tool_runtime.disclosed_tool_names == ()


async def test_resume_restores_disclosure_and_dispatches_after_crash(tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []
    seen_tools: list[tuple[str, ...]] = []
    seen_messages: list[list[LLMMessage]] = []
    scripts = {
        "a": [[TextBlock(text="a-out")]],
        "b": [
            _tool_use("tool_search", {"query": "policy"}, call_id="call-search"),
            InjectedCrash("crash after disclosure, before dispatch"),
        ],
        "c": [[TextBlock(text="merged")]],
    }
    a_completed = asyncio.Event()

    def hook(event: Any) -> None:
        if event.type == "task_complete" and event.agent == "a":
            a_completed.set()

    runtime = _runtime(
        tmp_path,
        scripts=scripts,
        calls=calls,
        seen_tools=seen_tools,
        seen_messages=seen_messages,
        event_hook=hook,
        wait_before_raise=a_completed,
    )
    with pytest.raises(InjectedCrash):
        await runtime.run(_workflow(), persist=False)

    run_dir = await asyncio.to_thread(_run_dir, tmp_path)
    state_text = await asyncio.to_thread(
        (run_dir / "run_tool_state.json").read_text, encoding="utf-8"
    )
    state = json.loads(state_text)
    assert state["dispatchable_tool_names"] == [EXTERNAL_TOOL]
    assert runtime.tool_runtime is not None
    assert runtime.tool_runtime.native_tool_names == CORE_NAMES

    resumed_scripts = {
        "a": [[TextBlock(text="should-not-run")]],
        "b": [
            _tool_use(
                "dispatch_action",
                {"tool_id": EXTERNAL_TOOL, "arguments": {"text": "resumed"}},
                call_id="call-dispatch",
            ),
            [TextBlock(text="b-out")],
        ],
        "c": [[TextBlock(text="merged")]],
    }
    resumed_tools: list[tuple[str, ...]] = []
    resumed_messages: list[list[LLMMessage]] = []
    resumed = _runtime(
        tmp_path,
        scripts=resumed_scripts,
        calls=calls,
        seen_tools=resumed_tools,
        seen_messages=resumed_messages,
    )
    result = await resumed.resume(run_dir.name, _workflow(), persist=False)

    assert result.status == "succeeded"
    assert calls == [{"tool": EXTERNAL_TOOL, "text": "resumed"}]
    assert all(names == PROVIDER_CORE_NAMES for names in resumed_tools)
    assert result.tool_runtime is not None
    assert result.tool_runtime.disclosed_tool_names == (EXTERNAL_TOOL,)
    assert result.tool_runtime.dispatch_failure_count == 0
    assert result.recovery is not None
    assert result.recovery.completed_tasks_before_resume == 1
    assert result.tool_runtime.native_tool_names == CORE_NAMES
    resumed_transcript = json.dumps(
        [message.model_dump(mode="json") for messages in resumed_messages for message in messages]
    )
    # The resumed in-flight task sees the tool.search result that made the tool dispatchable.
    assert EXTERNAL_TOOL in resumed_transcript
    assert "input_schema" in resumed_transcript


async def test_resume_keeps_disclosure_when_tool_is_unavailable(tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []
    seen_tools: list[tuple[str, ...]] = []
    seen_messages: list[list[LLMMessage]] = []
    scripts = {
        "a": [[TextBlock(text="a-out")]],
        "b": [
            _tool_use("tool_search", {"query": "policy"}, call_id="call-search"),
            InjectedCrash("crash after disclosure"),
        ],
        "c": [[TextBlock(text="merged")]],
    }
    a_completed = asyncio.Event()

    def hook(event: Any) -> None:
        if event.type == "task_complete" and event.agent == "a":
            a_completed.set()

    runtime = _runtime(
        tmp_path,
        scripts=scripts,
        calls=calls,
        seen_tools=seen_tools,
        seen_messages=seen_messages,
        event_hook=hook,
        wait_before_raise=a_completed,
    )
    with pytest.raises(InjectedCrash):
        await runtime.run(_workflow(), persist=False)
    run_dir = await asyncio.to_thread(_run_dir, tmp_path)

    resumed_scripts = {
        "a": [[TextBlock(text="should-not-run")]],
        "b": [
            _tool_use(
                "dispatch_action",
                {"tool_id": EXTERNAL_TOOL, "arguments": {"text": "resumed"}},
                call_id="call-dispatch",
            ),
            [TextBlock(text="b-out")],
        ],
        "c": [[TextBlock(text="merged")]],
    }
    resumed_tools: list[tuple[str, ...]] = []
    resumed_messages: list[list[LLMMessage]] = []
    resumed = _runtime(
        tmp_path,
        scripts=resumed_scripts,
        calls=calls,
        seen_tools=resumed_tools,
        seen_messages=resumed_messages,
        external_tools=(),
    )
    result = await resumed.resume(run_dir.name, _workflow(), persist=False)

    assert result.status == "succeeded"
    assert result.tool_runtime is not None
    assert result.tool_runtime.disclosed_tool_names == (EXTERNAL_TOOL,)
    assert result.tool_runtime.dispatches[0].error_code == "tool_unavailable"
    transcript = json.dumps(
        [message.model_dump(mode="json") for messages in resumed_messages for message in messages]
    )
    assert "tool_unavailable" in transcript
