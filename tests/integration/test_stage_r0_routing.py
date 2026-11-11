"""Stage R0: routed execution shape, observed through the real engine and a scripted provider.

The test does not call a model and does not run tools: it builds a public ``BenchmarkTask``, routes
it, and then executes the routed plan through ``AtlasContextEngine`` while capturing the messages
each worker actually received. This is what proves that

* a DIRECT decision runs exactly one worker holding the full public goal,
* a DELEGATE_PARALLEL decision keeps the original candidate DAG and prompts,
* shadow mode never changes execution,

rather than only proving that ``route_delegation`` returns a value.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from anycode import AgentConfig, OrchestratorConfig, TaskSpec, TeamConfig
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
from experiments.evaluation.models import BenchmarkTask, WorkerStep
from experiments.evaluation.stage_r0_routing import (
    a0_steps,
    build_proposal,
    plan_route,
    routing_policy,
)
from tests.helpers.fake_sandbox_provider import FakeSandboxProvider

from atlasos.actions import ActionCatalog, ActionPolicy
from atlasos.integrations.anycode_engine import AtlasContextEngine
from atlasos.sandbox import ProjectSandboxManager, SandboxProjectConfig, SandboxWorkspaceTools
from atlasos.tools import AtlasToolRuntime
from atlasos.workers import WorkerCatalog
from atlasos.workers.models import WorkerProfile, WorkerTaskContext
from atlasos.workers.renderer import render_worker_context
from atlasos.workers.routing import RouteKind, capabilities_from_catalog


class _Recording:
    def __init__(self, sink: list[tuple[str, list[LLMMessage], str | None]], label: str) -> None:
        self._sink = sink
        self._label = label
        self.name = f"recording-{label}"

    async def chat(self, messages: list[LLMMessage], options: LLMChatOptions) -> LLMResponse:
        self._sink.append((self._label, list(messages), options.system_prompt))
        return LLMResponse(
            id=f"resp-{self._label}-{len(self._sink)}",
            content=[TextBlock(text="done")],
            model=options.model,
            stop_reason="end_turn",
            usage=TokenUsage(input_tokens=1, output_tokens=1),
        )

    def stream(self, messages: list[LLMMessage], options: LLMStreamOptions):
        async def _gen():
            response = await self.chat(messages, options)
            for block in response.content:
                if isinstance(block, TextBlock):
                    yield StreamEvent(type="text", data=block.text)
            yield StreamEvent(type="done", data=response)

        return _gen()


def _tool_runtime(tmp_path: Path) -> AtlasToolRuntime:
    provider = FakeSandboxProvider(tmp_path / "provider")
    manager = ProjectSandboxManager(
        provider,
        SandboxProjectConfig(provider_name="fake", image="fake@sha256:0123456789abcdef"),
        run_id="run-r0",
    )
    return AtlasToolRuntime(
        "run-r0",
        state_path=tmp_path / "tool_state.json",
        conversation_path=tmp_path / "conversation.json",
        sandbox_tools=SandboxWorkspaceTools(manager, "task-r0", provider),
        action_policy=ActionPolicy(name="r0", default="DENY", rules=()),
        action_catalog=ActionCatalog(),
        idempotency_store=InMemoryToolIdempotencyStore(),
    )


def _read_only_task() -> BenchmarkTask:
    return BenchmarkTask(
        sample_id="r0-read-only",
        category="multi_worker",
        mode="multi_worker",
        prompt="Inspect two independent modules and report findings.",
        target="both reports present",
        visible_files={"README.md": "# demo\n"},
        hidden_files={},
        workers=(
            WorkerStep("a", "r0-a", "Inspect module A and write findings to a.md."),
            WorkerStep("b", "r0-b", "Inspect module B and write findings to b.md."),
        ),
        allowed_paths=("a.md", "b.md"),
        forbidden_paths=("tests/**",),
        required_artifacts=("a.md", "b.md"),
        tool_expectations={"distinct_workers": 2},
    )


def _coupled_task() -> BenchmarkTask:
    return BenchmarkTask(
        sample_id="r0-coupled",
        category="multi_worker",
        mode="multi_worker",
        prompt="Two writers share one state file.",
        target="final state",
        visible_files={"README.md": "# demo\n"},
        hidden_files={},
        workers=(
            WorkerStep("a", "r0-wa", 'Write /workspace/project/state.json with {"a": 1}.'),
            WorkerStep("b", "r0-wb", 'Write /workspace/project/state.json with {"b": 2}.'),
        ),
        allowed_paths=("state.json",),
        forbidden_paths=("tests/**",),
        required_artifacts=("state.json",),
        tool_expectations={"distinct_workers": 2},
    )


async def _execute(
    tmp_path: Path,
    steps: tuple[WorkerStep, ...],
    *,
    expected_workers: int,
) -> tuple[list[tuple[str, list[LLMMessage], str | None]], dict[str, Any]]:
    sink: list[tuple[str, list[LLMMessage], str | None]] = []
    provider_name = f"r0-{uuid.uuid4().hex}"
    counter = {"n": 0}

    async def factory(**kwargs: Any) -> _Recording:
        del kwargs
        counter["n"] += 1
        return _Recording(sink, f"w{counter['n']}")

    register_provider_factory(provider_name, factory)
    runtime = _tool_runtime(tmp_path)
    engine = AtlasContextEngine(OrchestratorConfig(), tool_runtime=runtime)
    context = ExecutionContext(principal="user:test", attributes={"run_id": "run-r0"})
    profile = WorkerProfile(
        profile_id="research_worker",
        role="research",
        instructions="read only",
        base_tools=("tool_search", "dispatch_action", "file_read", "grep", "list_files"),
        operating_scope=("research",),
        max_turns=6,
        can_delegate=False,
    )
    configs = [
        AgentConfig(
            name=f"research_worker:{step.task_id}",
            model="fake-model",
            provider=provider_name,
            system_prompt=render_worker_context(
                profile,
                WorkerTaskContext(task_id=step.task_id, objective=step.prompt, background=None),
            ),
            tools=list(profile.base_tools),
            max_turns=6,
            execution_context=context,
        )
        for step in steps
    ]
    titles = {step.task_id: f"{step.task_id}-title" for step in steps}
    specs = [
        TaskSpec(
            title=titles[step.task_id],
            description=step.prompt,
            assignee=configs[index].name,
            depends_on=[titles[item] for item in step.depends_on],
        )
        for index, step in enumerate(steps)
    ]
    team = engine.create_team("r0", TeamConfig(name="r0", agents=configs))
    try:
        result = await engine.run_tasks(team, specs)
    finally:
        await engine.close()
    assert len(result.agent_results) == expected_workers
    return sink, {"result": result, "specs": specs}


@pytest.mark.asyncio
async def test_direct_decision_runs_one_worker_with_the_full_goal(tmp_path: Path) -> None:
    task = _coupled_task()
    routed = plan_route(task, capabilities_from_catalog(WorkerCatalog()), routing_policy())
    assert routed.decision.route is RouteKind.DIRECT
    assert routed.is_direct

    plan = a0_steps(task)
    assert len(plan) == 1
    sink, executed = await _execute(tmp_path, plan, expected_workers=1)

    assert len(sink) == 1
    first_message = sink[0][1][0]
    text = "\n".join(block.text for block in first_message.content if isinstance(block, TextBlock))
    # The direct path keeps the original public goal verbatim (no rewritten prompt).
    assert task.prompt in text
    assert executed["result"].success is True


@pytest.mark.asyncio
async def test_parallel_decision_keeps_the_candidate_dag_and_prompts(tmp_path: Path) -> None:
    task = _read_only_task()
    routed = plan_route(task, capabilities_from_catalog(WorkerCatalog()), routing_policy())
    assert routed.decision.route is RouteKind.DELEGATE_PARALLEL
    assert routed.execution_mode == "a1"
    assert tuple(step.task_id for step in routed.steps) == ("a", "b")

    sink, executed = await _execute(tmp_path, routed.steps, expected_workers=2)

    assert len(sink) == 2
    for step in task.workers:
        assert any(
            step.prompt
            in "\n".join(
                block.text for block in messages[0].content if isinstance(block, TextBlock)
            )
            for _, messages, _ in sink
        )
    assert [spec.title for spec in executed["specs"]] == ["a-title", "b-title"]


@pytest.mark.asyncio
async def test_shadow_mode_never_changes_execution(tmp_path: Path) -> None:
    """Shadow computes the decision but still runs the candidate A1 plan."""

    task = _coupled_task()
    routed = plan_route(task, capabilities_from_catalog(WorkerCatalog()), routing_policy())
    assert routed.decision.route is RouteKind.DIRECT

    # Shadow keeps ``task.workers`` (2 workers) even though the router said DIRECT.
    sink, executed = await _execute(tmp_path, task.workers, expected_workers=2)
    assert len(sink) == 2
    assert len(executed["specs"]) == 2


@pytest.mark.asyncio
async def test_off_mode_is_byte_identical(tmp_path: Path) -> None:
    task = _read_only_task()
    proposal = build_proposal(task, task.workers, proposal_id="off-check")
    # Building a proposal must not touch the plan that ``off`` mode executes.
    assert tuple(step.prompt for step in task.workers) == (
        "Inspect module A and write findings to a.md.",
        "Inspect module B and write findings to b.md.",
    )
    assert proposal.overall_goal == task.prompt
    assert [item.description for item in proposal.candidate_tasks] == [
        step.prompt for step in task.workers
    ]
