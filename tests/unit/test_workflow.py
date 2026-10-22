from __future__ import annotations

import pytest

from atlasos.workflow import (
    STABLE_SYSTEM_PREFIX,
    AtlasAgentSpec,
    AtlasTaskSpec,
    AtlasWorkflow,
    WorkflowError,
    baseline_workflow,
    compile_workflow,
)

PROVIDERS = {"a": "provider-a", "b": "provider-b"}
BASELINE_PROVIDERS = {
    "researcher-a": "p-a",
    "researcher-b": "p-b",
    "reviewer": "p-r",
}


def _workflow(
    tasks: tuple[AtlasTaskSpec, ...],
    *,
    input_text: str = "",
) -> AtlasWorkflow:
    return AtlasWorkflow(
        name="test-workflow",
        agents=(AtlasAgentSpec("a", "you are a"), AtlasAgentSpec("b", "you are b")),
        tasks=tasks,
        input=input_text,
    )


def test_compiles_stable_ids_to_title_dependencies() -> None:
    workflow = _workflow(
        (
            AtlasTaskSpec("root", "Root Task", "do root", "a"),
            AtlasTaskSpec("join", "Join Task", "join", "b", ("root",)),
        )
    )

    compiled = compile_workflow(workflow, providers=PROVIDERS, model="test-model")

    assert [spec.title for spec in compiled.task_specs] == ["Root Task", "Join Task"]
    assert compiled.task_specs[1].depends_on == ["Root Task"]
    assert compiled.title_by_id == {"root": "Root Task", "join": "Join Task"}
    assert compiled.id_by_title == {"Root Task": "root", "Join Task": "join"}
    assert compiled.task_specs[0].assignee == "a"
    assert compiled.agent_configs[1].provider == "provider-b"
    # Stage 1 baseline exposes no upstream tools.
    assert compiled.agent_configs[0].tools == []


def test_root_tasks_receive_the_run_input_and_dependents_do_not() -> None:
    workflow = _workflow(
        (
            AtlasTaskSpec("root", "Root", "do root", "a"),
            AtlasTaskSpec("join", "Join", "join", "b", ("root",)),
        ),
        input_text="compare two designs",
    )

    compiled = compile_workflow(workflow, providers=PROVIDERS, model="m")

    assert "compare two designs" in compiled.task_specs[0].description
    assert "compare two designs" not in compiled.task_specs[1].description


def test_duplicate_titles_are_rejected_because_anycode_resolves_by_title() -> None:
    workflow = _workflow(
        (
            AtlasTaskSpec("one", "Same Title", "first", "a"),
            AtlasTaskSpec("two", "Same Title", "second", "b"),
        )
    )

    with pytest.raises(WorkflowError, match="duplicate task title"):
        compile_workflow(workflow, providers=PROVIDERS, model="m")


def test_unknown_dependency_is_rejected() -> None:
    workflow = _workflow((AtlasTaskSpec("one", "One", "x", "a", ("missing",)),))

    with pytest.raises(WorkflowError, match="unknown task"):
        compile_workflow(workflow, providers=PROVIDERS, model="m")


def test_unknown_assignee_is_rejected() -> None:
    workflow = _workflow((AtlasTaskSpec("one", "One", "x", "ghost"),))

    with pytest.raises(WorkflowError, match="unknown agent"):
        compile_workflow(workflow, providers=PROVIDERS, model="m")


def test_cycles_are_rejected() -> None:
    workflow = _workflow(
        (
            AtlasTaskSpec("one", "One", "x", "a", ("two",)),
            AtlasTaskSpec("two", "Two", "y", "b", ("one",)),
        )
    )

    with pytest.raises(WorkflowError, match="circular dependency"):
        compile_workflow(workflow, providers=PROVIDERS, model="m")


def test_same_agent_cannot_run_two_tasks_in_one_wave() -> None:
    workflow = _workflow(
        (
            AtlasTaskSpec("one", "One", "x", "a"),
            AtlasTaskSpec("two", "Two", "y", "a"),
            AtlasTaskSpec("three", "Three", "z", "b", ("one", "two")),
        )
    )

    with pytest.raises(WorkflowError, match="same dependency wave"):
        compile_workflow(workflow, providers=PROVIDERS, model="m")


def test_workflow_requires_exactly_one_final_task() -> None:
    workflow = _workflow(
        (
            AtlasTaskSpec("one", "One", "x", "a"),
            AtlasTaskSpec("two", "Two", "y", "b"),
        )
    )

    with pytest.raises(WorkflowError, match="exactly one final task"):
        compile_workflow(workflow, providers=PROVIDERS, model="m")


def test_baseline_workflow_has_two_parallel_roots_and_one_merge() -> None:
    workflow = baseline_workflow()

    roots = [task for task in workflow.tasks if not task.depends_on]
    merge = workflow.final_task()

    assert {task.task_id for task in workflow.tasks} == {"research-a", "research-b", "review"}
    assert {task.task_id for task in roots} == {"research-a", "research-b"}
    assert set(merge.depends_on) == {"research-a", "research-b"}
    assert merge.task_id == "review"


def test_baseline_workflow_compiles() -> None:
    compiled = compile_workflow(baseline_workflow(), providers=BASELINE_PROVIDERS, model="m")

    assert len(compiled.task_specs) == 3
    assert [agent.name for agent in compiled.agent_configs] == [
        "researcher-a",
        "researcher-b",
        "reviewer",
    ]
    assert compiled.task_specs[2].depends_on == ["Research A", "Research B"]


def test_baseline_agents_share_a_stable_prefix_then_diverge_by_role() -> None:
    workflow = baseline_workflow()

    for agent in workflow.agents:
        assert agent.system_prompt.startswith(STABLE_SYSTEM_PREFIX)
        assert "Role:" in agent.system_prompt

    roles = {agent.system_prompt.split("Role:", 1)[1] for agent in workflow.agents}
    assert len(roles) == 3


def test_stable_prefix_contains_no_volatile_values() -> None:
    lowered = STABLE_SYSTEM_PREFIX.lower()

    for forbidden in ("timestamp", "request id", "uuid", "cache hit", "metrics"):
        assert forbidden not in lowered
