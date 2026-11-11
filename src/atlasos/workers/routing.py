"""Stage R0: evidence-based selective delegation routing.

The router answers exactly one question: **is this candidate task plan worth delegating to more than
one worker?** It is a pure function of

* a :class:`DelegationProposal` — the candidate plan plus the evidence that supports it,
* a :class:`CapabilityCatalog` — read-only metadata derived from the trusted ``WorkerCatalog``,
* a :class:`RoutingPolicy` — frozen thresholds and version.

It deliberately does no I/O: no filesystem, no provider, no clock, no random, no shell, no DAG
rewrite. It never edits the proposal, a worker assignment or a ``TaskSpec``. Given the same inputs
and ``policy.version`` it returns the same decision, serialisable with :func:`decision_to_dict`.

Design notes (see ``docs/stage-r0-routing-report.md``):

* Evidence carries its own trust level, so a planner cannot assert ``independent=True``; an
  unsupported claim stays ``UNKNOWN`` and cannot unlock parallel execution.
* Three-valued evidence (PROVEN / DISPROVEN / UNKNOWN) replaces booleans: "no evidence" is not
  "safe".
* Gates run in a fixed order and the first blocking gate decides. Every gate is recorded, so both
  "why was no worker created?" and "why were two workers allowed?" are answerable from the decision
  object alone.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, cast

POLICY_VERSION = "r0.1.0"

#: Reason codes are a fixed vocabulary; nothing generates new codes at runtime.
REASON_INVALID_DAG = "INVALID_DAG"
REASON_MISSING_DEPENDENCY = "MISSING_DEPENDENCY"
REASON_DUPLICATE_TASK_ID = "DUPLICATE_TASK_ID"
REASON_CYCLIC_PLAN = "CYCLIC_PLAN"
REASON_UNKNOWN_PROFILE = "UNKNOWN_PROFILE"
REASON_UNCOVERED_GOAL = "UNCOVERED_GOAL"
REASON_TRIVIAL_DIRECT_TASK = "TRIVIAL_DIRECT_TASK"
REASON_INDEPENDENT_DELIVERABLES = "INDEPENDENT_DELIVERABLES"
REASON_READ_ONLY_INDEPENDENT_WORK = "READ_ONLY_INDEPENDENT_WORK"
REASON_DATA_DEPENDENCY = "DATA_DEPENDENCY"
REASON_SHARED_WRITE_RISK = "SHARED_WRITE_RISK"
REASON_UNSTABLE_INTERFACE = "UNSTABLE_INTERFACE"
REASON_CONTROLLED_MERGE_PROTOCOL = "CONTROLLED_MERGE_PROTOCOL"
REASON_SEMANTIC_COUPLING_UNKNOWN = "SEMANTIC_COUPLING_UNKNOWN"
REASON_NO_PARALLEL_WAVE = "NO_PARALLEL_WAVE"
REASON_PARALLEL_LIMIT_EXCEEDED = "PARALLEL_LIMIT_EXCEEDED"
REASON_EXTERNAL_SIDE_EFFECT = "EXTERNAL_SIDE_EFFECT"
REASON_UNKNOWN_RISK = "UNKNOWN_RISK"
REASON_CONTEXT_INSUFFICIENT = "CONTEXT_INSUFFICIENT"
REASON_INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
REASON_DELEGATION_ADMITTED = "DELEGATION_ADMITTED"
REASON_EXECUTION_MODE_UNSUPPORTED = "EXECUTION_MODE_UNSUPPORTED"


class RouteKind(StrEnum):
    """What the runtime should do with the candidate plan."""

    DIRECT = "direct"
    DELEGATE_ONE = "delegate_one"
    DELEGATE_PARALLEL = "delegate_parallel"


class EvidenceStatus(StrEnum):
    """Three-valued evidence; ``UNKNOWN`` is never treated as safe."""

    PROVEN = "proven"
    DISPROVEN = "disproven"
    UNKNOWN = "unknown"


class SourceKind(StrEnum):
    """Where an evidence item comes from. Only the first three can unlock delegation."""

    PUBLIC_TASK = "public_task"
    TRUSTED_METADATA = "trusted_metadata"
    STATIC_ANALYSIS = "static_analysis"
    PLANNER_CLAIM = "planner_claim"


TRUSTED_SOURCES = frozenset(
    {SourceKind.PUBLIC_TASK, SourceKind.TRUSTED_METADATA, SourceKind.STATIC_ANALYSIS}
)


class EvidenceKind(StrEnum):
    """Fixed evidence vocabulary. Items with another kind are ignored by the router."""

    WRITE_PATHS = "write_paths"
    READ_PATHS = "read_paths"
    RISK_CLASS = "risk_class"
    INTERFACE_SYMBOLS = "interface_symbols"
    INTERFACE_CONTRACT = "interface_contract"
    DELIVERABLE = "deliverable"
    CONTEXT = "context"
    MERGE_PROTOCOL = "merge_protocol"
    GOAL_COVERAGE = "goal_coverage"


class WorkRisk(StrEnum):
    """Router-level risk class, derived from public task evidence (not from M3 permissions)."""

    READ_ONLY = "read_only"
    LOCAL_REVERSIBLE_WRITE = "local_reversible_write"
    SHARED_MUTATION = "shared_mutation"
    EXTERNAL_SIDE_EFFECT = "external_side_effect"
    UNKNOWN = "unknown"


class GateStatus(StrEnum):
    PASS = "pass"
    INFO = "info"
    BLOCK = "block"


GATE_PROPOSAL = "GATE_0_PROPOSAL_VALIDITY"
GATE_NECESSITY = "GATE_1_DELEGATION_NECESSITY"
GATE_COUPLING = "GATE_2_DEPENDENCY_COUPLING"
GATE_PARALLEL = "GATE_3_PARALLEL_OPPORTUNITY"
GATE_RISK = "GATE_4_SIDE_EFFECT_RISK"
GATE_CONTEXT = "GATE_5_CONTEXT_SUFFICIENCY"
GATE_ADMISSION = "GATE_6_ADMISSION"


@dataclass(frozen=True)
class CandidateTask:
    """One task of the candidate plan, exactly as the planner proposed it."""

    task_id: str
    description: str
    depends_on: tuple[str, ...] = ()
    proposed_profile_id: str | None = None


@dataclass(frozen=True)
class EvidenceItem:
    """One auditable fact about the plan. ``value`` must be JSON-friendly."""

    evidence_id: str
    kind: EvidenceKind
    subject_task_ids: tuple[str, ...]
    value: object
    source_kind: SourceKind
    source_locator: str
    status: EvidenceStatus = EvidenceStatus.PROVEN

    @property
    def trusted(self) -> bool:
        return self.source_kind in TRUSTED_SOURCES


@dataclass(frozen=True)
class DelegationProposal:
    """The candidate plan plus the evidence that supports it."""

    proposal_id: str
    overall_goal: str
    candidate_tasks: tuple[CandidateTask, ...]
    evidence: tuple[EvidenceItem, ...] = ()

    def task(self, task_id: str) -> CandidateTask | None:
        for task in self.candidate_tasks:
            if task.task_id == task_id:
                return task
        return None


@dataclass(frozen=True)
class WorkerCapability:
    """Read-only routing metadata for one trusted worker profile."""

    profile_id: str
    supported_domains: tuple[str, ...]
    preferred_work_types: tuple[str, ...]
    avoid_work_types: tuple[str, ...]
    cost_tier: str
    parallel_safe: bool = True
    max_parallel_instances: int = 1


@dataclass(frozen=True)
class CapabilityCatalog:
    capabilities: tuple[WorkerCapability, ...]

    def profile_ids(self) -> tuple[str, ...]:
        return tuple(item.profile_id for item in self.capabilities)

    def resolve(self, profile_id: str | None) -> WorkerCapability | None:
        if profile_id is None:
            return None
        for item in self.capabilities:
            if item.profile_id == profile_id:
                return item
        return None

    def default_profile(self) -> WorkerCapability | None:
        for item in self.capabilities:
            if item.profile_id == "coding_worker":
                return item
        return self.capabilities[0] if self.capabilities else None


@dataclass(frozen=True)
class RoutingPolicy:
    """Frozen routing thresholds. Changing any value requires a new ``version``."""

    version: str = POLICY_VERSION
    max_parallel_workers: int = 2
    allow_shared_mutation: bool = False
    allow_unknown_risk: bool = False
    require_goal_coverage: bool = True
    require_context_evidence: bool = True
    min_tasks_for_delegation: int = 2
    allow_single_read_only_delegation: bool = True

    def digest(self) -> str:
        payload = json.dumps(
            {
                "version": self.version,
                "max_parallel_workers": self.max_parallel_workers,
                "allow_shared_mutation": self.allow_shared_mutation,
                "allow_unknown_risk": self.allow_unknown_risk,
                "require_goal_coverage": self.require_goal_coverage,
                "require_context_evidence": self.require_context_evidence,
                "min_tasks_for_delegation": self.min_tasks_for_delegation,
                "allow_single_read_only_delegation": self.allow_single_read_only_delegation,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class GateResult:
    gate: str
    status: GateStatus
    reason_codes: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    task_ids: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "gate": self.gate,
            "status": self.status.value,
            "reason_codes": list(self.reason_codes),
            "evidence_refs": list(self.evidence_refs),
            "task_ids": list(self.task_ids),
        }


@dataclass(frozen=True)
class RouteDecision:
    route: RouteKind
    approved_task_ids: tuple[str, ...]
    reason_codes: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    policy_version: str
    gates: tuple[GateResult, ...] = ()
    blocked_gates: tuple[str, ...] = ()
    diagnostics: tuple[str, ...] = ()
    proposal_id: str = ""
    policy_digest: str = ""
    max_parallel_wave_width: int = 0

    @property
    def admitted(self) -> bool:
        return self.route is not RouteKind.DIRECT

    def as_dict(self) -> dict[str, object]:
        return decision_to_dict(self)


@dataclass(frozen=True)
class _TaskEvidence:
    """Evidence resolved per task, already filtered to trusted, non-DISPROVEN items."""

    task_id: str
    write_paths: tuple[str, ...] = ()
    read_paths: tuple[str, ...] = ()
    risk: WorkRisk = WorkRisk.UNKNOWN
    risk_proven: bool = False
    interface_symbols: tuple[str, ...] = ()
    interface_contract: EvidenceStatus = EvidenceStatus.UNKNOWN
    deliverables: tuple[str, ...] = ()
    context_status: EvidenceStatus = EvidenceStatus.UNKNOWN
    context_refs: tuple[str, ...] = field(default_factory=tuple)
    refs: tuple[str, ...] = field(default_factory=tuple)

    @property
    def declared_read_only(self) -> bool:
        return self.risk is WorkRisk.READ_ONLY and self.risk_proven


def decision_to_dict(decision: RouteDecision) -> dict[str, object]:
    """Stable, JSON-ready projection of a decision (no runtime objects, no floats)."""

    return {
        "proposal_id": decision.proposal_id,
        "route": decision.route.value,
        "approved_task_ids": list(decision.approved_task_ids),
        "reason_codes": list(decision.reason_codes),
        "evidence_refs": list(decision.evidence_refs),
        "policy_version": decision.policy_version,
        "policy_digest": decision.policy_digest,
        "blocked_gates": list(decision.blocked_gates),
        "max_parallel_wave_width": decision.max_parallel_wave_width,
        "diagnostics": list(decision.diagnostics),
        "gates": [gate.as_dict() for gate in decision.gates],
    }


def decision_digest(decision: RouteDecision) -> str:
    payload = json.dumps(decision_to_dict(decision), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- plan utilities


def ready_waves(tasks: Sequence[CandidateTask]) -> list[tuple[str, ...]]:
    """Level the DAG into waves of simultaneously runnable task ids (deterministic order)."""

    order = {task.task_id: index for index, task in enumerate(tasks)}
    dependencies = {task.task_id: tuple(task.depends_on) for task in tasks}
    remaining = {task.task_id for task in tasks}
    done: set[str] = set()
    waves: list[tuple[str, ...]] = []
    while remaining:
        ready = sorted(
            (
                task_id
                for task_id in remaining
                if all(dependency in done for dependency in dependencies[task_id])
            ),
            key=lambda task_id: order[task_id],
        )
        if not ready:  # pragma: no cover - cycles are rejected before this point
            break
        waves.append(tuple(ready))
        done.update(ready)
        remaining.difference_update(ready)
    return waves


def max_wave_width(tasks: Sequence[CandidateTask]) -> int:
    return max((len(wave) for wave in ready_waves(tasks)), default=0)


def find_cycle(tasks: Sequence[CandidateTask]) -> tuple[str, ...]:
    """Return one dependency cycle as a task-id path, or ``()`` when the graph is acyclic."""

    graph = {task.task_id: tuple(task.depends_on) for task in tasks}
    state: dict[str, int] = {}
    stack: list[str] = []

    def visit(node: str) -> tuple[str, ...]:
        state[node] = 1
        stack.append(node)
        for dependency in graph.get(node, ()):
            if dependency not in graph:
                continue
            if state.get(dependency) == 1:
                index = stack.index(dependency)
                return tuple(stack[index:] + [dependency])
            if state.get(dependency, 0) == 0:
                found = visit(dependency)
                if found:
                    return found
        stack.pop()
        state[node] = 2
        return ()

    for task in tasks:
        if state.get(task.task_id, 0) == 0:
            found = visit(task.task_id)
            if found:
                return found
    return ()


_PATH_RE = re.compile(
    r"(?<![\w/.-])((?:[\w.-]+/)*[\w.-]+\.(?:py|json|md|txt|toml|yaml|yml|cfg|ini))(?![\w/])"
)
_WRITE_VERB_RE = re.compile(r"\b(write|create|implement|modify|edit|add|update|produce)\b", re.I)
_READ_VERB_RE = re.compile(r"\b(read|inspect|open|look at|check|examine|review|reuse)\b", re.I)
_NEGATION_RE = re.compile(r"\b(do not|don't|never)\b", re.I)
_SENTENCE_SPLIT = re.compile(r"(?<=[.;])\s+")


def extract_declared_paths(text: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Deterministically extract (write targets, explicit exclusions) from public task text.

    Mirrors the Stage G3-3 ownership classifier so routing and contracts agree. A path is a write
    target only when the nearest verb before it is a write verb ("read X then implement Y" owns Y),
    and a path inside a negated clause is an exclusion. Nothing is inferred beyond the text.
    """

    normalized = text.replace("/workspace/project/", "").replace("workspace/project/", "")
    owned: list[str] = []
    excluded: list[str] = []
    for sentence in (part.strip() for part in _SENTENCE_SPLIT.split(normalized)):
        if not sentence:
            continue
        mentions = [(match.group(1), match.start()) for match in _PATH_RE.finditer(sentence)]
        if not mentions:
            continue
        if _NEGATION_RE.search(sentence):
            for raw, _ in mentions:
                if raw not in excluded and raw not in owned:
                    excluded.append(raw)
            continue
        for raw, start in mentions:
            prefix = sentence[:start]
            last_write = max(
                (match.start() for match in _WRITE_VERB_RE.finditer(prefix)), default=-1
            )
            last_read = max((match.start() for match in _READ_VERB_RE.finditer(prefix)), default=-1)
            if last_write <= last_read:
                continue
            if raw not in owned:
                owned.append(raw)
            if raw in excluded:
                excluded.remove(raw)
    return tuple(sorted(owned)), tuple(sorted(excluded))


