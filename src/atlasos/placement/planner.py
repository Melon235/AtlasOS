"""Deterministic placement planner for the pre-placement fallback.

AnyCode 0.9.0 assigns agents once before building waves and exposes no per-ready-task hook, so
AtlasOS plans assignees deterministically before compilation and lets AnyCode execute the
already-placed DAG. This is placement planning, not dynamic runtime scheduling.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace

from atlasos.context.models import ContextRef
from atlasos.placement.models import (
    AgentContextState,
    AgentProfile,
    PlacementDecision,
    PlacementSummary,
    TaskRequirements,
)
from atlasos.placement.policies import (
    CapabilityOnlyPolicy,
    ContextLocalityPolicy,
    PlacementWeights,
    StaticPlacementPolicy,
)
from atlasos.workflow import AtlasWorkflow

Policy = StaticPlacementPolicy | CapabilityOnlyPolicy | ContextLocalityPolicy


@dataclass(frozen=True)
class PlacementPlan:
    workflow: AtlasWorkflow
    decisions: tuple[PlacementDecision, ...]
    summary: PlacementSummary


def plan_workflow(
    workflow: AtlasWorkflow,
    *,
    policy: Policy,
    weights: PlacementWeights | None = None,
) -> PlacementPlan:
    """Assign every task to an agent deterministically, wave by wave."""

    weights = weights or PlacementWeights()
    profiles = [
        AgentProfile(agent_id=agent.name, capabilities=agent.capabilities)
        for agent in workflow.agents
    ]
    states = {profile.agent_id: AgentContextState(profile.agent_id) for profile in profiles}
    loads = {profile.agent_id: 0 for profile in profiles}

    levels = dependency_levels(workflow)
    decisions: list[PlacementDecision] = []
    placed: dict[str, str] = {}
    tasks = {task.task_id: task for task in workflow.tasks}

    for level in sorted(set(levels.values())):
        wave = sorted(task_id for task_id, value in levels.items() if value == level)
        used_this_wave: set[str] = set()
        for task_id in wave:
            task = tasks[task_id]
            requirements = TaskRequirements(
                required_capabilities=task.required_capabilities,
                required_context=task.required_context,
            )
            candidates = [profile for profile in profiles if profile.agent_id not in used_this_wave]
            if not candidates:
                raise ValueError(f"no available agent for task {task_id!r} in wave {level}")
            decision = policy.choose(
                task=task,
                requirements=requirements,
                candidates=candidates,
                agent_context=states,
                load=loads,
                weights=weights,
            )
            decisions.append(decision)
            placed[task_id] = decision.agent_id
            used_this_wave.add(decision.agent_id)
            loads[decision.agent_id] = loads.get(decision.agent_id, 0) + 1

            state = states[decision.agent_id]
            for dependency in task.depends_on:
                state.consumed_task_ids.add(dependency)
                state.add_ref(ContextRef(f"task-output:{dependency}", 1))
            state.produced_task_ids.add(task_id)
            state.add_ref(ContextRef(f"task-output:{task_id}", 1))

    placed_workflow = replace(
        workflow,
        tasks=tuple(replace(task, assignee=placed[task.task_id]) for task in workflow.tasks),
    )
    summary = summarize_plan(placed_workflow, decisions)
    return PlacementPlan(workflow=placed_workflow, decisions=tuple(decisions), summary=summary)


def dependency_levels(workflow: AtlasWorkflow) -> dict[str, int]:
    tasks = {task.task_id: task for task in workflow.tasks}
    levels: dict[str, int] = {}

    def visit(task_id: str, path: set[str]) -> int:
        if task_id in levels:
            return levels[task_id]
        if task_id in path:
            raise ValueError(f"circular dependency at {task_id!r}")
        dependencies = tasks[task_id].depends_on
        value = (
            0 if not dependencies else 1 + max(visit(dep, path | {task_id}) for dep in dependencies)
        )
        levels[task_id] = value
        return value

    for task_id in tasks:
        visit(task_id, set())
    return levels


def summarize_plan(
    workflow: AtlasWorkflow,
    decisions: Sequence[PlacementDecision],
) -> PlacementSummary:
    assignees = {task.task_id: task.assignee for task in workflow.tasks}
    switches = 0
    for task in workflow.tasks:
        for dependency in task.depends_on:
            if assignees[dependency] != task.assignee:
                switches += 1

    contextual = [decision for decision in decisions if decision.required_context_count > 0]
    mean_locality = (
        sum(decision.context_locality for decision in contextual) / len(contextual)
        if contextual
        else 0.0
    )
    distribution: dict[str, int] = {}
    for assignee in assignees.values():
        distribution[assignee] = distribution.get(assignee, 0) + 1

    return PlacementSummary(
        policy_name=decisions[0].policy_name if decisions else "static_baseline",
        total_tasks=len(workflow.tasks),
        dependency_agent_switches=switches,
        mean_assignment_locality=mean_locality,
        total_logical_handoff_refs=sum(d.logical_handoff_refs for d in decisions),
        total_logical_handoff_chars=0,
        assignment_distribution=distribution,
    )


def placement_stress_workflow() -> AtlasWorkflow:
    """Two dependency chains plus a merge, with interchangeable workers."""

    from atlasos.workflow import AtlasAgentSpec, AtlasTaskSpec

    def chain(prefix: str, offset: int, capability: str = "research"):
        tasks = []
        previous: str | None = None
        workers = ("worker_a", "worker_b", "worker_c")
        for index in (1, 2, 3):
            task_id = f"{prefix.lower()}{index}"
            dependencies: tuple[str, ...] = () if previous is None else (previous,)
            from atlasos.context.requirements import ContextRequirement

            requirements = (
                ()
                if previous is None
                else (ContextRequirement(f"task-output:{previous}", previous),)
            )
            tasks.append(
                AtlasTaskSpec(
                    task_id=task_id,
                    title=f"{prefix}{index}",
                    description=f"Chain {prefix} step {index}.",
                    assignee=workers[(index - 1 + offset) % 3],
                    depends_on=dependencies,
                    required_capabilities=frozenset({capability}),
                    required_context=requirements,
                )
            )
            previous = task_id
        return tasks

    tasks = [*chain("A", 0), *chain("B", 1)]
    tasks.append(
        AtlasTaskSpec(
            task_id="merge",
            title="Merge",
            description="Merge both chains.",
            assignee="reviewer",
            depends_on=("a3", "b3"),
            required_capabilities=frozenset({"review"}),
            required_context=(
                from_requirement("a3"),
                from_requirement("b3"),
            ),
        )
    )
    return AtlasWorkflow(
        name="placement-stress",
        agents=(
            AtlasAgentSpec(
                name="worker_a",
                system_prompt="worker",
                role="worker",
                capabilities=frozenset({"research"}),
            ),
            AtlasAgentSpec(
                name="worker_b",
                system_prompt="worker",
                role="worker",
                capabilities=frozenset({"research"}),
            ),
            AtlasAgentSpec(
                name="worker_c",
                system_prompt="worker",
                role="worker",
                capabilities=frozenset({"research"}),
            ),
            AtlasAgentSpec(
                name="reviewer",
                system_prompt="reviewer",
                role="reviewer",
                capabilities=frozenset({"review"}),
            ),
        ),
        tasks=tuple(tasks),
    )


def from_requirement(source_task_id: str):
    from atlasos.context.requirements import ContextRequirement

    return ContextRequirement(f"task-output:{source_task_id}", source_task_id)
