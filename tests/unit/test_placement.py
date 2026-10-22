"""Stage 4 placement tests: capability filtering, locality, versioning, determinism."""

from __future__ import annotations

import pytest

from atlasos.context.models import ContextRef
from atlasos.context.requirements import ContextRequirement
from atlasos.placement import (
    AgentContextState,
    AgentProfile,
    CapabilityOnlyPolicy,
    ContextLocalityPolicy,
    PlacementWeights,
    placement_stress_workflow,
    plan_workflow,
)
from atlasos.workflow import AtlasAgentSpec, AtlasTaskSpec, AtlasWorkflow


def _state(agent_id: str, *task_ids: str, refs: tuple[ContextRef, ...] = ()) -> AgentContextState:
    state = AgentContextState(agent_id)
    state.produced_task_ids.update(task_ids)
    state.available_refs.update(refs)
    return state


def _requirements(*sources: str, version: int | None = None):
    from atlasos.placement.models import TaskRequirements

    return TaskRequirements(
        required_capabilities=frozenset(),
        required_context=tuple(
            ContextRequirement(f"task-output:{source}", source, version) for source in sources
        ),
    )


def _task() -> AtlasTaskSpec:
    return AtlasTaskSpec(task_id="merge", title="Merge", description="x", assignee="w")


def _profiles(*names: str) -> list[AgentProfile]:
    return [AgentProfile(name, frozenset({"research"})) for name in names]


def test_locality_is_matched_over_required() -> None:
    policy = ContextLocalityPolicy()
    states = {
        "agent_1": _state("agent_1", "a", "b"),
        "agent_2": _state("agent_2", "c"),
        "agent_3": _state("agent_3"),
    }
    decision = policy.choose(
        task=_task(),
        requirements=_requirements("a", "b", "c"),
        candidates=_profiles("agent_1", "agent_2", "agent_3"),
        agent_context=states,
        load={},
        weights=PlacementWeights(locality=1.0, load=0.0),
    )
    scores = decision.candidate_scores
    assert scores["agent_1"]["locality"] == pytest.approx(2 / 3)
    assert scores["agent_2"]["locality"] == pytest.approx(1 / 3)
    assert scores["agent_3"]["locality"] == 0.0
    assert decision.agent_id == "agent_1"
    assert decision.logical_handoff_refs == 1


def test_version_mismatch_does_not_count_as_locality() -> None:
    state = _state("a", refs=(ContextRef("task-output:a1", 1),))
    assert not state.has_requirement(ContextRequirement("task-output:a1", "a1", required_version=2))
    assert state.has_requirement(ContextRequirement("task-output:a1", "a1", required_version=1))


def test_failure_does_not_add_output_state() -> None:
    state = AgentContextState("worker")
    state.consumed_task_ids.add("input")
    assert "output" not in state.produced_task_ids


def test_capability_filter_rejects_incapable_agents() -> None:
    policy = CapabilityOnlyPolicy()
    from atlasos.placement.models import TaskRequirements

    requirements = TaskRequirements(required_capabilities=frozenset({"review"}))
    with pytest.raises(ValueError, match="no eligible agent"):
        policy.choose(
            task=_task(),
            requirements=requirements,
            candidates=[AgentProfile("worker", frozenset({"research"}))],
            agent_context={"worker": _state("worker")},
            load={},
            weights=PlacementWeights(),
        )


def test_equal_scores_are_deterministic() -> None:
    policy = CapabilityOnlyPolicy()
    calls = [
        policy.choose(
            task=_task(),
            requirements=_requirements(),
            candidates=_profiles("w1", "w2", "w3"),
            agent_context={},
            load={},
            weights=PlacementWeights(),
        ).agent_id
        for _ in range(5)
    ]
    assert len(set(calls)) == 1


def test_locality_policy_reduces_chain_switches(tmp_path) -> None:
    workflow = placement_stress_workflow()
    capability = plan_workflow(workflow, policy=CapabilityOnlyPolicy())
    locality = plan_workflow(workflow, policy=ContextLocalityPolicy())

    assert capability.summary.dependency_agent_switches > locality.summary.dependency_agent_switches
    assert (
        locality.summary.total_logical_handoff_refs < capability.summary.total_logical_handoff_refs
    )
    assert locality.summary.total_tasks == 7
    assert all(task.assignee for task in locality.workflow.tasks)


def test_workflow_requirements_are_explicit() -> None:
    workflow = placement_stress_workflow()
    a2 = next(task for task in workflow.tasks if task.task_id == "a2")

    assert "research" in a2.required_capabilities
    assert a2.required_context[0].source_task_id == "a1"


def test_planner_requires_capable_reviewer() -> None:
    workflow = AtlasWorkflow(
        name="review",
        agents=(
            AtlasAgentSpec("worker", "w", "worker", frozenset({"research"})),
            AtlasAgentSpec("reviewer", "r", "reviewer", frozenset({"review"})),
        ),
        tasks=(
            AtlasTaskSpec(
                "merge",
                "Merge",
                "m",
                "worker",
                required_capabilities=frozenset({"review"}),
            ),
        ),
    )

    plan = plan_workflow(workflow, policy=CapabilityOnlyPolicy())

    assert plan.workflow.tasks[0].assignee == "reviewer"
