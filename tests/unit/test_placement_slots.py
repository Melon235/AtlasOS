"""G1: immutable placement slots, frozen assignment plans and task execution binding.

Offline only. The real ``ScopedWorkerFactory`` and ``WorkerStore`` are exercised, so the assertions
are made against the objects the factory actually produces and persists, not against the new model's
own bookkeeping.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from anycode.identity.context import ExecutionContext

from atlasos.placement import (
    MAX_ACTIVE_TASKS,
    BindingError,
    FrozenAssignmentPlan,
    PlacementSlot,
    PlacementSlotError,
    PlanValidationError,
    bind_task,
    build_slot_pool,
    freeze_assignment,
    plan_from_json,
    plan_from_payload,
    slot_capabilities,
    verify_plan,
)
from atlasos.workers import ScopedWorkerFactory, WorkerAssignment, WorkerCatalog, WorkerStore
from atlasos.workflow import AtlasAgentSpec, AtlasTaskSpec, AtlasWorkflow

RUN_ID = "run-g1"


# -- fixtures -----------------------------------------------------------------------------------


def workflow(*task_specs: AtlasTaskSpec, name: str = "g1") -> AtlasWorkflow:
    return AtlasWorkflow(
        name=name,
        agents=(AtlasAgentSpec(name="agent", system_prompt="prompt"),),
        tasks=task_specs,
    )


def task(
    task_id: str,
    *,
    depends_on: tuple[str, ...] = (),
    capabilities: frozenset[str] = frozenset(),
) -> AtlasTaskSpec:
    return AtlasTaskSpec(
        task_id=task_id,
        title=task_id.upper(),
        description=f"do {task_id}",
        assignee="unplaced",
        depends_on=depends_on,
        required_capabilities=capabilities,
    )


def pool(*profile_ids: str) -> tuple[PlacementSlot, ...]:
    return build_slot_pool(
        run_id=RUN_ID, catalog=WorkerCatalog(), profile_ids=list(profile_ids or ("coding_worker",))
    )


def plan_for(
    wf: AtlasWorkflow,
    assignments: dict[str, str],
    *,
    slots: tuple[PlacementSlot, ...] | None = None,
    policy_name: str = "context_locality",
    policy_version: str = "g1",
    policy_config: dict[str, Any] | None = None,
    run_id: str = RUN_ID,
) -> FrozenAssignmentPlan:
    return freeze_assignment(
        run_id=run_id,
        workflow=wf,
        slots=slots if slots is not None else pool("coding_worker", "coding_worker"),
        assignments=assignments,
        catalog=WorkerCatalog(),
        policy_name=policy_name,
        policy_version=policy_version,
        policy_config=policy_config,
    )


def parent_context(*, run_id: str = RUN_ID) -> ExecutionContext:
    return ExecutionContext(
        principal="user:g1",
        tenant_scope="tenant-g1",
        classification="internal",
        attributes={"run_id": run_id, "safe_parent": "yes"},
    )


def make_factory(tmp_path: Path) -> ScopedWorkerFactory:
    return ScopedWorkerFactory(
        model="fake-model",
        provider="fake-provider",
        store=WorkerStore(tmp_path / "workers.json"),
    )


def assignment(profile_id: str = "coding_worker", *, text: str = "implement the module"):
    return WorkerAssignment(profile_id=profile_id, task=text)


# -- A. slot pool -------------------------------------------------------------------------------


def test_two_slots_of_the_same_profile_are_legal_and_distinct() -> None:
    slots = pool("coding_worker", "coding_worker")
    assert len(slots) == 2
    assert slots[0].slot_id != slots[1].slot_id
    assert {slot.profile_id for slot in slots} == {"coding_worker"}
    assert slots[0].slot_id == f"{RUN_ID}-slot1"
    assert slots[1].slot_id == f"{RUN_ID}-slot2"


def test_slot_ids_are_stable_for_the_same_input() -> None:
    first = pool("coding_worker", "coding_worker")
    second = pool("coding_worker", "coding_worker")
    assert [slot.slot_id for slot in first] == [slot.slot_id for slot in second]


def test_unknown_profile_is_rejected() -> None:
    with pytest.raises(PlacementSlotError, match="unknown worker profile"):
        pool("no_such_worker")


def test_empty_pool_is_rejected() -> None:
    with pytest.raises(PlacementSlotError, match="at least one slot"):
        build_slot_pool(run_id=RUN_ID, catalog=WorkerCatalog(), profile_ids=[])


@pytest.mark.parametrize("capacity", [0, -1, 2, True])
def test_illegal_capacity_is_rejected(capacity: object) -> None:
    with pytest.raises(PlacementSlotError):
        PlacementSlot(slot_id="s1", profile_id="coding_worker", max_active_tasks=capacity)  # type: ignore[arg-type]


def test_duplicate_slot_id_is_rejected() -> None:
    slot = PlacementSlot(slot_id="dup", profile_id="coding_worker")
    wf = workflow(task("t1"))
    with pytest.raises(PlanValidationError, match="duplicate slot_id"):
        freeze_assignment(
            run_id=RUN_ID,
            workflow=wf,
            slots=(slot, slot),
            assignments={"t1": "dup"},
            catalog=WorkerCatalog(),
            policy_name="context_locality",
            policy_version="g1",
        )


def test_capabilities_come_from_the_trusted_profile_scope() -> None:
    catalog = WorkerCatalog()
    for profile_id, expected in (
        ("coding_worker", frozenset({"coding"})),
        ("research_worker", frozenset({"research", "diagnostics"})),
        ("data_worker", frozenset({"data"})),
    ):
        profile = catalog.resolve(profile_id)
        assert slot_capabilities(profile) == frozenset(profile.operating_scope) == expected


# -- B. immutable plan --------------------------------------------------------------------------


def test_same_input_yields_the_same_digest() -> None:
    wf = workflow(task("t1"), task("t2", depends_on=("t1",)))
    a = plan_for(wf, {"t1": f"{RUN_ID}-slot1", "t2": f"{RUN_ID}-slot2"})
    b = plan_for(wf, {"t1": f"{RUN_ID}-slot1", "t2": f"{RUN_ID}-slot2"})
    assert a.plan_digest == b.plan_digest
    assert a.to_json() == b.to_json()


def test_insertion_order_does_not_change_the_digest() -> None:
    wf = workflow(task("t1"), task("t2", depends_on=("t1",)))
    forward = plan_for(wf, {"t1": f"{RUN_ID}-slot1", "t2": f"{RUN_ID}-slot2"})
    reverse = plan_for(wf, {"t2": f"{RUN_ID}-slot2", "t1": f"{RUN_ID}-slot1"})
    assert forward.plan_digest == reverse.plan_digest


def test_mutating_the_input_containers_cannot_change_a_frozen_plan() -> None:
    wf = workflow(task("t1"), task("t2"))
    assignments = {"t1": f"{RUN_ID}-slot1", "t2": f"{RUN_ID}-slot2"}
    policy_config = {"locality": 1.0}
    slots = list(pool("coding_worker", "coding_worker"))
    plan = freeze_assignment(
        run_id=RUN_ID,
        workflow=wf,
        slots=slots,
        assignments=assignments,
        catalog=WorkerCatalog(),
        policy_name="context_locality",
        policy_version="g1",
        policy_config=policy_config,
    )
    before = plan.plan_digest

    assignments["t1"] = f"{RUN_ID}-slot2"
    assignments["t3"] = f"{RUN_ID}-slot1"
    policy_config["locality"] = 99.0
    slots.append(PlacementSlot(slot_id="extra", profile_id="coding_worker"))

    assert plan.plan_digest == before
    assert plan.slot_for("t1").slot_id == f"{RUN_ID}-slot1"
    assert plan.slot_for("t2").slot_id == f"{RUN_ID}-slot2"
    assert len(plan.slots) == 2
    verify_plan(plan)


def test_plan_internals_cannot_be_mutated() -> None:
    plan = plan_for(workflow(task("t1")), {"t1": f"{RUN_ID}-slot1"})
    with pytest.raises(AttributeError):
        plan.assignments = ()  # type: ignore[misc]
    with pytest.raises(AttributeError):
        plan.plan_digest = "sha256:tampered"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        plan.assignments[0].slot_id = f"{RUN_ID}-slot2"  # type: ignore[misc]


def test_dag_slot_or_assignment_change_breaks_the_digest_or_the_validation() -> None:
    wf = workflow(task("t1"), task("t2", depends_on=("t1",)))
    base = plan_for(wf, {"t1": f"{RUN_ID}-slot1", "t2": f"{RUN_ID}-slot2"})

    # A different assignment: different digest.
    moved = plan_for(wf, {"t1": f"{RUN_ID}-slot2", "t2": f"{RUN_ID}-slot1"})
    assert moved.plan_digest != base.plan_digest

    # A different DAG: different digest.
    longer = workflow(task("t1"), task("t2", depends_on=("t1",)), task("t3", depends_on=("t2",)))
    extended = plan_for(
        longer,
        {"t1": f"{RUN_ID}-slot1", "t2": f"{RUN_ID}-slot2", "t3": f"{RUN_ID}-slot1"},
    )
    assert extended.plan_digest != base.plan_digest

    # A different policy configuration: different digest.
    other_policy = plan_for(
        wf,
        {"t1": f"{RUN_ID}-slot1", "t2": f"{RUN_ID}-slot2"},
        policy_config={"locality": 5.0},
    )
    assert other_policy.plan_digest != base.plan_digest

    # A different slot profile: different digest.
    slots = pool("coding_worker", "research_worker")
    mixed = plan_for(wf, {"t1": f"{RUN_ID}-slot1", "t2": f"{RUN_ID}-slot2"}, slots=slots)
    assert mixed.plan_digest != base.plan_digest

    # Tampering with a stored field is detected.
    with pytest.raises(PlanValidationError, match="digest mismatch"):
        verify_plan(replace(base, run_id="another-run"))


def test_serialisation_round_trip_preserves_identity_and_digest() -> None:
    plan = plan_for(workflow(task("t1")), {"t1": f"{RUN_ID}-slot1"})
    restored = plan_from_json(plan.to_json())
    assert restored.plan_digest == plan.plan_digest
    assert restored.to_json() == plan.to_json()
    assert restored.slot_for("t1").slot_id == plan.slot_for("t1").slot_id
    assert restored.slot_profiles == plan.slot_profiles


def test_tampered_payload_is_rejected() -> None:
    plan = plan_for(
        workflow(task("t1"), task("t2")),
        {"t1": f"{RUN_ID}-slot1", "t2": f"{RUN_ID}-slot2"},
    )

    # Content edited, stored digest left alone.
    envelope = json.loads(plan.to_json())
    envelope["plan"]["assignments"][1]["slot_id"] = f"{RUN_ID}-slot1"
    with pytest.raises(PlanValidationError, match="digest mismatch"):
        plan_from_payload(envelope)

    # Digest edited, content left alone.
    envelope = json.loads(plan.to_json())
    envelope["plan_digest"] = "sha256:" + "0" * 64
    with pytest.raises(PlanValidationError, match="digest mismatch"):
        plan_from_payload(envelope)

    # Envelope missing its digest.
    with pytest.raises(PlanValidationError, match="must carry"):
        plan_from_payload({"plan": json.loads(plan.to_json())["plan"]})

    # Content structurally broken.
    broken = json.loads(plan.to_json())
    broken["plan"]["schema_version"] = 99
    with pytest.raises(PlanValidationError):
        plan_from_payload(broken)


def test_plan_rejects_unknown_schema_version() -> None:
    plan = plan_for(workflow(task("t1")), {"t1": f"{RUN_ID}-slot1"})
    with pytest.raises(PlanValidationError, match="unsupported plan schema"):
        verify_plan(replace(plan, schema_version=99))


# -- C. plan legality ---------------------------------------------------------------------------


def test_missing_assignment_is_rejected() -> None:
    wf = workflow(task("t1"), task("t2"))
    with pytest.raises(PlanValidationError, match="without a slot"):
        plan_for(wf, {"t1": f"{RUN_ID}-slot1"})


def test_unknown_task_or_slot_is_rejected() -> None:
    wf = workflow(task("t1"))
    with pytest.raises(PlanValidationError, match="unknown task"):
        plan_for(wf, {"t1": f"{RUN_ID}-slot1", "ghost": f"{RUN_ID}-slot1"})
    with pytest.raises(PlanValidationError, match="unknown slot"):
        plan_for(wf, {"t1": f"{RUN_ID}-slot9"})


def test_duplicate_task_ids_are_rejected() -> None:
    wf = workflow(task("t1"), task("t1"))
    with pytest.raises(PlanValidationError, match="duplicate task ids"):
        plan_for(wf, {"t1": f"{RUN_ID}-slot1"})


def test_incompatible_capability_is_rejected() -> None:
    wf = workflow(task("t1", capabilities=frozenset({"data"})))
    slots = pool("coding_worker")
    with pytest.raises(PlanValidationError, match="missing capabilities"):
        plan_for(wf, {"t1": slots[0].slot_id}, slots=slots)


def test_unknown_dependency_and_cycle_are_rejected() -> None:
    with pytest.raises(PlanValidationError, match="unknown task"):
        plan_for(workflow(task("t1", depends_on=("ghost",))), {"t1": f"{RUN_ID}-slot1"})

    cyc = workflow(task("t1", depends_on=("t2",)), task("t2", depends_on=("t1",)))
    with pytest.raises(PlanValidationError, match="circular"):
        plan_for(cyc, {"t1": f"{RUN_ID}-slot1", "t2": f"{RUN_ID}-slot2"})


def test_same_wave_same_slot_is_rejected_but_different_waves_are_allowed() -> None:
    same_wave = workflow(task("t1"), task("t2"))
    with pytest.raises(PlanValidationError, match="concurrently in wave"):
        plan_for(same_wave, {"t1": f"{RUN_ID}-slot1", "t2": f"{RUN_ID}-slot1"})

    chained = workflow(task("t1"), task("t2", depends_on=("t1",)))
    plan = plan_for(chained, {"t1": f"{RUN_ID}-slot1", "t2": f"{RUN_ID}-slot1"})
    assert plan.wave_for("t1") != plan.wave_for("t2")


# -- D. real worker construction ----------------------------------------------------------------


def test_two_tasks_on_one_slot_get_independent_workers(tmp_path: Path) -> None:
    wf = workflow(task("a"), task("b", depends_on=("a",)))
    slot_id = f"{RUN_ID}-slot1"
    plan = plan_for(wf, {"a": slot_id, "b": slot_id})
    factory = make_factory(tmp_path)
    context = parent_context()

    binding_a, worker_a = bind_task(
        plan=plan,
        logical_task_id="a",
        runtime_task_id=f"{RUN_ID}-a",
        factory=factory,
        parent_context=context,
        parent_agent="atlas-evaluation",
        assignment=assignment(text="implement module a"),
    )
    binding_b, worker_b = bind_task(
        plan=plan,
        logical_task_id="b",
        runtime_task_id=f"{RUN_ID}-b",
        factory=factory,
        parent_context=context,
        parent_agent="atlas-evaluation",
        assignment=assignment(text="implement module b"),
    )

    # Distinct execution identities, one shared slot.
    assert binding_a.placement_slot_id == binding_b.placement_slot_id == slot_id
    assert worker_a.record.worker_id != worker_b.record.worker_id
    assert worker_a.agent_config.name != worker_b.agent_config.name
    assert worker_a.record.task_id == f"{RUN_ID}-a"
    assert worker_b.record.task_id == f"{RUN_ID}-b"

    # The trusted profile really came from the slot.
    assert worker_a.record.profile_id == worker_b.record.profile_id == "coding_worker"
    assert worker_a.record.profile_id == plan.slot_for("a").profile_id
    assert worker_a.agent_config.name == f"coding_worker:{RUN_ID}-a"

    # Principal is inherited, untouched; worker identity only lives in attributes.
    assert worker_a.execution_context.principal == context.principal
    assert worker_b.execution_context.principal == context.principal
    assert worker_a.execution_context.attributes["worker_id"] == f"{RUN_ID}-a"
    assert worker_a.execution_context.attributes["task_id"] == f"{RUN_ID}-a"
    atlas_worker = worker_a.execution_context.attributes["atlas_worker"]
    assert isinstance(atlas_worker, dict)
    assert atlas_worker["task_id"] == f"{RUN_ID}-a"
    assert atlas_worker["worker_id"] == f"{RUN_ID}-a"

    # Both records really went into the existing store and can be read back independently.
    store = WorkerStore(tmp_path / "workers.json")
    records = {record.worker_id: record for record in store.list()}
    assert set(records) == {f"{RUN_ID}-a", f"{RUN_ID}-b"}
    assert records[f"{RUN_ID}-a"].task_id == f"{RUN_ID}-a"
    assert records[f"{RUN_ID}-b"].task_id == f"{RUN_ID}-b"

    # The existing resume() still restores each task record exactly.
    resumed_a = factory.resume(f"{RUN_ID}-a")
    resumed_b = factory.resume(f"{RUN_ID}-b")
    assert resumed_a.record.task_id == f"{RUN_ID}-a"
    assert resumed_b.record.task_id == f"{RUN_ID}-b"
    assert resumed_a.agent_config.name != resumed_b.agent_config.name


def test_profile_mismatch_is_rejected_before_the_factory_persists(tmp_path: Path) -> None:
    plan = plan_for(workflow(task("a")), {"a": f"{RUN_ID}-slot1"})
    factory = make_factory(tmp_path)
    with pytest.raises(BindingError, match="does not match slot"):
        bind_task(
            plan=plan,
            logical_task_id="a",
            runtime_task_id=f"{RUN_ID}-a",
            factory=factory,
            parent_context=parent_context(),
            parent_agent="atlas-evaluation",
            assignment=assignment("data_worker"),
        )
    assert WorkerStore(tmp_path / "workers.json").list() == ()


def test_cross_run_plan_is_rejected(tmp_path: Path) -> None:
    plan = plan_for(workflow(task("a")), {"a": f"{RUN_ID}-slot1"})
    with pytest.raises(BindingError, match="run_id"):
        bind_task(
            plan=plan,
            logical_task_id="a",
            runtime_task_id=f"{RUN_ID}-a",
            factory=make_factory(tmp_path),
            parent_context=parent_context(run_id="another-run"),
            parent_agent="atlas-evaluation",
            assignment=assignment(),
        )


def test_conflicting_rebinding_is_rejected_and_keeps_the_first_record(tmp_path: Path) -> None:
    wf = workflow(task("a"))
    slots = pool("coding_worker", "coding_worker")
    plan = plan_for(wf, {"a": slots[0].slot_id}, slots=slots)
    factory = make_factory(tmp_path)
    binding, _ = bind_task(
        plan=plan,
        logical_task_id="a",
        runtime_task_id=f"{RUN_ID}-a",
        factory=factory,
        parent_context=parent_context(),
        parent_agent="atlas-evaluation",
        assignment=assignment(),
    )

    # Same binding again is fine.
    same, _ = bind_task(
        plan=plan,
        logical_task_id="a",
        runtime_task_id=f"{RUN_ID}-a",
        factory=factory,
        parent_context=parent_context(),
        parent_agent="atlas-evaluation",
        assignment=assignment(),
        prior=binding,
    )
    assert same == binding

    # A different slot for the same task is refused.
    moved = plan_for(wf, {"a": slots[1].slot_id}, slots=slots)
    with pytest.raises(BindingError, match="already bound"):
        bind_task(
            plan=moved,
            logical_task_id="a",
            runtime_task_id=f"{RUN_ID}-a",
            factory=factory,
            parent_context=parent_context(),
            parent_agent="atlas-evaluation",
            assignment=assignment(),
            prior=binding,
        )

    record = WorkerStore(tmp_path / "workers.json").load(f"{RUN_ID}-a")
    assert record.assignment.task == "implement the module"


def test_unknown_task_cannot_be_bound(tmp_path: Path) -> None:
    plan = plan_for(workflow(task("a")), {"a": f"{RUN_ID}-slot1"})
    with pytest.raises(PlanValidationError, match="has no slot"):
        bind_task(
            plan=plan,
            logical_task_id="ghost",
            runtime_task_id=f"{RUN_ID}-ghost",
            factory=make_factory(tmp_path),
            parent_context=parent_context(),
            parent_agent="atlas-evaluation",
            assignment=assignment(),
        )


def test_max_active_tasks_is_fixed_for_g1() -> None:
    assert MAX_ACTIVE_TASKS == 1
    assert PlacementSlot(slot_id="s", profile_id="coding_worker").max_active_tasks == 1
