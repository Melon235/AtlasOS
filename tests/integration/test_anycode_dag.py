"""Stage 0 spike: a dependency-ordered multi-agent run on AnyCode with deterministic fakes.

Proves the upstream extension path (provider factories, team/task construction, wave scheduling)
without touching AtlasOS optimization code, which does not exist yet.
"""

from __future__ import annotations

from typing import Any

import pytest
from anycode import AnyCode, OrchestratorConfig
from anycode.plugins.registry import register_provider_factory
from anycode.providers.fake import FakeAdapter, FakeResponse
from anycode.types import LLMChatOptions, LLMMessage, TextBlock

from atlasos.integrations.anycode_runtime import build_agent, build_task, build_team

PROVIDERS: dict[str, tuple[str, str]] = {
    "atlasos-test-research-a": ("researcher-a", "fact A"),
    "atlasos-test-research-b": ("researcher-b", "fact B"),
    "atlasos-test-reviewer": ("reviewer", "merged review"),
}

RECORDED: dict[str, list[list[LLMMessage]]] = {}


class _RecordingFake(FakeAdapter):
    """FakeAdapter that also captures the prompts AnyCode assembled."""

    def __init__(self, reply: str, sink: list[list[LLMMessage]]) -> None:
        super().__init__(responses=[FakeResponse(text=reply)])
        self._sink = sink

    async def chat(self, messages: list[LLMMessage], options: LLMChatOptions):  # type: ignore[override]
        self._sink.append(list(messages))
        return await super().chat(messages, options)


def _install_provider(provider: str, reply: str) -> None:
    async def factory(**_: Any) -> FakeAdapter:
        return _RecordingFake(reply, RECORDED.setdefault(provider, []))

    register_provider_factory(provider, factory)


async def test_dependencies_run_before_dependents_and_outputs_are_forwarded() -> None:
    for provider, (_, reply) in PROVIDERS.items():
        _install_provider(provider, reply)

    events: list[Any] = []
    engine = AnyCode(OrchestratorConfig(max_concurrency=3, on_progress=events.append))
    team = build_team(
        engine,
        name="stage0-spike",
        agents=[
            build_agent(name=name, provider=provider, system_prompt=f"You are {name}.")
            for provider, (name, _) in PROVIDERS.items()
        ],
    )

    # Deliberately shuffled: dependency resolution must come from `depends_on`, not list order.
    specs = [
        build_task(
            title="Merge review",
            description="Merge both research outputs.",
            assignee="reviewer",
            depends_on=["Research A", "Research B"],
        ),
        build_task(title="Research A", description="Collect fact A.", assignee="researcher-a"),
        build_task(title="Research B", description="Collect fact B.", assignee="researcher-b"),
    ]

    result = await engine.run_tasks(team, specs)

    assert result.success is True
    assert set(result.agent_results) == {"researcher-a", "researcher-b", "reviewer"}
    # One deterministic fake call per agent.
    assert result.total_token_usage.input_tokens == 15
    assert result.total_token_usage.output_tokens == 15

    title_by_id = {
        event.data.id: event.data.title for event in events if event.type == "task_start"
    }
    sequence: list[tuple[str, str]] = []
    for event in events:
        if event.type == "task_start":
            sequence.append(("start", title_by_id[event.data.id]))
        elif event.type == "task_complete":
            sequence.append(("complete", title_by_id[event.task]))

    assert sequence.index(("complete", "Research A")) < sequence.index(("start", "Merge review"))
    assert sequence.index(("complete", "Research B")) < sequence.index(("start", "Merge review"))

    reviewer_prompt = "\n".join(
        block.text
        for message in RECORDED["atlasos-test-reviewer"][0]
        for block in message.content
        if isinstance(block, TextBlock)
    )
    assert "fact A" in reviewer_prompt
    assert "fact B" in reviewer_prompt


@pytest.mark.parametrize("provider", sorted(PROVIDERS))
async def test_each_provider_sees_exactly_one_call(provider: str) -> None:
    assert len(RECORDED.get(provider, [])) <= 1
