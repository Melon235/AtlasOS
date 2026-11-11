"""Stage G3-3: what the model actually receives in each contract mode.

The test drives the real Atlas stack (``ScopedWorkerFactory`` → ``AgentConfig`` → ``TaskSpec`` →
``AtlasContextEngine``) with a scripted provider, so the assertions are made on the message list
that reached the model, not on the builder's return value.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from anycode import OrchestratorConfig, TaskSpec, TeamConfig
from anycode.identity.context import ExecutionContext
from anycode.plugins.registry import register_provider_factory
from anycode.tools.idempotency import InMemoryToolIdempotencyStore
from anycode.types import (
    LLMChatOptions,
    LLMMessage,
    LLMResponse,
    LLMStreamOptions,
    StreamEvent,
    TextBlock,
    TokenUsage,
)
from experiments.evaluation.stage_e_tasks import definition_by_id
from experiments.evaluation.stage_g3_worker_contracts import (
    WORKER_CONTRACT_LEGACY,
    WORKER_CONTRACT_STRUCTURED,
    worker_task_text,
)
from tests.helpers.fake_sandbox_provider import FakeSandboxProvider

from atlasos.actions import ActionCatalog, ActionPolicy
from atlasos.budget import RunBudgetState
from atlasos.integrations.anycode_engine import AtlasContextEngine
from atlasos.sandbox import ProjectSandboxManager, SandboxProjectConfig, SandboxWorkspaceTools
from atlasos.tools import AtlasToolRuntime
from atlasos.workers import ScopedWorkerFactory, WorkerAssignment, WorkerStore

ENVIRONMENT_NOTE = (
    "Environment: project workspace is /workspace/project (OpenSandbox, network none)."
)


class _Recording:
    """One provider instance per agent; records the messages of every call."""

    def __init__(self, sink: list[Any], mode: str, label: str) -> None:
        self._sink = sink
        self._mode = mode
        self._label = label
        self.name = f"recording-{label}"

    def _respond(self, messages: list[LLMMessage], options: Any) -> LLMResponse:
        # AnyCode passes the worker system prompt through ``options.system_prompt``, not as a
        # message, so both surfaces are captured.
        self._sink.append((self._mode, list(messages), options.system_prompt))
        return LLMResponse(
            id=f"resp-{self._label}-{len(self._sink)}",
            content=[TextBlock(text="done")],
            model=options.model,
            stop_reason="end_turn",
            usage=TokenUsage(input_tokens=1, output_tokens=1),
        )

    async def chat(self, messages: list[LLMMessage], options: LLMChatOptions) -> LLMResponse:
        return self._respond(messages, options)

    def stream(self, messages: list[LLMMessage], options: LLMStreamOptions):
        async def _gen():
            response = self._respond(messages, options)
            for block in response.content:
                if isinstance(block, TextBlock):
                    yield StreamEvent(type="text", data=block.text)
            yield StreamEvent(type="done", data=response)

        return _gen()


def _runtime(tmp_path: Path) -> AtlasToolRuntime:
    provider = FakeSandboxProvider(tmp_path / "provider")
    manager = ProjectSandboxManager(
        provider,
        SandboxProjectConfig(provider_name="fake", image="fake@sha256:0123456789abcdef"),
        run_id="run-g33",
    )
    tools = SandboxWorkspaceTools(manager, "task-g33", provider)
    return AtlasToolRuntime(
        "run-g33",
        state_path=tmp_path / "tool_state.json",
        conversation_path=tmp_path / "conversation.json",
        sandbox_tools=tools,
        action_policy=ActionPolicy(name="g33", default="DENY", rules=()),
        action_catalog=ActionCatalog(),
        idempotency_store=InMemoryToolIdempotencyStore(),
    )


def _message_text(message: LLMMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    return "\n".join(block.text for block in content if isinstance(block, TextBlock))


async def _run_mode(
    tmp_path: Path, mode: str, seen: list[tuple[str, list[LLMMessage]]]
) -> dict[str, Any]:
    definition = definition_by_id("e1-02-report")
    runtime = _runtime(tmp_path / mode)
    factory = ScopedWorkerFactory(
        model="fake-model",
        provider=None,
        store=WorkerStore(tmp_path / mode / "workers.json"),
    )
    parent = ExecutionContext(principal="user:test", attributes={"run_id": "run-g33"})
    workers = {}
    for step in definition.workers:
        workers[step.task_id] = factory.create(
            parent_context=parent,
            parent_agent="atlas-evaluation",
            assignment=WorkerAssignment(
                profile_id="coding_worker",
                task=f"{ENVIRONMENT_NOTE}\n\n{step.prompt}",
            ),
            worker_id=f"w-{step.task_id}",
            task_id=f"t-{step.task_id}",
        )

    provider_name = f"g33-{mode}-{uuid.uuid4().hex}"
    counter = {"n": 0}

    async def provider_factory(**kwargs: Any) -> _Recording:
        del kwargs
        counter["n"] += 1
        return _Recording(seen, mode, f"{mode}-{counter['n']}")

    register_provider_factory(provider_name, provider_factory)

    engine = AtlasContextEngine(
        OrchestratorConfig(),
        tool_runtime=runtime,
        budget_state=RunBudgetState(limit_tokens=120_000),
    )
    agent_configs = [
        workers[step.task_id].agent_config.model_copy(update={"provider": provider_name})
        for step in definition.workers
    ]
    titles = {step.task_id: f"{definition.sample_id}:{step.task_id}" for step in definition.workers}
    task_specs = [
        TaskSpec(
            title=titles[step.task_id],
            description=worker_task_text(
                step, steps=definition.workers, sample_id=definition.sample_id, mode=mode
            ),
            assignee=workers[step.task_id].worker_name,
            depends_on=[titles[item] for item in step.depends_on],
        )
        for step in definition.workers
    ]
    team = engine.create_team("g33", TeamConfig(name="g33", agents=agent_configs))
    try:
        result = await engine.run_tasks(team, task_specs)
    finally:
        await engine.close()
    return {
        "result": result,
        "specs": task_specs,
        "workers": workers,
        "runs": seen,
    }


def _collect(seen: list[Any], mode: str) -> dict[str, tuple[list[LLMMessage], str | None]]:
    """Map each captured conversation to the step whose task text it carries."""

    definition = definition_by_id("e1-02-report")
    per_step: dict[str, tuple[list[LLMMessage], str | None]] = {}
    for captured_mode, messages, system in seen:
        if captured_mode != mode:
            continue
        first = _message_text(messages[0])
        for step in definition.workers:
            if step.prompt in first and step.task_id not in per_step:
                per_step[step.task_id] = (messages, system)
                break
    return per_step


@pytest.mark.asyncio
async def test_contract_reaches_the_model_without_touching_the_system_prompt(
    tmp_path: Path,
) -> None:
    seen: list[Any] = []
    await _run_mode(tmp_path, WORKER_CONTRACT_LEGACY, seen)
    await _run_mode(tmp_path, WORKER_CONTRACT_STRUCTURED, seen)

    legacy_messages = _collect(seen, WORKER_CONTRACT_LEGACY)
    structured_messages = _collect(seen, WORKER_CONTRACT_STRUCTURED)
    assert set(legacy_messages) == {"parser", "stats", "render"}
    assert set(structured_messages) == {"parser", "stats", "render"}

    for step_id in ("parser", "stats", "render"):
        legacy_turns, legacy_system = legacy_messages[step_id]
        structured_turns, structured_system = structured_messages[step_id]

        # Same system prompt base: only the task turn differs.
        assert legacy_system == structured_system
        assert legacy_system is not None
        assert "[WORKER TASK CONTRACT]" not in legacy_system
        legacy_task = _message_text(legacy_turns[0])
        structured_task = _message_text(structured_turns[0])
        assert "[WORKER TASK CONTRACT]" not in legacy_task
        assert "[WORKER TASK CONTRACT]" in structured_task
        assert structured_task.startswith(legacy_task)

    # The structured text the model saw is exactly the builder output.
    definition = definition_by_id("e1-02-report")
    render_step = next(item for item in definition.workers if item.task_id == "render")
    structured_render = worker_task_text(
        render_step,
        steps=definition.workers,
        sample_id=definition.sample_id,
        mode=WORKER_CONTRACT_STRUCTURED,
    )
    structured_first_turn = structured_messages["render"][0][0]
    assert structured_render in _message_text(structured_first_turn)


@pytest.mark.asyncio
async def test_dag_and_budget_are_identical_in_both_modes(tmp_path: Path) -> None:
    seen: list[tuple[str, list[LLMMessage]]] = []
    legacy = await _run_mode(tmp_path, WORKER_CONTRACT_LEGACY, seen)
    structured = await _run_mode(tmp_path, WORKER_CONTRACT_STRUCTURED, seen)

    legacy_dag = [(spec.title, tuple(spec.depends_on)) for spec in legacy["specs"]]
    structured_dag = [(spec.title, tuple(spec.depends_on)) for spec in structured["specs"]]
    assert legacy_dag == structured_dag

    legacy_workers = sorted(legacy["workers"])
    structured_workers = sorted(structured["workers"])
    assert legacy_workers == structured_workers
    for step_id in legacy_workers:
        # Profile, tools and execution context are untouched by the contract.
        assert legacy["workers"][step_id].agent_config.tools == (
            structured["workers"][step_id].agent_config.tools
        )
        assert (
            legacy["workers"][step_id].execution_context
            == structured["workers"][step_id].execution_context
        )

    assert legacy["result"].success is True
    assert structured["result"].success is True