# --------------------------------------------------------------------------- capabilities

WRITE_TOOLS = frozenset({"file_write", "file_edit", "bash"})
READ_ONLY_WORK_TYPES = ("read_only_analysis", "diagnosis", "code_review")
WRITE_WORK_TYPES = ("module_implementation", "module_edit", "verification")


def capabilities_from_catalog(catalog: object) -> CapabilityCatalog:
    """Derive routing metadata from the trusted ``WorkerCatalog`` (single source of truth).

    Nothing here weakens M2: the profile ids, tools and scopes come from the catalog, and a planner
    still cannot add a profile, a tool or a scope. A read-only profile is one whose tool list has no
    write tool; that is a *routing* fact, not an authorization decision (M3 still authorises every
    action).
    """

    lookup: Any = getattr(catalog, "profiles", None)
    mapping = lookup() if callable(lookup) else lookup
    if not isinstance(mapping, Mapping):
        raise TypeError("capabilities_from_catalog requires a WorkerCatalog-like object")
    mapping = cast("Mapping[str, Any]", mapping)
    capabilities: list[WorkerCapability] = []
    for profile in mapping.values():
        tools = frozenset(getattr(profile, "base_tools", ()) or ())
        write_capable = bool(tools & WRITE_TOOLS)
        capabilities.append(
            WorkerCapability(
                profile_id=str(profile.profile_id),
                supported_domains=tuple(getattr(profile, "operating_scope", ()) or ("general",)),
                preferred_work_types=WRITE_WORK_TYPES if write_capable else READ_ONLY_WORK_TYPES,
                avoid_work_types=() if write_capable else ("module_implementation",),
                cost_tier="standard" if write_capable else "cheap",
                parallel_safe=True,
                max_parallel_instances=1,
            )
        )
    return CapabilityCatalog(tuple(capabilities))


