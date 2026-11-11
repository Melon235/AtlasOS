"""Stage R0: build routing inputs from public Stage E1 task metadata, and apply the decision.

This module is the *adapter* between the benchmark definition and
:mod:`atlasos.workers.routing`. It only reads public benchmark data (task text, ``depends_on``,
``required_artifacts``) and produces evidence with explicit provenance, so the router never has to
trust a planner claim and never touches hidden files or the oracle.

The routed plan returned here is consumed by ``runner.py``: ``steps`` decides whether the existing
single-worker (A0) or multi-worker (A1) execution path is used. Nothing else about execution
changes — same worker factory, same prompts, same DAG, same budget.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from atlasos.workers.routing import (
    REASON_EXECUTION_MODE_UNSUPPORTED,
    CandidateTask,
    CapabilityCatalog,
    DelegationProposal,
    EvidenceItem,
    EvidenceKind,
    EvidenceStatus,
    RouteDecision,
    RouteKind,
    RoutingPolicy,
    SourceKind,
    WorkRisk,
    decision_digest,
    extract_declared_paths,
    route_delegation,
)

__all__ = [
    "DELEGATION_ROUTE_MODES",
    "DELEGATION_ROUTE_OFF",
    "DELEGATION_ROUTE_SELECTIVE",
    "DELEGATION_ROUTE_SHADOW",
    "ROUTING_POLICY_VERSION",
    "ROUTING_POLICY_VERSION_WIDE",
    "ROUTING_POLICY_VERSIONS",
    "RoutedPlan",
    "a0_steps",
    "build_proposal",
    "decision_digest",
    "expected_worker_count",
    "plan_route",
    "routing_policy",
]

if TYPE_CHECKING:  # pragma: no cover - typing only
    from experiments.evaluation.models import BenchmarkTask, WorkerStep

_EXTERNAL_RE = re.compile(
    r"\b(http|https|network|email|send|payment|charge|deploy|publish|upload|external api)\b", re.I
)
_READ_ONLY_RE = re.compile(
    r"\b(read|inspect|review|analyse|analyze|diagnose|report on|explain)\b", re.I
)
_WRITE_RE = re.compile(r"\b(write|create|implement|modify|edit|add|update|produce|merge)\b", re.I)
MAX_INTERFACE_SYMBOLS = 6

DELEGATION_ROUTE_OFF = "off"
DELEGATION_ROUTE_SHADOW = "shadow"
DELEGATION_ROUTE_SELECTIVE = "selective"
DELEGATION_ROUTE_MODES = (
    DELEGATION_ROUTE_OFF,
    DELEGATION_ROUTE_SHADOW,
    DELEGATION_ROUTE_SELECTIVE,
)

#: Frozen R0 policy. Any change here requires a new ``version`` and a new experiment round.
ROUTING_POLICY_VERSION = "r0.1.0"
#: Stage R0.5 width-sensitivity policy. Identical to ``r0.1.0`` except ``max_parallel_workers``.
ROUTING_POLICY_VERSION_WIDE = "r0.2.0"
ROUTING_POLICY_VERSIONS = (ROUTING_POLICY_VERSION, ROUTING_POLICY_VERSION_WIDE)

#: The only field that may differ between the two frozen versions.
MAX_PARALLEL_BY_VERSION = {
    ROUTING_POLICY_VERSION: 2,
    ROUTING_POLICY_VERSION_WIDE: 3,
}


def routing_policy(version: str = ROUTING_POLICY_VERSION) -> RoutingPolicy:
    """Frozen routing policy for this stage (single source of truth).

    ``r0.1.0`` is the official policy and is never redefined in place. ``r0.2.0`` exists only for
    the Stage R0.5 width-sensitivity experiment and differs in exactly one field
    (``max_parallel_workers``); every gate, reason code and evidence rule is shared.
    """

    if version not in MAX_PARALLEL_BY_VERSION:
        raise ValueError(f"unknown routing policy version: {version!r}")
    return RoutingPolicy(
        version=version,
        max_parallel_workers=MAX_PARALLEL_BY_VERSION[version],
        allow_shared_mutation=False,
        allow_unknown_risk=False,
        require_goal_coverage=True,
        require_context_evidence=True,
        min_tasks_for_delegation=2,
        allow_single_read_only_delegation=True,
    )


@dataclass(frozen=True)
class RoutedPlan:
    """Decision plus the concrete execution shape it implies."""

    decision: RouteDecision
    steps: tuple[WorkerStep, ...]
    execution_mode: str
    diagnostics: tuple[str, ...] = ()

    @property
    def is_direct(self) -> bool:
        return self.execution_mode == "a0"

    def as_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision.as_dict(),
            "execution_mode": self.execution_mode,
            "executed_task_ids": [step.task_id for step in self.steps],
            "diagnostics": list(self.diagnostics),
        }


def _contract_items(task: BenchmarkTask, steps: tuple[WorkerStep, ...]) -> list[EvidenceItem]:
    """Stage R0.5: frozen-interface evidence, verified against the public fixture.

    A task may declare ``frozen_contracts[task_id] = (path, ...)``. The declaration alone proves
    nothing: a contract is only PROVEN when every declared document is actually part of the public
    fixture (``visible_files``). Declared-but-absent documents become UNKNOWN evidence, so the
    router still blocks on ``UNSTABLE_INTERFACE``.
    """

    items: list[EvidenceItem] = []
    declared = getattr(task, "frozen_contracts", None) or {}
    for step in steps:
        paths = tuple(declared.get(step.task_id, ()))
        if not paths:
            continue
        present = tuple(path for path in paths if path in task.visible_files)
        missing = tuple(path for path in paths if path not in task.visible_files)
        items.append(
            EvidenceItem(
                evidence_id=f"contract:{step.task_id}",
                kind=EvidenceKind.INTERFACE_CONTRACT,
                subject_task_ids=(step.task_id,),
                value={"declared": list(paths), "present": list(present), "missing": list(missing)},
                source_kind=SourceKind.TRUSTED_METADATA,
                source_locator="task.visible_files",
                status=EvidenceStatus.PROVEN if present and not missing else EvidenceStatus.UNKNOWN,
            )
        )
    return items


def _risk_for(text: str, write_paths: tuple[str, ...]) -> tuple[WorkRisk, EvidenceStatus]:
    if _EXTERNAL_RE.search(text):
        return WorkRisk.EXTERNAL_SIDE_EFFECT, EvidenceStatus.PROVEN
    if write_paths:
        return WorkRisk.LOCAL_REVERSIBLE_WRITE, EvidenceStatus.PROVEN
    if _READ_ONLY_RE.search(text) and not _WRITE_RE.search(text):
        return WorkRisk.READ_ONLY, EvidenceStatus.PROVEN
    return WorkRisk.UNKNOWN, EvidenceStatus.UNKNOWN


def _interface_symbols(step: WorkerStep, steps: tuple[WorkerStep, ...]) -> tuple[str, ...]:
    """Sibling modules/symbols the task text names (public text only)."""

    text = step.description if hasattr(step, "description") else step.prompt
    symbols: list[str] = []
    for other in steps:
        if other.task_id == step.task_id:
            continue
        candidates = {other.task_id, other.task_id.rsplit("-", 1)[-1]}
        for candidate in sorted(candidates):
            if len(candidate) < 3:
                continue
            if re.search(rf"\b{re.escape(candidate)}\b", text, re.I) and candidate not in symbols:
                symbols.append(candidate)
                break
    return tuple(sorted(symbols))[:MAX_INTERFACE_SYMBOLS]


def build_proposal(
    task: BenchmarkTask,
    steps: tuple[WorkerStep, ...],
    *,
    proposal_id: str,
) -> DelegationProposal:
    """Turn a public benchmark plan into a routing proposal with provenance-tagged evidence."""

    evidence: list[EvidenceItem] = []
    covered_artifacts: set[str] = set()

    for step in steps:
        write_paths, excluded = extract_declared_paths(step.prompt)
        if excluded:
            evidence.append(
                EvidenceItem(
                    evidence_id=f"excluded:{step.task_id}",
                    kind=EvidenceKind.READ_PATHS,
                    subject_task_ids=(step.task_id,),
                    value=excluded,
                    source_kind=SourceKind.STATIC_ANALYSIS,
                    source_locator=f"step:{step.task_id}:prompt",
                )
            )
        if write_paths:
            evidence.append(
                EvidenceItem(
                    evidence_id=f"write:{step.task_id}",
                    kind=EvidenceKind.WRITE_PATHS,
                    subject_task_ids=(step.task_id,),
                    value=write_paths,
                    source_kind=SourceKind.STATIC_ANALYSIS,
                    source_locator=f"step:{step.task_id}:prompt",
                )
            )
            evidence.append(
                EvidenceItem(
                    evidence_id=f"deliverable:{step.task_id}",
                    kind=EvidenceKind.DELIVERABLE,
                    subject_task_ids=(step.task_id,),
                    value=write_paths,
                    source_kind=SourceKind.STATIC_ANALYSIS,
                    source_locator=f"step:{step.task_id}:prompt",
                )
            )
            covered_artifacts.update(write_paths)

        risk, risk_status = _risk_for(step.prompt, write_paths)
        evidence.append(
            EvidenceItem(
                evidence_id=f"risk:{step.task_id}",
                kind=EvidenceKind.RISK_CLASS,
                subject_task_ids=(step.task_id,),
                value=risk.value,
                source_kind=SourceKind.STATIC_ANALYSIS
                if risk_status is EvidenceStatus.PROVEN
                else SourceKind.PLANNER_CLAIM,
                source_locator=f"step:{step.task_id}:prompt",
                status=risk_status,
            )
        )

        symbols = _interface_symbols(step, steps)
        if symbols:
            evidence.append(
                EvidenceItem(
                    evidence_id=f"interface:{step.task_id}",
                    kind=EvidenceKind.INTERFACE_SYMBOLS,
                    subject_task_ids=(step.task_id,),
                    value=symbols,
                    source_kind=SourceKind.STATIC_ANALYSIS,
                    source_locator=f"step:{step.task_id}:prompt",
                )
            )

        has_goal = bool(step.prompt.strip())
        has_deliverable = bool(write_paths or task.required_artifacts)
        upstream_known = all(
            dependency in {item.task_id for item in steps} for dependency in step.depends_on
        )
        context_status = (
            EvidenceStatus.PROVEN
            if (has_goal and has_deliverable and upstream_known)
            else EvidenceStatus.UNKNOWN
        )
        evidence.append(
            EvidenceItem(
                evidence_id=f"context:{step.task_id}",
                kind=EvidenceKind.CONTEXT,
                subject_task_ids=(step.task_id,),
                value={
                    "goal": has_goal,
                    "deliverable": has_deliverable,
                    "upstream_declared": upstream_known,
                },
                source_kind=SourceKind.STATIC_ANALYSIS,
                source_locator=f"step:{step.task_id}:prompt",
                status=context_status,
            )
        )

    required = set(task.required_artifacts)
    contributing = tuple(step.task_id for step in steps if extract_declared_paths(step.prompt)[0])
    coverage_proven = (
        bool(required) and required <= covered_artifacts and len(contributing) == len(steps)
    )
    evidence.append(
        EvidenceItem(
            evidence_id="coverage:plan",
            kind=EvidenceKind.GOAL_COVERAGE,
            subject_task_ids=tuple(step.task_id for step in steps),
            value=contributing,
            source_kind=SourceKind.TRUSTED_METADATA,
            source_locator="task.required_artifacts",
            status=EvidenceStatus.PROVEN if coverage_proven else EvidenceStatus.UNKNOWN,
        )
    )
    evidence.extend(_contract_items(task, steps))

    return DelegationProposal(
        proposal_id=proposal_id,
        overall_goal=task.prompt,
        candidate_tasks=tuple(
            CandidateTask(
                task_id=step.task_id,
                description=step.prompt,
                depends_on=tuple(step.depends_on),
                proposed_profile_id="coding_worker",
            )
            for step in steps
        ),
        evidence=tuple(evidence),
    )


def direct_steps(task: BenchmarkTask) -> tuple[WorkerStep, ...]:
    """The existing A0 plan: one worker holding the whole public goal (never rebuilt here)."""

    return task.workers


def plan_route(
    task: BenchmarkTask,
    capabilities: CapabilityCatalog,
    policy: RoutingPolicy | None = None,
) -> RoutedPlan:
    """Route the A1 candidate plan and choose the execution shape (A0 fallback or A1)."""

    steps = task.workers
    if len(steps) <= 1:
        proposal = build_proposal(task, steps, proposal_id=f"{task.sample_id}:single")
        decision = route_delegation(proposal, capabilities, policy)
        return RoutedPlan(
            decision=decision,
            steps=steps,
            execution_mode="a0",
            diagnostics=("single_candidate_task",),
        )

    proposal = build_proposal(task, steps, proposal_id=f"{task.sample_id}:candidate")
    decision = route_delegation(proposal, capabilities, policy)
    if decision.route is RouteKind.DELEGATE_PARALLEL:
        return RoutedPlan(decision=decision, steps=steps, execution_mode="a1")
    if decision.route is RouteKind.DELEGATE_ONE:
        # R0 has no single-delegation execution path in the evaluation harness; record the
        # limitation instead of pretending the decision was executed.
        return RoutedPlan(
            decision=decision,
            steps=steps,
            execution_mode="a0",
            diagnostics=(REASON_EXECUTION_MODE_UNSUPPORTED,),
        )
    return RoutedPlan(decision=decision, steps=steps, execution_mode="a0")


def a0_steps(task: BenchmarkTask) -> tuple[WorkerStep, ...]:
    """Single-worker plan for a routed DIRECT decision (same public goal, same profile)."""

    from experiments.evaluation.models import WorkerStep

    return (WorkerStep(task_id="solo", title=f"{task.sample_id}-solo", prompt=task.prompt),)


def expected_worker_count(steps: tuple[WorkerStep, ...]) -> int:
    return len(steps)
