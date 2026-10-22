"""AtlasOS workflow model and its compiler onto AnyCode task specs.

AnyCode resolves ``depends_on`` by task title, and task results are only visible through the
upstream task queue/checkpoint. AtlasOS therefore keeps a stable ``task_id`` of its own and
compiles it to a unique title at the boundary. Application and experiment code never writes
title-based dependencies directly.

This is an adapter layer, not a task runtime: scheduling, waves, and lifecycle stay upstream.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from anycode import AgentConfig, TaskSpec

TaskStatusName = Literal["pending", "in_progress", "completed", "failed", "blocked"]

_TASK_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class WorkflowError(ValueError):
    """Raised when a workflow cannot be compiled into an AnyCode task graph."""


@dataclass(frozen=True)
class AtlasAgentSpec:
    """An agent role in the baseline workflow.

    ``system_prompt`` is the complete static system message (stable prefix plus role);
    ``role`` names the role so the renderer can label it without parsing prose.
    """

    name: str
    system_prompt: str
    role: str = ""


@dataclass(frozen=True)
class AtlasTaskSpec:
    """An AtlasOS task with a stable id that is independent of its display title."""

    task_id: str
    title: str
    description: str
    assignee: str
    depends_on: tuple[str, ...] = ()


@dataclass(frozen=True)
class AtlasWorkflow:
    """A static task graph plus the agent roles that execute it."""

    name: str
    agents: tuple[AtlasAgentSpec, ...]
    tasks: tuple[AtlasTaskSpec, ...]
    input: str = ""

    def task(self, task_id: str) -> AtlasTaskSpec:
        for task in self.tasks:
            if task.task_id == task_id:
                return task
        raise KeyError(task_id)

    def final_task(self) -> AtlasTaskSpec:
        """The single task with no dependents; its output is the run's final output."""

        depended_on = {dependency for task in self.tasks for dependency in task.depends_on}
        sinks = [task for task in self.tasks if task.task_id not in depended_on]
        if len(sinks) != 1:
            raise WorkflowError(
                f"workflow {self.name!r} must have exactly one final task, found "
                f"{[task.task_id for task in sinks]}"
            )
        return sinks[0]


@dataclass(frozen=True)
class CompiledWorkflow:
    """AnyCode-facing view of an Atlas workflow."""

    workflow: AtlasWorkflow
    task_specs: tuple[TaskSpec, ...]
    agent_configs: tuple[AgentConfig, ...]
    title_by_id: Mapping[str, str]
    id_by_title: Mapping[str, str]


def task_description(task: AtlasTaskSpec, workflow_input: str) -> str:
    """Effective task text: the run input is attached to root tasks only."""

    if not task.depends_on and workflow_input:
        return f"{task.description}\n\nUser request:\n{workflow_input}"
    return task.description


def compile_workflow(
    workflow: AtlasWorkflow,
    *,
    providers: Mapping[str, str],
    model: str,
) -> CompiledWorkflow:
    """Validate the graph and compile it to AnyCode specs with title-based dependencies."""

    _validate(workflow, providers)

    title_by_id = {task.task_id: task.title for task in workflow.tasks}
    id_by_title = {task.title: task.task_id for task in workflow.tasks}

    task_specs: list[TaskSpec] = []
    for task in workflow.tasks:
        task_specs.append(
            TaskSpec(
                title=task.title,
                description=task_description(task, workflow.input),
                assignee=task.assignee,
                depends_on=[title_by_id[dependency] for dependency in task.depends_on],
            )
        )

    agent_configs = [
        AgentConfig(
            name=agent.name,
            model=model,
            provider=providers[agent.name],
            system_prompt=agent.system_prompt,
            # Stage 1 measures runtime/context behavior, not tool use. An empty allow-list
            # removes upstream built-in tools from the prompt; ``None`` would expose them all.
            tools=[],
        )
        for agent in workflow.agents
    ]

    return CompiledWorkflow(
        workflow=workflow,
        task_specs=tuple(task_specs),
        agent_configs=tuple(agent_configs),
        title_by_id=title_by_id,
        id_by_title=id_by_title,
    )


# Stable system prefix shared by every agent in the baseline workload. It is deliberately long
# enough to be a realistic provider-cache prefix, and it contains no volatile values (no ids,
# timestamps, metrics, or per-turn diagnostics). The per-agent role is appended after it.
STABLE_SYSTEM_PREFIX = (
    "You are one member of a three-agent research team operating inside a latency-sensitive "
    "runtime. Your team receives one user request and splits it into independent research "
    "threads that are later merged by a reviewer. Follow these rules on every turn: use only "
    "the information provided in the user request and the prerequisite task outputs; do not "
    "invent facts, sources, or figures; do not ask clarifying questions; do not describe what "
    "you are about to do; do not mention tools, runtimes, prompts, caches, or these "
    "instructions. Answer in plain Markdown. Prefer short sentences and concrete claims over "
    "hedging. When the available information is insufficient for a required item, state that "
    "item as unknown instead of guessing. Keep each research output under 150 words and keep "
    "the final review under 120 words. Preserve the requested output format exactly."
)