# --------------------------------------------------------------------------- evidence


def _trusted_items(
    proposal: DelegationProposal, kind: EvidenceKind, *, status: EvidenceStatus | None = None
) -> list[EvidenceItem]:
    return [
        item
        for item in proposal.evidence
        if item.kind is kind
        and item.trusted
        and item.status is not EvidenceStatus.DISPROVEN
        and (status is None or item.status is status)
    ]


def _strings(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, Iterable):
        return tuple(str(item) for item in value)
    return ()


def _replace(current: _TaskEvidence, **updates: object) -> _TaskEvidence:
    payload: dict[str, object] = {
        "task_id": current.task_id,
        "write_paths": current.write_paths,
        "read_paths": current.read_paths,
        "risk": current.risk,
        "risk_proven": current.risk_proven,
        "interface_symbols": current.interface_symbols,
        "interface_contract": current.interface_contract,
        "deliverables": current.deliverables,
        "context_status": current.context_status,
        "context_refs": current.context_refs,
        "refs": current.refs,
    }
    payload.update(updates)
    return _TaskEvidence(**payload)  # type: ignore[arg-type]


def collect_task_evidence(proposal: DelegationProposal) -> dict[str, _TaskEvidence]:
    """Resolve per-task evidence from trusted, non-DISPROVEN items only."""

    resolved = {
        task.task_id: _TaskEvidence(task_id=task.task_id) for task in proposal.candidate_tasks
    }

    def apply(kind: EvidenceKind, update: str) -> None:
        for item in _trusted_items(proposal, kind):
            for task_id in item.subject_task_ids:
                if task_id not in resolved:
                    continue
                current = resolved[task_id]
                if update == "write_paths":
                    resolved[task_id] = _replace(
                        current,
                        write_paths=tuple(sorted({*current.write_paths, *_strings(item.value)})),
                        refs=(*current.refs, item.evidence_id),
                    )
                elif update == "read_paths":
                    resolved[task_id] = _replace(
                        current,
                        read_paths=tuple(sorted({*current.read_paths, *_strings(item.value)})),
                        refs=(*current.refs, item.evidence_id),
                    )
                elif update == "risk":
                    try:
                        risk = WorkRisk(str(item.value))
                    except ValueError:
                        risk = WorkRisk.UNKNOWN
                    resolved[task_id] = _replace(
                        current,
                        risk=risk,
                        risk_proven=item.status is EvidenceStatus.PROVEN,
                        refs=(*current.refs, item.evidence_id),
                    )
                elif update == "interface":
                    resolved[task_id] = _replace(
                        current,
                        interface_symbols=tuple(
                            sorted({*current.interface_symbols, *_strings(item.value)})
                        ),
                        refs=(*current.refs, item.evidence_id),
                    )
                elif update == "contract":
                    resolved[task_id] = _replace(
                        current,
                        interface_contract=item.status,
                        refs=(*current.refs, item.evidence_id),
                    )
                elif update == "deliverable":
                    resolved[task_id] = _replace(
                        current,
                        deliverables=tuple(sorted({*current.deliverables, *_strings(item.value)})),
                        refs=(*current.refs, item.evidence_id),
                    )
                elif update == "context":
                    resolved[task_id] = _replace(
                        current,
                        context_status=item.status,
                        context_refs=(*current.context_refs, item.evidence_id),
                        refs=(*current.refs, item.evidence_id),
                    )

    apply(EvidenceKind.WRITE_PATHS, "write_paths")
    apply(EvidenceKind.READ_PATHS, "read_paths")
    apply(EvidenceKind.RISK_CLASS, "risk")
    apply(EvidenceKind.INTERFACE_SYMBOLS, "interface")
    apply(EvidenceKind.INTERFACE_CONTRACT, "contract")
    apply(EvidenceKind.DELIVERABLE, "deliverable")
    apply(EvidenceKind.CONTEXT, "context")
    return resolved


