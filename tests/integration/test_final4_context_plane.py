"""Final-4 Gate 1: deterministic verification of the corrected evaluation entry.

The engine, the DAG, the prompt seam, the YAML resolution and the router are real; only the provider
is a deterministic fake and the sandbox is absent. The assertions are made on the messages the
provider actually receives, which is the only thing that decides whether the Atlas Context Plane
participated in a run.

Nothing here calls DeepSeek or OpenSandbox. Live behaviour is a separate, budgeted step.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from anycode import OrchestratorConfig, TaskSpec
from anycode.plugins.registry import register_provider_factory
from anycode.providers.fake import FakeAdapter, FakeResponse
from anycode.types import AgentConfig, LLMChatOptions, LLMMessage, TeamConfig
from experiments.evaluation.final4 import runner_entry
from experiments.evaluation.final4.validation_tasks import TASKS as VALIDATION_TASKS
from experiments.evaluation.final4.validation_tasks import Task
from experiments.evaluation.runner import (
    CONTEXT_PLANE_TASK_SCOPED,
    CONTEXT_PLANE_UPSTREAM,
    EvaluationEnvironmentError,
    RunnerSettings,
    _context_evidence,
    _context_prompt_builder,
    _enforce_context_plane_config,
    run_sample,
)
from experiments.evaluation.stage_r0_routing import a0_steps, plan_route, routing_policy

from atlasos.config import load_config
from atlasos.integrations.anycode_engine import AtlasContextEngine
from atlasos.workers import WorkerCatalog
from atlasos.workers.routing import capabilities_from_catalog, extract_declared_paths

RESULTS_ROOT = Path(__file__).resolve().parents[2] / "experiments" / "results" / "final4"
SERIAL = next(task for task in VALIDATION_TASKS if task.task_id == "validation-serial")

RECORDED: dict[str, list[list[LLMMessage]]] = {}
SYSTEM_PROMPTS: dict[str, list[str]] = {}


# -- helpers ------------------------------------------------------------------------------------


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


def _received_text(messages: list[LLMMessage]) -> str:
    return "\n".join(
        str(getattr(block, "text", "")) for message in messages for block in message.content
    )


def _fake_workers(task_entry: Any) -> dict[str, Any]:
    """The only two attributes ``_context_prompt_builder`` reads from a created worker."""

    return {
        step.task_id: SimpleNamespace(
            worker_name=f"w-{step.task_id}",
            agent_config=SimpleNamespace(system_prompt=f"SYSTEM-{step.task_id}"),
        )
        for step in task_entry.workers
    }


def _task_for(task: Task) -> Any:
    return runner_entry.build_task(task, multi=True)


# -- configuration ------------------------------------------------------------------------------


def test_every_atlas_unit_declares_a_configuration_that_can_satisfy_it() -> None:
    expectations = {"X0": CONTEXT_PLANE_UPSTREAM, "R0": CONTEXT_PLANE_TASK_SCOPED}
    expectations["R1"] = CONTEXT_PLANE_TASK_SCOPED
    expectations["R2"] = CONTEXT_PLANE_TASK_SCOPED

    for unit, expectation in expectations.items():
        resolved = runner_entry.preflight(unit)
        assert resolved["expectation"] == expectation
        assert resolved["policy"] == expectation
        assert resolved["sandbox_provider"] == "opensandbox"

    context_config = load_config(runner_entry.CONTEXT_CONFIG)
    assert context_config.context.policy == "task_scoped"
    assert context_config.context.renderer == "stable_deterministic"
    assert context_config.context.transform.name == "identity"
    assert context_config.placement.policy == "bypass"

    baseline_config = load_config(runner_entry.BASELINE_CONFIG)
    assert baseline_config.context.policy == CONTEXT_PLANE_UPSTREAM
    assert baseline_config.context.renderer == CONTEXT_PLANE_UPSTREAM


def test_a_declared_context_plane_that_the_yaml_does_not_enable_is_refused() -> None:
    baseline = runner_entry.BASELINE_CONFIG
    context = runner_entry.CONTEXT_CONFIG

    with pytest.raises(EvaluationEnvironmentError, match="evaluation configuration error"):
        _enforce_context_plane_config(
            CONTEXT_PLANE_TASK_SCOPED,
            config=load_config(baseline),
            config_file=baseline,
            handoff_mode="legacy",
        )
    with pytest.raises(EvaluationEnvironmentError, match="handoff_mode"):
        _enforce_context_plane_config(
            CONTEXT_PLANE_TASK_SCOPED,
            config=load_config(context),
            config_file=context,
            handoff_mode="dual",
        )
    with pytest.raises(EvaluationEnvironmentError, match="evaluation configuration error"):
        _enforce_context_plane_config(
            CONTEXT_PLANE_UPSTREAM,
            config=load_config(context),
            config_file=context,
            handoff_mode="legacy",
        )


@pytest.mark.asyncio
async def test_run_sample_stops_before_any_io_when_the_claim_does_not_hold(tmp_path: Path) -> None:
    settings = RunnerSettings(
        runs_root=tmp_path / "runs",
        config_path=runner_entry.BASELINE_CONFIG,
        context_plane_expectation=CONTEXT_PLANE_TASK_SCOPED,
    )
    with pytest.raises(EvaluationEnvironmentError, match="evaluation configuration error"):
        await run_sample(_task_for(SERIAL), settings=settings, repetition_id=0)
    assert not (tmp_path / "runs").exists()


# -- public task information ---------------------------------------------------------------------


def test_single_worker_turn_carries_the_full_public_facts() -> None:
    task = runner_entry.build_task(SERIAL, multi=False)
    assert len(task.workers) == 1
    turn = task.workers[0].prompt
    assert task.prompt == turn
    for fact in (
        "PUBLIC REQUIREMENT",
        "PUBLIC INTERFACE CONTRACT",
        "DATA CONSTRAINTS",
        "PUBLIC ACCEPTANCE",
        "test_public.py",
        "report.clean_rows",
        "monkeypatch",
        "Git history",
    ):
        assert fact in turn, fact


def test_multi_workers_share_one_public_context_and_have_disjoint_write_sets() -> None:
    for task in VALIDATION_TASKS:
        entry = runner_entry.build_task(task, multi=True)
        shared = {step.shared_context for step in entry.workers}
        assert shared == {task.shared_context}
        assert all(step.shared_context for step in entry.workers)

        declared = [set(extract_declared_paths(step.prompt)[0]) for step in entry.workers]
        assert all(declared), f"{task.task_id} has a step with no declared write path"
        seen: set[str] = set()
        for paths in declared:
            assert not (seen & paths), f"{task.task_id}: overlapping write sets {paths}"
            seen |= paths
        assert seen == set(task.seed).intersection(seen)


def test_every_worker_receives_the_same_public_facts_as_the_single_arm() -> None:
    for task in VALIDATION_TASKS:
        single_turn = runner_entry.build_task(task, multi=False).workers[0].prompt
        assert task.shared_context in single_turn
        for step in runner_entry.build_task(task, multi=True).workers:
            turn = f"{step.shared_context}\n\n{step.prompt}"
            # The whole shared block, verbatim: requirement, interface, constraints, acceptance.
            assert task.shared_context in turn
            for fact in ("PUBLIC INTERFACE CONTRACT", "DATA CONSTRAINTS", "test_public.py"):
                assert fact in turn
            # The worker's own assignment is a strict addition, never a replacement.
            assert step.prompt
            assert step.prompt not in task.shared_context


# -- the real seam ------------------------------------------------------------------------------


async def _run_dag(task: Task) -> tuple[Any, Any, Any, Any]:
    entry = _task_for(task)
    workers = _fake_workers(entry)
    title_by_id = {step.task_id: f"{task.task_id}-{step.task_id}" for step in entry.workers}
    task_texts = {step.task_id: f"{step.shared_context}\n\n{step.prompt}" for step in entry.workers}
    config = load_config(runner_entry.CONTEXT_CONFIG)
    plane = _context_prompt_builder(
        config=config,
        task=entry,
        title_by_id=title_by_id,
        task_texts=task_texts,
        worker_by_task=workers,
    )
    assert plane is not None
    builder, store = plane

    engine = AtlasContextEngine(OrchestratorConfig(), prompt_builder=builder)
    agent_configs = []
    providers = {}
    for step in entry.workers:
        provider_name = f"final4-gate1-{task.task_id}-{step.task_id}"
        _install(provider_name, f"RESULT-OF-{step.task_id}")
        providers[step.task_id] = provider_name
        agent_configs.append(
            AgentConfig(
                name=workers[step.task_id].worker_name,
                model="fake",
                provider=provider_name,
                system_prompt=f"SYSTEM-{step.task_id}",
                tools=[],
            )
        )
    team = engine.create_team("final4-gate1", TeamConfig(name="final4-gate1", agents=agent_configs))
    specs = [
        TaskSpec(
            title=title_by_id[step.task_id],
            description=task_texts[step.task_id],
            assignee=workers[step.task_id].worker_name,
            depends_on=[title_by_id[item] for item in step.depends_on],
        )
        for step in entry.workers
    ]
    try:
        result = await engine.run_tasks(team, specs)
    finally:
        await engine.close()
    return entry, builder, store, (result, providers)


@pytest.mark.asyncio
async def test_dependency_output_reaches_the_provider_request_exactly_once() -> None:
    RECORDED.clear()
    SYSTEM_PROMPTS.clear()
    entry, builder, store, (result, providers) = await _run_dag(SERIAL)
    assert result.success is True

    producer_prompt = _received_text(RECORDED[providers["w1"]][0])
    consumer_prompt = _received_text(RECORDED[providers["w2"]][0])

    assert "RESULT-OF-w1" in consumer_prompt
    assert consumer_prompt.count("RESULT-OF-w1") == 1
    assert "RESULT-OF-w1" not in producer_prompt
    # The trusted worker system prompt is unchanged and is not inlined into the user turn.
    assert SYSTEM_PROMPTS[providers["w2"]][0] == "SYSTEM-w2"
    # The shared public facts survive the render for both workers.
    assert "PUBLIC INTERFACE CONTRACT" in consumer_prompt
    assert "PUBLIC INTERFACE CONTRACT" in producer_prompt

    renders = builder.renders()
    assert set(renders) == {"w1", "w2"}
    assert renders["w2"]["rendered_dependency_order"] == ["w1"]
    assert renders["w2"]["context_layout_hash"] != renders["w1"]["context_layout_hash"]

    evidence = _context_evidence(
        builder=builder, store=store, task=entry, task_texts={}, observations=[]
    )
    assert evidence["enabled"] is True
    assert evidence["builder"] == "AtlasTaskPromptBuilder"
    assert evidence["store_block_ids"] == ["task-output:w1"]
    assert evidence["builder_invocations"] == 2
    by_task = {item["task_id"]: item for item in evidence["tasks"]}
    assert by_task["w1"]["declared_dependencies"] == []
    assert by_task["w1"]["dependencies"] == []
    w2 = by_task["w2"]
    assert w2["declared_dependencies"] == ["w1"]
    assert w2["selected_block_ids"] == ["task-output:w1"]
    assert w2["selected_block_versions"] == [1]
    assert w2["transform_name"] == "identity"
    assert w2["dependencies"][0]["in_rendered_prompt"] is True
    assert w2["dependencies"][0]["injection_count"] == 1
    assert w2["dependencies"][0]["duplicate_injection"] is False
    assert w2["task_prompt_hash"]
    assert evidence["provider_requests"] == []
    assert (
        "DIFFERENT_FUNCTIONS"
        in evidence["correlation"]["task_prompt_hash_vs_provider_request_hash"]
    )


# -- router -------------------------------------------------------------------------------------


def test_direct_and_delegate_share_the_same_frozen_candidate_plan() -> None:
    capabilities = capabilities_from_catalog(WorkerCatalog())
    for task in VALIDATION_TASKS:
        entry = _task_for(task)
        routed = plan_route(entry, capabilities, routing_policy("r0.1.0"))
        assert routed.steps == entry.workers
        assert {step.task_id for step in routed.steps} == {
            str(worker["task_id"]) for worker in task.workers
        }
        if routed.is_direct:
            collapsed = a0_steps(entry)
            assert len(collapsed) == 1
            assert collapsed[0].prompt == entry.prompt
        assert routed.decision.reason_codes


def test_the_router_cannot_see_the_shared_public_context() -> None:
    """Routing reads the worker's own assignment, not the model-visible turn."""

    for task in VALIDATION_TASKS:
        for step in _task_for(task).workers:
            assert "PUBLIC INTERFACE CONTRACT" not in step.prompt
            assert step.shared_context


