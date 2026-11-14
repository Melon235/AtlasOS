"""Immutable placement slots, frozen assignment plans, and task execution binding.

This is the Atlas-side domain model for *where* a task runs. It owns three things and nothing else:

* a run-scoped :class:`PlacementSlot` pool, built from the trusted worker catalog;
* a :class:`FrozenAssignmentPlan`, which freezes ``task -> slot`` before execution and carries a
  canonical digest so a later reader can detect any change;
* :func:`bind_task`, which turns one frozen assignment into a real ``WorkerInstance`` through the
  existing :class:`~atlasos.workers.factory.ScopedWorkerFactory`.

It deliberately does **not** schedule, plan, or execute anything. Order stays with AnyCode,
planner output stays with :mod:`atlasos.placement.planner`, and admission is not implemented here.

Identity rules this module must not weaken:

``task_id``
    The task's identity inside the workflow. Never replaced by a slot or a worker.
``placement_slot_id``
    A run-scoped, host-owned logical execution resource. One slot may serve several tasks.
``worker_id``
    The task-scoped worker execution instance, unchanged in meaning. Two tasks on one slot get
    different ``worker_id`` values, therefore different ``AgentConfig.name`` values.
``ExecutionContext.principal``
    Inherited from the parent context by the factory; this module never rewrites it.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from atlasos.placement.planner import dependency_levels
from atlasos.trajectory.models import canonical_digest, canonical_json
from atlasos.workers.catalog import WorkerCatalog
from atlasos.workers.models import WorkerAssignment, WorkerConfigurationError, WorkerInstance
from atlasos.workflow import AtlasWorkflow

if TYPE_CHECKING:
    from anycode.identity.context import ExecutionContext

    from atlasos.workers.factory import ScopedWorkerFactory

#: Schema of the serialised plan. A reader that does not know this number must refuse the plan.
PLAN_SCHEMA_VERSION = 1

#: G1 supports exactly one active task per slot. Higher values would need the admission layer
#: that G3 owns, so the model rejects them instead of accepting an unenforced claim.
MAX_ACTIVE_TASKS = 1


class PlacementSlotError(ValueError):
    """Raised when a slot pool cannot be built from trusted configuration."""


class PlanValidationError(ValueError):
    """Raised when an assignment cannot be frozen, or a frozen plan does not verify."""


class BindingError(ValueError):
    """Raised when a task execution binding would violate the frozen plan or the factory trust."""


@dataclass(frozen=True, slots=True)
class PlacementSlot:
    """One run-scoped logical execution resource. Host-owned; never derived from task text.

    ``capabilities`` is a coarse *eligibility* filter for the planner. It is not an authorization
    grant: tool permission stays with the action policy and the gate, exactly as before.
    """

    slot_id: str
    profile_id: str
    capabilities: frozenset[str] = frozenset()
    max_active_tasks: int = MAX_ACTIVE_TASKS

    def __post_init__(self) -> None:
        if not isinstance(self.slot_id, str) or not self.slot_id.strip():
            raise PlacementSlotError("slot_id must be a non-empty string")
        if not isinstance(self.profile_id, str) or not self.profile_id.strip():
            raise PlacementSlotError("profile_id must be a non-empty string")
        if not isinstance(self.capabilities, frozenset) or any(
            not isinstance(item, str) or not item.strip() for item in self.capabilities
        ):
            raise PlacementSlotError("capabilities must be a frozenset of non-empty strings")
        if isinstance(self.max_active_tasks, bool) or not isinstance(self.max_active_tasks, int):
            raise PlacementSlotError("max_active_tasks must be an int")
        if self.max_active_tasks != MAX_ACTIVE_TASKS:
            raise PlacementSlotError(
                f"max_active_tasks={self.max_active_tasks} is not supported in G1; "
                f"only {MAX_ACTIVE_TASKS} is enforced by the runtime"
            )


@dataclass(frozen=True, slots=True)
class SlotProfile:
    """The trusted profile definition that a slot's workers are built from.

    ``definition_digest`` covers the whole definition, not just ``profile_id``, so changing a role,
    scope, turn limit or tool preference changes the plan digest.
    """

    slot_id: str
    profile_id: str
    role: str
    operating_scope: tuple[str, ...]
    base_tools: tuple[str, ...]
    max_turns: int
    can_delegate: bool
    definition_digest: str


@dataclass(frozen=True, slots=True)
class AssignmentEntry:
    """One ``logical task -> slot`` decision."""

    task_id: str
    slot_id: str


@dataclass(frozen=True, slots=True)
class FrozenAssignmentPlan:
    """An immutable, digest-verified ``task -> slot`` plan for one run.

    Every collection is a tuple or a frozenset, so neither the caller's input containers nor the
    plan's own fields can be mutated after the fact.
    """

    schema_version: int
    run_id: str
    workflow_digest: str
    slots: tuple[PlacementSlot, ...]
    slot_profiles: tuple[SlotProfile, ...]
    assignments: tuple[AssignmentEntry, ...]
    wave_of_task: tuple[tuple[str, int], ...]
    policy_name: str
    policy_version: str
    policy_config_digest: str
    plan_digest: str

    # -- lookup -------------------------------------------------------------------------------

    def slot_for(self, task_id: str) -> PlacementSlot:
        for entry in self.assignments:
            if entry.task_id == task_id:
                for slot in self.slots:
                    if slot.slot_id == entry.slot_id:
                        return slot
                raise PlanValidationError(f"task {task_id!r} points at an unknown slot")
        raise PlanValidationError(f"task {task_id!r} has no slot in this plan")

    def slot_ids_for(self, task_id: str) -> tuple[str, ...]:
        return tuple(entry.slot_id for entry in self.assignments if entry.task_id == task_id)

    def profile_for(self, slot_id: str) -> SlotProfile:
        for profile in self.slot_profiles:
            if profile.slot_id == slot_id:
                return profile
        raise PlanValidationError(f"slot {slot_id!r} has no profile fingerprint")

    def wave_for(self, task_id: str) -> int:
        for item_id, wave in self.wave_of_task:
            if item_id == task_id:
                return wave
        raise PlanValidationError(f"task {task_id!r} is not in this plan")

    def assignment_map(self) -> dict[str, str]:
        return {entry.task_id: entry.slot_id for entry in self.assignments}

    # -- serialisation ------------------------------------------------------------------------

    def payload(self) -> dict[str, Any]:
        """Plan content, excluding the digest (a digest cannot cover itself)."""

        return _payload(
            schema_version=self.schema_version,
            run_id=self.run_id,
            workflow_digest=self.workflow_digest,
            slots=self.slots,
            slot_profiles=self.slot_profiles,
            assignments=self.assignments,
            wave_of_task=self.wave_of_task,
            policy_name=self.policy_name,
            policy_version=self.policy_version,
            policy_config_digest=self.policy_config_digest,
        )

    def envelope(self) -> dict[str, Any]:
        """Content plus its digest. This is what gets persisted or handed to another process."""

        return {"plan": self.payload(), "plan_digest": self.plan_digest}

    def to_json(self) -> str:
        return canonical_json(self.envelope())


@dataclass(frozen=True, slots=True)
class TaskExecutionBinding:
    """The auditable result of turning one frozen assignment into a real worker.

    ``logical_task_id`` is the workflow's ``task_id``; ``runtime_task_id`` is the id the executor
    actually uses (AnyCode's task id, and here also the worker id). They are not the same string and
    must not be conflated.
    """

    run_id: str
    logical_task_id: str
    runtime_task_id: str
    placement_slot_id: str
    worker_id: str
    agent_name: str
    profile_id: str
    plan_digest: str

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": PLAN_SCHEMA_VERSION,
            "run_id": self.run_id,
            "logical_task_id": self.logical_task_id,
            "runtime_task_id": self.runtime_task_id,
            "placement_slot_id": self.placement_slot_id,
            "worker_id": self.worker_id,
            "agent_name": self.agent_name,
            "profile_id": self.profile_id,
            "plan_digest": self.plan_digest,
        }


# -- slot pool ----------------------------------------------------------------------------------


def slot_capabilities(profile: Any) -> frozenset[str]:
    """Host mapping from a trusted profile to planner visibility.

    The rule is exactly: ``capabilities = profile.operating_scope``. ``operating_scope`` is the only
    coarse, validated capability vocabulary a ``WorkerProfile`` carries today (there is no generic
    ``capabilities`` field), it is host-owned and immutable, and it is already the vocabulary the
    planner compares against ``required_capabilities``.

    What this mapping is *not*: ``base_tools`` is not consulted, because the runtime replaces an
    agent's tool list at build time, and task text is never consulted, because it is model-supplied.
    """

    return frozenset(profile.operating_scope)


def build_slot_pool(
    *,
    run_id: str,
    catalog: WorkerCatalog,
    profile_ids: Sequence[str],
) -> tuple[PlacementSlot, ...]:
    """Build the run's slot pool from trusted profiles. Duplicate profiles are allowed, on purpose.

    ``profile_ids=("coding_worker", "coding_worker")`` produces two distinct slots holding the same
    profile: two resources of one kind is a legitimate pool, not a duplicate entry.
    """

    if not isinstance(run_id, str) or not run_id.strip():
        raise PlacementSlotError("run_id must be a non-empty string")
    if not profile_ids:
        raise PlacementSlotError("a slot pool must contain at least one slot")

    slots: list[PlacementSlot] = []
    seen: set[str] = set()
    for index, profile_id in enumerate(profile_ids, start=1):
        if not isinstance(profile_id, str) or not profile_id.strip():
            raise PlacementSlotError("profile_id entries must be non-empty strings")
        try:
            profile = catalog.resolve(profile_id)
        except WorkerConfigurationError as error:
            raise PlacementSlotError(str(error)) from error
        slot = PlacementSlot(
            slot_id=f"{run_id}-slot{index}",
            profile_id=profile.profile_id,
            capabilities=slot_capabilities(profile),
            max_active_tasks=MAX_ACTIVE_TASKS,
        )
        if slot.slot_id in seen:
            raise PlacementSlotError(f"duplicate slot_id {slot.slot_id!r}")
        seen.add(slot.slot_id)
        slots.append(slot)
    return tuple(slots)


def _slot_profile(slot: PlacementSlot, catalog: WorkerCatalog) -> SlotProfile:
    profile = catalog.resolve(slot.profile_id)
    definition = {
        "profile_id": profile.profile_id,
        "role": profile.role,
        "instructions": profile.instructions,
        "base_tools": sorted(profile.base_tools),
        "operating_scope": list(profile.operating_scope),
        "max_turns": profile.max_turns,
        "can_delegate": profile.can_delegate,
    }
    return SlotProfile(
        slot_id=slot.slot_id,
        profile_id=profile.profile_id,
        role=profile.role,
        operating_scope=tuple(profile.operating_scope),
        base_tools=tuple(sorted(profile.base_tools)),
        max_turns=profile.max_turns,
        can_delegate=profile.can_delegate,
        definition_digest=canonical_digest(definition),
    )


# -- freezing -----------------------------------------------------------------------------------


def workflow_digest(workflow: AtlasWorkflow) -> str:
    """Digest of the parts of a workflow that the assignment must agree with.

    Covers task identity, dependencies, required capabilities and required context keys. It does not
    cover agent roles (a plan assigns slots, not agents) and does not cover prompt text.
    """

    tasks = [
        {
            "task_id": task.task_id,
            "depends_on": sorted(task.depends_on),
            "required_capabilities": sorted(task.required_capabilities),
            "required_context": sorted(
                {
                    (requirement.logical_key, requirement.source_task_id)
                    for requirement in task.required_context
                }
            ),
        }
        for task in sorted(workflow.tasks, key=lambda item: item.task_id)
    ]
    return canonical_digest({"name": workflow.name, "tasks": tasks})


def freeze_assignment(
    *,
    run_id: str,
    workflow: AtlasWorkflow,
    slots: Sequence[PlacementSlot],
    assignments: Mapping[str, str],
    catalog: WorkerCatalog,
    policy_name: str,
    policy_version: str,
    policy_config: Mapping[str, Any] | None = None,
) -> FrozenAssignmentPlan:
    """Validate a ``task -> slot`` mapping and freeze it.

    The planner is *not* called here: G1 freezes a mapping that something else produced. Every
    legality rule is checked before a digest exists, so an invalid plan cannot be handed to a
    binding by accident.
    """

    if not isinstance(run_id, str) or not run_id.strip():
        raise PlanValidationError("run_id must be a non-empty string")
    if not slots:
        raise PlanValidationError("a plan needs at least one slot")
    if not isinstance(assignments, Mapping):
        raise PlanValidationError("assignments must be a mapping of task_id -> slot_id")
    if not policy_name or not policy_version:
        raise PlanValidationError("policy_name and policy_version are required")

    slot_by_id: dict[str, PlacementSlot] = {}
    for slot in slots:
        if slot.slot_id in slot_by_id:
            raise PlanValidationError(f"duplicate slot_id {slot.slot_id!r}")
        slot_by_id[slot.slot_id] = slot

    tasks = {task.task_id: task for task in workflow.tasks}
    if len(tasks) != len(workflow.tasks):
        raise PlanValidationError("workflow contains duplicate task ids")
    if not tasks:
        raise PlanValidationError("workflow contains no tasks")

    unknown_tasks = sorted(set(assignments) - set(tasks))
    if unknown_tasks:
        raise PlanValidationError(f"assignment names unknown task(s): {unknown_tasks}")
    missing = sorted(set(tasks) - set(assignments))
    if missing:
        raise PlanValidationError(f"task(s) without a slot: {missing}")

    # Dependencies are checked before the level computation, because that helper assumes a closed
    # graph and would raise a bare KeyError on a dangling dependency.
    for task in workflow.tasks:
        for dependency in task.depends_on:
            if dependency not in tasks:
                raise PlanValidationError(
                    f"task {task.task_id!r} depends on unknown task {dependency!r}"
                )
    try:
        levels = dependency_levels(workflow)
    except (KeyError, ValueError) as error:
        raise PlanValidationError(f"workflow dependency graph is invalid: {error}") from error

    entries: list[AssignmentEntry] = []
    for task_id in sorted(tasks):
        slot_id = assignments[task_id]
        slot = slot_by_id.get(slot_id)
        if slot is None:
            raise PlanValidationError(f"task {task_id!r} assigned to unknown slot {slot_id!r}")
        entries.append(AssignmentEntry(task_id=task_id, slot_id=slot.slot_id))

    for task in workflow.tasks:
        slot = slot_by_id[assignments[task.task_id]]
        missing_capabilities = sorted(task.required_capabilities - slot.capabilities)
        if missing_capabilities:
            raise PlanValidationError(
                f"slot {slot.slot_id!r} cannot satisfy task {task.task_id!r}: "
                f"missing capabilities {missing_capabilities}"
            )

    by_wave: dict[int, dict[str, list[str]]] = {}
    for task_id, level in levels.items():
        slot_id = assignments[task_id]
        by_wave.setdefault(level, {}).setdefault(slot_id, []).append(task_id)
    for level, per_slot in sorted(by_wave.items()):
        for slot_id, task_ids in sorted(per_slot.items()):
            slot = slot_by_id[slot_id]
            if len(task_ids) > slot.max_active_tasks:
                raise PlanValidationError(
                    f"slot {slot_id!r} would run {sorted(task_ids)} concurrently in wave {level} "
                    f"but capacity is {slot.max_active_tasks}"
                )

    profiles: list[SlotProfile] = []
    for slot_id in sorted(slot_by_id):
        try:
            profiles.append(_slot_profile(slot_by_id[slot_id], catalog))
        except WorkerConfigurationError as error:
            raise PlanValidationError(str(error)) from error

    payload = _payload(
        schema_version=PLAN_SCHEMA_VERSION,
        run_id=run_id,
        workflow_digest=workflow_digest(workflow),
        slots=[slot_by_id[slot_id] for slot_id in sorted(slot_by_id)],
        slot_profiles=profiles,
        assignments=entries,
        wave_of_task=tuple(sorted(levels.items())),
        policy_name=policy_name,
        policy_version=policy_version,
        policy_config_digest=canonical_digest(dict(policy_config or {})),
    )
    return FrozenAssignmentPlan(
        schema_version=PLAN_SCHEMA_VERSION,
        run_id=run_id,
        workflow_digest=str(payload["workflow_digest"]),
        slots=tuple(slot_by_id[slot_id] for slot_id in sorted(slot_by_id)),
        slot_profiles=tuple(profiles),
        assignments=tuple(entries),
        wave_of_task=tuple(sorted(levels.items())),
        policy_name=policy_name,
        policy_version=policy_version,
        policy_config_digest=canonical_digest(dict(policy_config or {})),
        plan_digest=canonical_digest(payload),
    )


def _payload(
    *,
    schema_version: int,
    run_id: str,
    workflow_digest: str,
    slots: Iterable[PlacementSlot],
    slot_profiles: Iterable[SlotProfile],
    assignments: Iterable[AssignmentEntry],
    wave_of_task: Iterable[tuple[str, int]],
    policy_name: str,
    policy_version: str,
    policy_config_digest: str,
) -> dict[str, Any]:
    return {
        "schema_version": schema_version,
        "run_id": run_id,
        "workflow_digest": workflow_digest,
        "slots": [
            {
                "slot_id": slot.slot_id,
                "profile_id": slot.profile_id,
                "capabilities": sorted(slot.capabilities),
                "max_active_tasks": slot.max_active_tasks,
            }
            for slot in sorted(slots, key=lambda item: item.slot_id)
        ],
        "slot_profiles": [
            {
                "slot_id": profile.slot_id,
                "profile_id": profile.profile_id,
                "role": profile.role,
                "operating_scope": list(profile.operating_scope),
                "base_tools": list(profile.base_tools),
                "max_turns": profile.max_turns,
                "can_delegate": profile.can_delegate,
                "definition_digest": profile.definition_digest,
            }
            for profile in sorted(slot_profiles, key=lambda item: item.slot_id)
        ],
        "assignments": [
            {"task_id": entry.task_id, "slot_id": entry.slot_id}
            for entry in sorted(assignments, key=lambda item: item.task_id)
        ],
        "waves": [{"task_id": task_id, "wave": wave} for task_id, wave in sorted(wave_of_task)],
        "policy": {
            "name": policy_name,
            "version": policy_version,
            "config_digest": policy_config_digest,
        },
    }


# -- verification -------------------------------------------------------------------------------


def verify_plan(plan: FrozenAssignmentPlan) -> None:
    """Recompute the digest from the plan's own fields and refuse any mismatch."""

    if plan.schema_version != PLAN_SCHEMA_VERSION:
        raise PlanValidationError(
            f"unsupported plan schema {plan.schema_version!r}; expected {PLAN_SCHEMA_VERSION}"
        )
    recomputed = canonical_digest(plan.payload())
    if recomputed != plan.plan_digest:
        raise PlanValidationError(
            f"plan digest mismatch: stored {plan.plan_digest!r}, recomputed {recomputed!r}"
        )


def plan_from_payload(envelope: Mapping[str, Any]) -> FrozenAssignmentPlan:
    """Rebuild a plan from its serialised envelope and verify the stored digest.

    The envelope is ``{"plan": <content>, "plan_digest": "sha256:..."}``. The stored digest is
    compared against a digest recomputed from the content, so a payload edited in transit is
    rejected even though it is self-consistent. This is an integrity check, not a signature: trust
    in a plan comes from the host that produced it, never from the digest alone.
    """

    if not isinstance(envelope, Mapping):
        raise PlanValidationError("plan envelope must be a mapping")
    if "plan" not in envelope or "plan_digest" not in envelope:
        raise PlanValidationError("plan envelope must carry 'plan' and 'plan_digest'")
    stored_digest = envelope["plan_digest"]
    if not isinstance(stored_digest, str) or not stored_digest:
        raise PlanValidationError("plan_digest must be a non-empty string")
    payload = envelope["plan"]
    if not isinstance(payload, Mapping):
        raise PlanValidationError("plan must be a mapping")
    try:
        schema_version = int(payload["schema_version"])
        run_id = str(payload["run_id"])
        workflow_digest_value = str(payload["workflow_digest"])
        policy = payload["policy"]
        slots = tuple(
            PlacementSlot(
                slot_id=str(item["slot_id"]),
                profile_id=str(item["profile_id"]),
                capabilities=frozenset(str(value) for value in item["capabilities"]),
                max_active_tasks=int(item["max_active_tasks"]),
            )
            for item in payload["slots"]
        )
        slot_profiles = tuple(
            SlotProfile(
                slot_id=str(item["slot_id"]),
                profile_id=str(item["profile_id"]),
                role=str(item["role"]),
                operating_scope=tuple(str(value) for value in item["operating_scope"]),
                base_tools=tuple(str(value) for value in item["base_tools"]),
                max_turns=int(item["max_turns"]),
                can_delegate=bool(item["can_delegate"]),
                definition_digest=str(item["definition_digest"]),
            )
            for item in payload["slot_profiles"]
        )
        assignments = tuple(
            AssignmentEntry(task_id=str(item["task_id"]), slot_id=str(item["slot_id"]))
            for item in payload["assignments"]
        )
        waves = tuple((str(item["task_id"]), int(item["wave"])) for item in payload["waves"])
        policy_name = str(policy["name"])
        policy_version = str(policy["version"])
        policy_config_digest = str(policy["config_digest"])
    except (KeyError, TypeError, ValueError) as error:
        raise PlanValidationError(f"malformed plan payload: {error}") from error

    plan = FrozenAssignmentPlan(
        schema_version=schema_version,
        run_id=run_id,
        workflow_digest=workflow_digest_value,
        slots=slots,
        slot_profiles=slot_profiles,
        assignments=assignments,
        wave_of_task=waves,
        policy_name=policy_name,
        policy_version=policy_version,
        policy_config_digest=policy_config_digest,
        plan_digest=canonical_digest(
            _payload(
                schema_version=schema_version,
                run_id=run_id,
                workflow_digest=workflow_digest_value,
                slots=slots,
                slot_profiles=slot_profiles,
                assignments=assignments,
                wave_of_task=waves,
                policy_name=policy_name,
                policy_version=policy_version,
                policy_config_digest=policy_config_digest,
            )
        ),
    )
    if plan.plan_digest != stored_digest:
        raise PlanValidationError(
            f"plan digest mismatch: stored {stored_digest!r}, recomputed {plan.plan_digest!r}"
        )
    verify_plan(plan)
    return plan


def plan_from_json(text: str) -> FrozenAssignmentPlan:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise PlanValidationError(f"plan is not valid JSON: {error}") from error
    return plan_from_payload(payload)


# -- binding ------------------------------------------------------------------------------------


def bind_task(
    *,
    plan: FrozenAssignmentPlan,
    logical_task_id: str,
    runtime_task_id: str,
    factory: ScopedWorkerFactory,
    parent_context: ExecutionContext,
    parent_agent: str,
    assignment: WorkerAssignment,
    prior: TaskExecutionBinding | None = None,
) -> tuple[TaskExecutionBinding, WorkerInstance]:
    """Build the real worker for one frozen assignment.

    Order matters and is the point of this function: the slot is resolved from the verified plan,
    the slot's profile is checked against the untrusted assignment **before** the factory persists
    anything, and the constructed instance is re-checked afterwards. A worker is never built first
    and stamped with a slot later.

    ``prior`` is the caller's record of an earlier binding for the same logical task. Supplying it
    makes a second, conflicting binding for that task an error instead of a silent overwrite. This
    module keeps no state of its own.
    """

    verify_plan(plan)
    if plan.run_id != str(parent_context.attributes.get("run_id", "")):
        raise BindingError(
            f"plan run_id {plan.run_id!r} does not match the parent context run id "
            f"{parent_context.attributes.get('run_id')!r}"
        )

    slot = plan.slot_for(logical_task_id)
    if assignment.profile_id != slot.profile_id:
        raise BindingError(
            f"assignment profile {assignment.profile_id!r} does not match slot "
            f"{slot.slot_id!r} profile {slot.profile_id!r}"
        )
    if not isinstance(runtime_task_id, str) or not runtime_task_id.strip():
        raise BindingError("runtime_task_id must be a non-empty string")

    if prior is not None:
        if prior.logical_task_id != logical_task_id:
            raise BindingError(
                f"prior binding is for {prior.logical_task_id!r}, not {logical_task_id!r}"
            )
        if (
            prior.placement_slot_id != slot.slot_id
            or prior.profile_id != slot.profile_id
            or prior.runtime_task_id != runtime_task_id
            or prior.plan_digest != plan.plan_digest
        ):
            raise BindingError(
                f"task {logical_task_id!r} is already bound to slot "
                f"{prior.placement_slot_id!r}; refusing to rebind it to {slot.slot_id!r}"
            )

    worker = factory.create(
        parent_context=parent_context,
        parent_agent=parent_agent,
        assignment=assignment,
        worker_id=runtime_task_id,
        task_id=runtime_task_id,
    )

    # Verify the *constructed* instance, not the request.
    if worker.record.profile_id != slot.profile_id:
        raise BindingError(
            f"constructed worker profile {worker.record.profile_id!r} does not match slot "
            f"{slot.slot_id!r} profile {slot.profile_id!r}"
        )
    if worker.record.worker_id != runtime_task_id or worker.record.task_id != runtime_task_id:
        raise BindingError("constructed worker identity does not match the requested task id")
    if worker.agent_config.name != worker.record.worker_name:
        raise BindingError("constructed agent name does not match the worker name")
    if worker.execution_context.attributes.get("task_id") != runtime_task_id:
        raise BindingError("constructed execution context does not carry this task id")

    binding = TaskExecutionBinding(
        run_id=plan.run_id,
        logical_task_id=logical_task_id,
        runtime_task_id=runtime_task_id,
        placement_slot_id=slot.slot_id,
        worker_id=worker.record.worker_id,
        agent_name=worker.agent_config.name,
        profile_id=worker.record.profile_id,
        plan_digest=plan.plan_digest,
    )
    return binding, worker