def _merge_protocol(proposal: DelegationProposal) -> tuple[bool, tuple[str, ...]]:
    for item in _trusted_items(proposal, EvidenceKind.MERGE_PROTOCOL, status=EvidenceStatus.PROVEN):
        if bool(item.value):
            return True, (item.evidence_id,)
    return False, ()


def _goal_coverage(proposal: DelegationProposal) -> tuple[EvidenceStatus, tuple[str, ...]]:
    required = {task.task_id for task in proposal.candidate_tasks}
    for item in _trusted_items(proposal, EvidenceKind.GOAL_COVERAGE):
        covered = set(_strings(item.value))
        if item.status is EvidenceStatus.PROVEN and covered >= required:
            return EvidenceStatus.PROVEN, (item.evidence_id,)
    return EvidenceStatus.UNKNOWN, ()


# --------------------------------------------------------------------------- gates


def _gate(
    gate: str,
    status: GateStatus,
    *,
    reasons: Sequence[str] = (),
    refs: Sequence[str] = (),
    tasks: Sequence[str] = (),
) -> GateResult:
    return GateResult(
        gate=gate,
        status=status,
        reason_codes=tuple(dict.fromkeys(reasons)),
        evidence_refs=tuple(dict.fromkeys(refs)),
        task_ids=tuple(tasks),
    )