# -- digests ------------------------------------------------------------------------------------


def test_task_set_revision_two_records_what_it_supersedes() -> None:
    current = json.loads((RESULTS_ROOT / "validation_tasks_v2.json").read_text(encoding="utf-8"))
    previous = json.loads((RESULTS_ROOT / "validation_tasks.json").read_text(encoding="utf-8"))
    manifest = json.loads((RESULTS_ROOT / "pilot_manifest_v2.json").read_text(encoding="utf-8"))

    assert current["revision"] == 2
    assert manifest["revision"] == 2
    assert manifest["context_config_file"] == "experiments/configs/final4-context.yaml"
    assert set(manifest["supersedes"]) == {task.task_id for task in VALIDATION_TASKS}

    old = {record["task_id"]: record for record in previous["tasks"]}
    new = {record["task_id"]: record for record in current["tasks"]}
    for task_id, record in new.items():
        assert record["candidate_plan_digest"]
        assert record["shared_context_digest"]
        assert (
            manifest["supersedes"][task_id]["candidate_plan_digest"]
            == (old[task_id]["candidate_plan_digest"])
        )
        # The oracle, the seed and the solution are unchanged by the correction.
        assert record["oracle_digest"] == old[task_id]["oracle_digest"]
        assert record["solution_digest"] == old[task_id]["solution_digest"]
        assert record["initial_workspace_digest"] == old[task_id]["initial_workspace_digest"]
        assert record["candidate_plan_digest"] != old[task_id]["candidate_plan_digest"]
