"""AnyCode runtime integration.

AtlasOS does not own orchestration. This module only wires AtlasOS adapters into AnyCode's
documented extension points (provider factories) and builds teams/tasks for baseline
experiments, so that upstream scheduling and lifecycle semantics stay untouched.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from anycode import AgentConfig, AnyCode, FakeAdapter, FakeResponse, TaskSpec, Team, TeamConfig
from anycode.plugins.registry import register_provider_factory

_FAKE_FACTORIES: dict[str, Callable[..., Awaitable[FakeAdapter]]] = {}


def register_fake_provider(name: str, reply: str) -> None:
    """Register a deterministic AnyCode provider whose agent always answers with *reply*.

    Used by offline tests and the Stage 0 spike; live providers are registered by
    ``atlasos.integrations.deepseek.register_deepseek_provider``.
    """

    if name in _FAKE_FACTORIES:
        return

    async def factory(**_: Any) -> FakeAdapter:
        return FakeAdapter(responses=[FakeResponse(text=reply)])

    _FAKE_FACTORIES[name] = factory
    register_provider_factory(name, factory)


def build_agent(
    *,
    name: str,
    provider: str,
    system_prompt: str | None = None,
    model: str = "fake-model",
) -> AgentConfig:
    """Build an upstream agent config; AtlasOS adds no behavior here."""

    return AgentConfig(name=name, model=model, provider=provider, system_prompt=system_prompt)


def build_team(engine: AnyCode, *, name: str, agents: Sequence[AgentConfig]) -> Team:
    """Create a team through the upstream engine."""

    return engine.create_team(name, TeamConfig(name=name, agents=list(agents)))


def build_task(
    *,
    title: str,
    description: str,
    assignee: str | None = None,
    depends_on: Sequence[str] | None = None,
) -> TaskSpec:
    """Build an upstream task spec. ``depends_on`` matches prerequisite task *titles*."""

    return TaskSpec(
        title=title,
        description=description,
        assignee=assignee,
        depends_on=list(depends_on or []),
    )
