"""M6 end-to-end AnyCode agent-loop integration: exact interrupted tool batch."""

# The M6 harness always constructs the durable stores; optional attributes are configured here.
# pyright: reportOptionalMemberAccess=false

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from anycode import AgentConfig, OrchestratorConfig, TaskSpec, TeamConfig
from anycode.plugins.registry import register_provider_factory
from anycode.types import (
    AgentInfo,
    LLMChatOptions,
    LLMMessage,
    LLMResponse,
    LLMStreamOptions,
    StreamEvent,
    TextBlock,
    TokenUsage,
    ToolUseBlock,
    ToolUseContext,
)
from tests.helpers.m6_harness import M6World

from atlasos.approvals.resume import resume_execution_suspension
from atlasos.integrations.anycode_engine import AtlasContextEngine
from atlasos.suspension.batch import ExecutionSuspended


class ScriptedAdapter:
    def __init__(self, responses: list[Any], counter: list[int]) -> None:
        self.responses = responses
        self.counter = counter
        self.name = "scripted"

    def _next(self, options: Any) -> LLMResponse:
        self.counter[0] += 1
        index = self.counter[0] - 1
        blocks: list[Any] = (
            self.responses[index]() if index < len(self.responses) else [TextBlock(text="done")]
        )
        return LLMResponse(
            id=f"resp-{index}",
            content=blocks,
            model=options.model,
            stop_reason="tool_use"
            if any(isinstance(block, ToolUseBlock) for block in blocks)
            else "end_turn",
            usage=TokenUsage(input_tokens=1, output_tokens=1),
        )

    async def chat(self, messages: list[LLMMessage], options: LLMChatOptions) -> LLMResponse:
        del messages
        return self._next(options)

    def stream(self, messages: list[LLMMessage], options: LLMStreamOptions):
        async def _gen():
            response = await self.chat(messages, options)
            for block in response.content:
                if isinstance(block, ToolUseBlock):
                    yield StreamEvent(type="tool_use", data=block)
                elif isinstance(block, TextBlock):
                    yield StreamEvent(type="text", data=block.text)
            yield StreamEvent(type="done", data=response)

        return _gen()


def _register(name: str, responses: list[Any], counter: list[int]) -> None:
    async def factory(**kwargs: Any) -> ScriptedAdapter:
        del kwargs
        return ScriptedAdapter(responses, counter)

    register_provider_factory(name, factory)


async def _run_engine(
    world: M6World,
    provider: str,
    *,
    execution_context: Any,
):
    engine = AtlasContextEngine(OrchestratorConfig(), tool_runtime=world.runtime())
    agent = AgentConfig(
        name="worker",
        model="fake-model",
        provider=provider,
        system_prompt="worker",
        execution_context=execution_context,
    )
    team = engine.create_team("m6", TeamConfig(name="m6", agents=[agent]))
    spec = TaskSpec(title="Task", description="do it", assignee="worker")
    try:
        return await engine.run_tasks(team, [spec])
    finally:
        await engine.close()


async def test_m6_agent_loop_suspends_and_resumes_without_llm_reconstruction(
    tmp_path: Path,
) -> None:
    world = M6World(tmp_path / "world")
    runtime = world.runtime()
    await world.disclose(runtime, "send external email notification")
    await world.disclose(runtime, "search internal documentation")
    target = world.root / "workspace" / "note.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("hello", encoding="utf-8")

    context = world.context()
    assert context.execution_context is not None

    def batch() -> list[Any]:
        return [
            ToolUseBlock(id="call-a", name="file_read", input={"path": str(target)}),
            ToolUseBlock(
                id="call-b",
                name="dispatch_action",
                input={
                    "tool_id": "email.send",
                    "arguments": {"to": "ops@example.test", "body": "secret-body"},
                },
            ),
            ToolUseBlock(
                id="call-c",
                name="dispatch_action",
                input={"tool_id": "docs.search", "arguments": {"query": "checkout"}},
            ),
        ]

    def final() -> list[Any]:
        return [TextBlock(text="all done")]

    counter = [0]
    provider1 = f"m6-agent-{uuid.uuid4().hex}"
    _register(provider1, [batch, final], counter)

    suspension_id = ""
    try:
        await _run_engine(world, provider1, execution_context=context.execution_context)
        raise AssertionError("expected an execution suspension")
    except ExecutionSuspended as suspended:
        suspension_id = suspended.suspension_id

    suspension = runtime.suspension_store.get(suspension_id)
    assert suspension is not None and suspension.state == "ACTIVE"
    assert world.provider.effects == 0
    assert runtime.action_ledger.list() == ()
    assert counter[0] == 1
    assert runtime.conversation_store.load("task-m6") is None

    world.approve(runtime, suspension)
    outcome = await resume_execution_suspension(
        suspension_id,
        runtime=runtime,
        coordinator=runtime.suspension_coordinator,  # type: ignore[arg-type]
        context=ToolUseContext(
            agent=AgentInfo(name="worker", role="worker", model="fake-model"),
            execution_context=context.execution_context,
        ),
        owner="test-resumer",
    )
    assert outcome.status == "settled"
    assert world.provider.effects == 1
    assert counter[0] == 1  # resume itself never calls the LLM
    conversation = runtime.conversation_store.load("task-m6")
    assert conversation is not None and len(conversation) == 2
    assert [block["tool_use_id"] for block in conversation[1]["content"]] == [
        "call-a",
        "call-b",
        "call-c",
    ]
    assert runtime.m6_metrics is not None
    assert runtime.m6_metrics.llm_reexecutions_during_suspension_resume == 0
    assert runtime.m6_metrics.tool_results_committed == 3

    # A fresh engine resumes the same task; its first model call sees the durable
    # assistant + ToolResult continuation, so the pending action is never reconstructed.
    counter[0] = 0
    provider2 = f"m6-agent-{uuid.uuid4().hex}"
    _register(provider2, [final], counter)
    result = await _run_engine(world, provider2, execution_context=context.execution_context)
    assert result.success
    assert counter[0] == 1
    assert world.provider.effects == 1
    assert runtime.conversation_store.load("task-m6") == conversation


async def test_m6_agent_normal_non_hitl_batch_unchanged(tmp_path: Path) -> None:
    world = M6World(tmp_path / "world")
    runtime = world.runtime()
    await world.disclose(runtime, "search internal documentation")
    target = world.root / "workspace" / "note.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("hello", encoding="utf-8")
    context = world.context()
    assert context.execution_context is not None

    def batch() -> list[Any]:
        return [
            ToolUseBlock(id="read", name="file_read", input={"path": str(target)}),
            ToolUseBlock(
                id="docs",
                name="dispatch_action",
                input={"tool_id": "docs.search", "arguments": {"query": "checkout"}},
            ),
        ]

    def final() -> list[Any]:
        return [TextBlock(text="done")]

    counter = [0]
    provider = f"m6-agent-{uuid.uuid4().hex}"
    _register(provider, [batch, final], counter)
    result = await _run_engine(world, provider, execution_context=context.execution_context)
    assert result.success
    assert world.provider.effects == 0
    assert world.calls["docs.search"] == 1
    assert runtime.suspension_store.list_active() == ()
    assert counter[0] == 2