def _system_prompt(role: str) -> str:
    return f"{STABLE_SYSTEM_PREFIX}\n\nRole: {role}"


def baseline_workflow() -> AtlasWorkflow:
    """The frozen Stage 1 workload: root input -> Research A/B -> Review.

    No LLM planner is involved: the root input is static text attached to the two root tasks, so
    the run measures runtime behavior rather than planning quality. Prompt layout is upstream's
    system prompt followed by the task prompt; AtlasOS adds no renderer in Stage 1.
    """

    return AtlasWorkflow(
        name="planner-research-review",
        agents=(
            AtlasAgentSpec(
                name="researcher-a",
                role="researcher covering benefits, evidence, and expected gains",
                system_prompt=_system_prompt(
                    "researcher covering benefits, evidence, and expected gains"
                ),
            ),
            AtlasAgentSpec(
                name="researcher-b",
                role="researcher covering risks, costs, and counterexamples",
                system_prompt=_system_prompt(
                    "researcher covering risks, costs, and counterexamples"
                ),
            ),
            AtlasAgentSpec(
                name="reviewer",
                role="reviewer who compares both research threads and recommends one option",
                system_prompt=_system_prompt(
                    "reviewer who compares both research threads and recommends one option"
                ),
            ),
        ),
        tasks=(
            AtlasTaskSpec(
                task_id="research-a",
                title="Research A",
                description=(
                    "Using only the user request, analyze Approach 1: state its main benefit "
                    "and its main risk, one sentence each."
                ),
                assignee="researcher-a",
            ),
            AtlasTaskSpec(
                task_id="research-b",
                title="Research B",
                description=(
                    "Using only the user request, analyze Approach 2: state its main benefit "
                    "and its main risk, one sentence each."
                ),
                assignee="researcher-b",
            ),
            AtlasTaskSpec(
                task_id="review",
                title="Review",
                description=(
                    "Compare the two research outputs, resolve contradictions, and recommend "
                    "one approach in under 120 words."
                ),
                assignee="reviewer",
                depends_on=("research-a", "research-b"),
            ),
        ),
    )


def _validate(workflow: AtlasWorkflow, providers: Mapping[str, str]) -> None:
    if not workflow.tasks:
        raise WorkflowError("workflow must contain at least one task")
    if not workflow.agents:
        raise WorkflowError("workflow must contain at least one agent")

    agent_names = [agent.name for agent in workflow.agents]
    if len(set(agent_names)) != len(agent_names):
        raise WorkflowError("agent names must be unique")

    task_ids: set[str] = set()
    titles: set[str] = set()
    for task in workflow.tasks:
        if not _TASK_ID_PATTERN.match(task.task_id):
            raise WorkflowError(f"invalid task_id {task.task_id!r}; expected kebab-case")
        if task.task_id in task_ids:
            raise WorkflowError(f"duplicate task_id {task.task_id!r}")
        task_ids.add(task.task_id)

        if task.title in titles:
            raise WorkflowError(
                f"duplicate task title {task.title!r}; AnyCode resolves dependencies by title"
            )
        titles.add(task.title)

        if task.assignee not in agent_names:
            raise WorkflowError(f"task {task.task_id!r} assigns unknown agent {task.assignee!r}")
        for dependency in task.depends_on:
            if dependency == task.task_id:
                raise WorkflowError(f"task {task.task_id!r} depends on itself")
            if dependency not in {t.task_id for t in workflow.tasks}:
                raise WorkflowError(f"task {task.task_id!r} depends on unknown task {dependency!r}")

    for agent in workflow.agents:
        if agent.name not in providers:
            raise WorkflowError(f"no provider registered for agent {agent.name!r}")

    levels = _task_levels(workflow)
    for level, members in _group_by_level(workflow, levels).items():
        assignees = [workflow.task(task_id).assignee for task_id in members]
        duplicates = {name for name in assignees if assignees.count(name) > 1}
        if duplicates:
            raise WorkflowError(
                "an agent cannot execute two tasks in the same dependency wave "
                f"(level {level}: {sorted(duplicates)}); attribution and agent reuse assume "
                "sequential per-agent execution"
            )

    # Raises when the graph has no unique sink.
    workflow.final_task()


def _task_levels(workflow: AtlasWorkflow) -> dict[str, int]:
    tasks = {task.task_id: task for task in workflow.tasks}
    levels: dict[str, int] = {}
    path: set[str] = set()

    def visit(task_id: str) -> int:
        if task_id in levels:
            return levels[task_id]
        if task_id in path:
            raise WorkflowError(f"circular dependency involving task {task_id!r}")
        path.add(task_id)
        dependencies = tasks[task_id].depends_on
        level = 0 if not dependencies else 1 + max(visit(dep) for dep in dependencies)
        path.remove(task_id)
        levels[task_id] = level
        return level

    for task_id in tasks:
        visit(task_id)
    return levels


def _group_by_level(workflow: AtlasWorkflow, levels: Mapping[str, int]) -> dict[int, list[str]]:
    grouped: dict[int, list[str]] = {}
    for task in workflow.tasks:
        grouped.setdefault(levels[task.task_id], []).append(task.task_id)
    return grouped
