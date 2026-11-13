"""Final-3 R-4: a real two-task DAG renders dependency context through the prompt seam.

The engine, the DAG and the prompt seam are real; only the provider is a deterministic fake. The
assertions are made on the messages the provider actually receives, which is what decides whether
the Context Plane participated in a coding run.
"""

from __future__ import annotations

from typing import Any

import pytest
from anycode import OrchestratorConfig
from anycode.plugins.registry import register_provider_factory
from anycode.providers.fake import FakeAdapter, FakeResponse
from anycode.types import AgentConfig, LLMChatOptions, LLMMessage, TeamConfig

from atlasos.context import (
    InMemoryContextStore,
    StablePromptRenderer,
    TaskScopedSelector,
)
from atlasos.context.task_prompt import AtlasTaskPromptBuilder
from atlasos.context.transform import IdentityContextTransform
from atlasos.integrations.anycode_engine import AtlasContextEngine
from atlasos.workflow import AtlasTaskSpec

PROVIDER_A = "final3-context-a"
PROVIDER_B = "final3-context-b"
RECORDED: dict[str, list[list[LLMMessage]]] = {}
SYSTEM_PROMPTS: dict[str, list[str]] = {}


class _RecordingFake(FakeAdapter):
    def __init__(self, reply: str, sink: list[list[LLMMessage]], systems: list[str]) -> None:
        super().__init__(responses=[FakeResponse(text=reply)])
        self._sink = sink
        self._systems = systems

    async def chat(self, messages: list[LLMMessage], options: LLMChatOptions):  # type: ignore[override]
        self._sink.append(list(messages))
        self._systems.append(options.system_prompt or "")
        return await super().chat(messages, options)


def _install(provider: str, reply: str) -> None:
    async def factory(**_: Any) -> FakeAdapter:
        return _RecordingFake(
            reply,
            RECORDED.setdefault(provider, []),
            SYSTEM_PROMPTS.setdefault(provider, []),
        )

    register_provider_factory(provider, factory)


def _text(messages: list[LLMMessage]) -> str:
    return "\n".join(
        str(getattr(block, "text", "")) for message in messages for block in message.content
    )


@pytest.mark.asyncio
async def test_dependent_task_receives_dependency_context_exactly_once() -> None:
    _install(PROVIDER_A, "RESULT-OF-A")
    _install(PROVIDER_B, "RESULT-OF-B")

    spec_a = AtlasTaskSpec(task_id="a", title="A", description="do a", assignee="w-a")
    spec_b = AtlasTaskSpec(
        task_id="b", title="B", description="do b", assignee="w-b", depends_on=("a",)
    )
    builder = AtlasTaskPromptBuilder(
        store=InMemoryContextStore(),
        selector=TaskScopedSelector(),
        renderer=StablePromptRenderer(preserve_input_order=True),
        transform=IdentityContextTransform(),
        specs_by_title={"A": spec_a, "B": spec_b},
        agents_by_name={"w-a": ("SYSTEM-A", "role-a"), "w-b": ("SYSTEM-B", "role-b")},
    )
    engine = AtlasContextEngine(OrchestratorConfig(), prompt_builder=builder)
    team = engine.create_team(
        "final3",
        TeamConfig(
            name="final3",
            agents=[
                AgentConfig(
                    name="w-a",
                    model="fake",
                    provider=PROVIDER_A,
                    system_prompt="SYSTEM-A",
                    tools=[],
                ),
                AgentConfig(
                    name="w-b",
                    model="fake",
                    provider=PROVIDER_B,
                    system_prompt="SYSTEM-B",
                    tools=[],
                ),
            ],
        ),
    )
    try:
        result = await engine.run_tasks(
            team,
            [
                _task_spec("A", "do a", "w-a"),
                _task_spec("B", "do b", "w-b", depends_on=["A"]),
            ],
        )
    finally:
        await engine.close()

    assert result.success is True

    prompt_a = _text(RECORDED[PROVIDER_A][0])
    prompt_b = _text(RECORDED[PROVIDER_B][0])
    assert "<dependency" not in prompt_a
    assert '<dependency task_id="a"' in prompt_b
    assert "RESULT-OF-A" in prompt_b
    assert prompt_b.count("RESULT-OF-A") == 1
    assert "do b" in prompt_b
    assert SYSTEM_PROMPTS[PROVIDER_B][0] == "SYSTEM-B"
    assert "SYSTEM-B" not in prompt_b

    renders = builder.renders()
    assert set(renders) == {"a", "b"}
    assert renders["b"]["rendered_dependency_order"] == ["a"]
    assert renders["b"]["context_layout_hash"] != renders["a"]["context_layout_hash"]


def _task_spec(title: str, description: str, assignee: str, *, depends_on: list[str] | None = None):
    from anycode import TaskSpec

    return TaskSpec(
        title=title,
        description=description,
        assignee=assignee,
        depends_on=depends_on or [],
    )