def _decision(
    proposal: DelegationProposal,
    policy: RoutingPolicy,
    gates: Sequence[GateResult],
    *,
    route: RouteKind,
    reasons: Sequence[str],
    approved: Sequence[str] = (),
    diagnostics: Sequence[str] = (),
) -> RouteDecision:
    return RouteDecision(
        route=route,
        approved_task_ids=tuple(approved),
        reason_codes=tuple(dict.fromkeys(reasons)),
        evidence_refs=tuple(dict.fromkeys(ref for gate in gates for ref in gate.evidence_refs)),
        policy_version=policy.version,
        gates=tuple(gates),
        blocked_gates=tuple(gate.gate for gate in gates if gate.status is GateStatus.BLOCK),
        diagnostics=tuple(dict.fromkeys(diagnostics)),
        proposal_id=proposal.proposal_id,
        policy_digest=policy.digest(),
        max_parallel_wave_width=max_wave_width(proposal.candidate_tasks),
    )


def route_delegation(
    proposal: DelegationProposal,
    capabilities: CapabilityCatalog,
    policy: RoutingPolicy | None = None,
) -> RouteDecision:
    """Decide whether ``proposal`` may be delegated, and in which shape."""

    active = policy or RoutingPolicy()
    gates: list[GateResult] = []
    diagnostics: list[str] = []
    tasks = proposal.candidate_tasks

    # -- Gate 0a: structural validity ---------------------------------------------
    if not tasks:
        gates.append(_gate(GATE_PROPOSAL, GateStatus.BLOCK, reasons=[REASON_INVALID_DAG]))
        return _decision(
            proposal, active, gates, route=RouteKind.DIRECT, reasons=[REASON_INVALID_DAG]
        )

    ids = [task.task_id for task in tasks]
    id_set = set(ids)
    duplicates = sorted({task_id for task_id in ids if ids.count(task_id) > 1})
    missing = sorted(
        {dependency for task in tasks for dependency in task.depends_on if dependency not in id_set}
    )
    cycle = find_cycle(tasks)
    unknown_profiles = sorted(
        {
            task.proposed_profile_id
            for task in tasks
            if task.proposed_profile_id is not None
            and capabilities.resolve(task.proposed_profile_id) is None
        }
    )
    structure_reasons: list[str] = []
    if any(not task.task_id.strip() for task in tasks):
        structure_reasons.append(REASON_INVALID_DAG)
    if duplicates:
        structure_reasons.append(REASON_DUPLICATE_TASK_ID)
    if missing:
        structure_reasons.append(REASON_MISSING_DEPENDENCY)
    if cycle:
        structure_reasons.append(REASON_CYCLIC_PLAN)
    if unknown_profiles:
        structure_reasons.append(REASON_UNKNOWN_PROFILE)
    if structure_reasons:
        gates.append(_gate(GATE_PROPOSAL, GateStatus.BLOCK, reasons=structure_reasons, tasks=ids))
        return _decision(proposal, active, gates, route=RouteKind.DIRECT, reasons=structure_reasons)

    coverage_status, coverage_refs = _goal_coverage(proposal)
    if coverage_status is not EvidenceStatus.PROVEN and active.require_goal_coverage:
        gates.append(
            _gate(
                GATE_PROPOSAL,
                GateStatus.BLOCK,
                reasons=[REASON_UNCOVERED_GOAL],
                refs=coverage_refs,
                tasks=ids,
            )
        )
        return _decision(
            proposal, active, gates, route=RouteKind.DIRECT, reasons=[REASON_UNCOVERED_GOAL]
        )
    if coverage_status is not EvidenceStatus.PROVEN:
        diagnostics.append("goal_coverage_unverified")
    gates.append(_gate(GATE_PROPOSAL, GateStatus.PASS, refs=coverage_refs, tasks=ids))

    evidence = collect_task_evidence(proposal)
    waves = ready_waves(tasks)
    width = max((len(wave) for wave in waves), default=0)

    # -- Gate 1: necessity ---------------------------------------------------------
    if len(tasks) < active.min_tasks_for_delegation:
        single = evidence[tasks[0].task_id]
        if (
            active.allow_single_read_only_delegation
            and single.declared_read_only
            and single.deliverables
            and single.context_status is EvidenceStatus.PROVEN
        ):
            gates.append(
                _gate(
                    GATE_NECESSITY,
                    GateStatus.PASS,
                    reasons=[REASON_DELEGATION_ADMITTED],
                    refs=single.refs,
                    tasks=(tasks[0].task_id,),
                )
            )
            return _decision(
                proposal,
                active,
                gates,
                route=RouteKind.DELEGATE_ONE,
                reasons=[REASON_DELEGATION_ADMITTED],
                approved=(tasks[0].task_id,),
            )
        gates.append(_gate(GATE_NECESSITY, GateStatus.BLOCK, reasons=[REASON_TRIVIAL_DIRECT_TASK]))
        return _decision(
            proposal, active, gates, route=RouteKind.DIRECT, reasons=[REASON_TRIVIAL_DIRECT_TASK]
        )
    gates.append(
        _gate(
            GATE_NECESSITY,
            GateStatus.PASS if width > 1 else GateStatus.INFO,
            reasons=[REASON_INDEPENDENT_DELIVERABLES] if width > 1 else [REASON_NO_PARALLEL_WAVE],
            tasks=ids,
        )
    )

    # -- Gate 2: dependency and coupling ------------------------------------------
    coupling_reasons: list[str] = []
    coupling_refs: list[str] = []
    merge_ok, merge_refs = _merge_protocol(proposal)

    for task in tasks:
        for upstream in task.depends_on:
            coupling_reasons.append(REASON_DATA_DEPENDENCY)
            coupling_refs.extend(evidence[upstream].refs)

    owners: dict[str, list[str]] = {}
    for task in tasks:
        for path in evidence[task.task_id].write_paths:
            owners.setdefault(path, []).append(task.task_id)
    shared = {path for path, owner_ids in owners.items() if len(set(owner_ids)) > 1}
    if shared:
        wave_of = {task_id: index for index, wave in enumerate(waves) for task_id in wave}
        concurrent = any(
            len({wave_of.get(owner) for owner in owners[path]}) < len(set(owners[path]))
            for path in shared
        )
        if (merge_ok or active.allow_shared_mutation) and not concurrent:
            # Serialised by the declared DAG, with a trusted merge protocol: a controlled
            # collaboration rather than a conflict.
            coupling_reasons.append(REASON_CONTROLLED_MERGE_PROTOCOL)
            coupling_refs.extend(merge_refs)
        else:
            coupling_reasons.append(REASON_SHARED_WRITE_RISK)
            for path in sorted(shared):
                for owner in sorted(set(owners[path])):
                    coupling_refs.extend(evidence[owner].refs)

    unstable = [
        task.task_id
        for task in tasks
        if evidence[task.task_id].interface_symbols
        and evidence[task.task_id].interface_contract is not EvidenceStatus.PROVEN
    ]
    if unstable:
        coupling_reasons.append(REASON_UNSTABLE_INTERFACE)
        for task_id in unstable:
            coupling_refs.extend(evidence[task_id].refs)

    if any(
        not evidence[task.task_id].write_paths and evidence[task.task_id].risk is WorkRisk.UNKNOWN
        for task in tasks
    ):
        coupling_reasons.append(REASON_SEMANTIC_COUPLING_UNKNOWN)

    blocking_coupling = sorted(
        {
            REASON_SHARED_WRITE_RISK,
            REASON_UNSTABLE_INTERFACE,
            REASON_SEMANTIC_COUPLING_UNKNOWN,
        }.intersection(coupling_reasons)
    )
    gates.append(
        _gate(
            GATE_COUPLING,
            GateStatus.BLOCK if blocking_coupling else GateStatus.PASS,
            reasons=coupling_reasons,
            refs=coupling_refs,
            tasks=ids,
        )
    )
    if blocking_coupling:
        return _decision(proposal, active, gates, route=RouteKind.DIRECT, reasons=blocking_coupling)

    # -- Gate 3: parallel opportunity ---------------------------------------------
    if width < 2:
        gates.append(_gate(GATE_PARALLEL, GateStatus.BLOCK, reasons=[REASON_NO_PARALLEL_WAVE]))
        return _decision(
            proposal, active, gates, route=RouteKind.DIRECT, reasons=[REASON_NO_PARALLEL_WAVE]
        )
    if width > active.max_parallel_workers:
        gates.append(
            _gate(
                GATE_PARALLEL,
                GateStatus.BLOCK,
                reasons=[REASON_PARALLEL_LIMIT_EXCEEDED],
                tasks=ids,
            )
        )
        return _decision(
            proposal,
            active,
            gates,
            route=RouteKind.DIRECT,
            reasons=[REASON_PARALLEL_LIMIT_EXCEEDED],
            diagnostics=[f"wave_width={width}>max_parallel_workers"],
        )
    gates.append(_gate(GATE_PARALLEL, GateStatus.PASS, tasks=ids))

    # -- Gate 4: side effect and integration risk ---------------------------------
    risk_reasons: list[str] = []
    risk_refs: list[str] = []
    for task in tasks:
        current = evidence[task.task_id]
        risk_refs.extend(current.refs)
        if current.risk is WorkRisk.EXTERNAL_SIDE_EFFECT:
            risk_reasons.append(REASON_EXTERNAL_SIDE_EFFECT)
        elif current.risk is WorkRisk.SHARED_MUTATION and not merge_ok:
            risk_reasons.append(REASON_SHARED_WRITE_RISK)
        elif current.risk is WorkRisk.UNKNOWN and not active.allow_unknown_risk:
            risk_reasons.append(REASON_UNKNOWN_RISK)
    if risk_reasons:
        unique = sorted(set(risk_reasons))
        gates.append(_gate(GATE_RISK, GateStatus.BLOCK, reasons=unique, refs=risk_refs, tasks=ids))
        return _decision(proposal, active, gates, route=RouteKind.DIRECT, reasons=unique)
    gates.append(_gate(GATE_RISK, GateStatus.PASS, refs=risk_refs, tasks=ids))

    # -- Gate 5: context sufficiency ----------------------------------------------
    context_reasons: list[str] = []
    context_refs: list[str] = []
    for task in tasks:
        current = evidence[task.task_id]
        context_refs.extend(current.context_refs or current.refs)
        needs_context = active.require_context_evidence
        if (
            current.context_status is EvidenceStatus.DISPROVEN
            or needs_context
            and current.context_status is not EvidenceStatus.PROVEN
        ):
            context_reasons.append(REASON_CONTEXT_INSUFFICIENT)
        if not current.deliverables:
            context_reasons.append(REASON_CONTEXT_INSUFFICIENT)
    if context_reasons:
        gates.append(
            _gate(
                GATE_CONTEXT,
                GateStatus.BLOCK,
                reasons=sorted(set(context_reasons)),
                refs=context_refs,
                tasks=ids,
            )
        )
        return _decision(
            proposal,
            active,
            gates,
            route=RouteKind.DIRECT,
            reasons=[REASON_CONTEXT_INSUFFICIENT],
        )
    gates.append(_gate(GATE_CONTEXT, GateStatus.PASS, refs=context_refs, tasks=ids))

    # -- Gate 6: admission ---------------------------------------------------------
    first_wave = waves[0] if waves else ()
    read_only = all(evidence[task_id].declared_read_only for task_id in first_wave)
    gates.append(
        _gate(
            GATE_ADMISSION,
            GateStatus.PASS,
            reasons=[
                REASON_DELEGATION_ADMITTED,
                REASON_READ_ONLY_INDEPENDENT_WORK if read_only else REASON_INDEPENDENT_DELIVERABLES,
            ],
            tasks=ids,
        )
    )
    return _decision(
        proposal,
        active,
        gates,
        route=RouteKind.DELEGATE_PARALLEL,
        reasons=[REASON_DELEGATION_ADMITTED],
        approved=ids,
        diagnostics=diagnostics,
    )
