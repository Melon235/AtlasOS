"""Stage R0: delegation router unit tests (pure functions, no I/O)."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from atlasos.workers import WorkerCatalog
from atlasos.workers.routing import (
    REASON_CONTEXT_INSUFFICIENT,
    REASON_CONTROLLED_MERGE_PROTOCOL,
    REASON_CYCLIC_PLAN,
    REASON_DELEGATION_ADMITTED,
    REASON_DUPLICATE_TASK_ID,
    REASON_MISSING_DEPENDENCY,
    REASON_NO_PARALLEL_WAVE,
    REASON_PARALLEL_LIMIT_EXCEEDED,
    REASON_SHARED_WRITE_RISK,
    REASON_TRIVIAL_DIRECT_TASK,
    REASON_UNCOVERED_GOAL,
    REASON_UNKNOWN_PROFILE,
    REASON_UNSTABLE_INTERFACE,
    CandidateTask,
    CapabilityCatalog,
    DelegationProposal,
    EvidenceItem,
    EvidenceKind,
    EvidenceStatus,
    RouteKind,
    RoutingPolicy,
    SourceKind,
    WorkRisk,
    capabilities_from_catalog,
    decision_digest,
    decision_to_dict,
    extract_declared_paths,
    max_wave_width,
    ready_waves,
    route_delegation,
)

POLICY = RoutingPolicy()


def _proposal(
    tasks: list[CandidateTask],
    evidence: list[EvidenceItem],
    *,
    proposal_id: str = "p1",
) -> DelegationProposal:
    return DelegationProposal(
        proposal_id=proposal_id,
        overall_goal="public goal",
        candidate_tasks=tuple(tasks),
        evidence=tuple(evidence),
    )


def _write(task_id: str, paths: tuple[str, ...]) -> EvidenceItem:
    return EvidenceItem(
        evidence_id=f"write:{task_id}",
        kind=EvidenceKind.WRITE_PATHS,
        subject_task_ids=(task_id,),
        value=paths,
        source_kind=SourceKind.STATIC_ANALYSIS,
        source_locator=f"step:{task_id}",
    )


def _risk(
    task_id: str, risk: WorkRisk, status: EvidenceStatus = EvidenceStatus.PROVEN
) -> EvidenceItem:
    return EvidenceItem(
        evidence_id=f"risk:{task_id}",
        kind=EvidenceKind.RISK_CLASS,
        subject_task_ids=(task_id,),
        value=risk.value,
        source_kind=SourceKind.STATIC_ANALYSIS,
        source_locator=f"step:{task_id}",
        status=status,
    )


def _deliverable(task_id: str, paths: tuple[str, ...]) -> EvidenceItem:
    return EvidenceItem(
        evidence_id=f"deliverable:{task_id}",
        kind=EvidenceKind.DELIVERABLE,
        subject_task_ids=(task_id,),
        value=paths,
        source_kind=SourceKind.STATIC_ANALYSIS,
        source_locator=f"step:{task_id}",
    )


def _context(task_id: str, status: EvidenceStatus = EvidenceStatus.PROVEN) -> EvidenceItem:
    return EvidenceItem(
        evidence_id=f"context:{task_id}",
        kind=EvidenceKind.CONTEXT,
        subject_task_ids=(task_id,),
        value={"goal": True},
        source_kind=SourceKind.STATIC_ANALYSIS,
        source_locator=f"step:{task_id}",
        status=status,
    )


def _coverage(tasks: list[CandidateTask], *, proven: bool = True) -> EvidenceItem:
    return EvidenceItem(
        evidence_id="coverage",
        kind=EvidenceKind.GOAL_COVERAGE,
        subject_task_ids=tuple(task.task_id for task in tasks),
        value=tuple(task.task_id for task in tasks),
        source_kind=SourceKind.TRUSTED_METADATA,
        source_locator="task.required_artifacts",
        status=EvidenceStatus.PROVEN if proven else EvidenceStatus.UNKNOWN,
    )


def _interface(task_id: str, symbols: tuple[str, ...]) -> EvidenceItem:
    return EvidenceItem(
        evidence_id=f"interface:{task_id}",
        kind=EvidenceKind.INTERFACE_SYMBOLS,
        subject_task_ids=(task_id,),
        value=symbols,
        source_kind=SourceKind.STATIC_ANALYSIS,
        source_locator=f"step:{task_id}",
    )


def _contract(task_id: str) -> EvidenceItem:
    return EvidenceItem(
        evidence_id=f"contract:{task_id}",
        kind=EvidenceKind.INTERFACE_CONTRACT,
        subject_task_ids=(task_id,),
        value="frozen",
        source_kind=SourceKind.TRUSTED_METADATA,
        source_locator="interface registry",
        status=EvidenceStatus.PROVEN,
    )


def _read_only_pair() -> tuple[list[CandidateTask], list[EvidenceItem]]:
    tasks = [
        CandidateTask("a", "Inspect module X and report findings.", (), "research_worker"),
        CandidateTask("b", "Inspect module Y and report findings.", (), "research_worker"),
    ]
    evidence = [
        _risk("a", WorkRisk.READ_ONLY),
        _risk("b", WorkRisk.READ_ONLY),
        _deliverable("a", ("report-x.md",)),
        _deliverable("b", ("report-y.md",)),
        _context("a"),
        _context("b"),
        _coverage(tasks),
    ]
    return tasks, evidence


def _write_pair(
    *, shared: bool = False, interface: tuple[str, ...] = (), frozen: bool = False
) -> tuple[list[CandidateTask], list[EvidenceItem]]:
    tasks = [
        CandidateTask("a", "Implement a.py module.", (), "coding_worker"),
        CandidateTask("b", "Implement b.py module.", (), "coding_worker"),
    ]
    paths_a = ("a.py", "shared.py") if shared else ("a.py",)
    paths_b = ("b.py", "shared.py") if shared else ("b.py",)
    evidence = [
        _write("a", paths_a),
        _write("b", paths_b),
        _deliverable("a", paths_a),
        _deliverable("b", paths_b),
        _risk("a", WorkRisk.LOCAL_REVERSIBLE_WRITE),
        _risk("b", WorkRisk.LOCAL_REVERSIBLE_WRITE),
        _context("a"),
        _context("b"),
        _coverage(tasks),
    ]
    if interface:
        evidence.append(_interface("b", interface))
        if frozen:
            evidence.append(_contract("b"))
    return tasks, evidence


# --------------------------------------------------------------------------- determinism


def test_router_is_deterministic() -> None:
    tasks, evidence = _read_only_pair()
    catalog = capabilities_from_catalog(WorkerCatalog())
    first = route_delegation(_proposal(tasks, evidence), catalog)
    second = route_delegation(_proposal(tasks, evidence), catalog)
    assert decision_to_dict(first) == decision_to_dict(second)
    assert decision_digest(first) == decision_digest(second)


def test_decision_is_json_serialisable_and_stable() -> None:
    tasks, evidence = _read_only_pair()
    decision = route_delegation(
        _proposal(tasks, evidence), capabilities_from_catalog(WorkerCatalog())
    )
    payload = json.loads(json.dumps(decision_to_dict(decision), sort_keys=True))
    assert payload["route"] == "delegate_parallel"
    assert set(payload) == {
        "proposal_id",
        "route",
        "approved_task_ids",
        "reason_codes",
        "evidence_refs",
        "policy_version",
        "policy_digest",
        "blocked_gates",
        "max_parallel_wave_width",
        "diagnostics",
        "gates",
    }


def test_repeated_calls_share_no_state() -> None:
    tasks, evidence = _read_only_pair()
    catalog = capabilities_from_catalog(WorkerCatalog())
    baseline = decision_to_dict(route_delegation(_proposal(tasks, evidence), catalog))
    route_delegation(_proposal(tasks, evidence, proposal_id="other"), catalog)
    assert decision_to_dict(route_delegation(_proposal(tasks, evidence), catalog)) == baseline


# --------------------------------------------------------------------------- gates


def test_single_task_goes_direct() -> None:
    tasks = [CandidateTask("only", "Implement one module.", (), "coding_worker")]
    decision = route_delegation(
        _proposal(tasks, [_coverage(tasks)]), capabilities_from_catalog(WorkerCatalog())
    )
    assert decision.route is RouteKind.DIRECT
    assert REASON_TRIVIAL_DIRECT_TASK in decision.reason_codes


def test_chain_has_no_parallel_wave() -> None:
    tasks = [
        CandidateTask("a", "Implement a.py.", (), "coding_worker"),
        CandidateTask("b", "Implement b.py.", ("a",), "coding_worker"),
        CandidateTask("c", "Implement c.py.", ("b",), "coding_worker"),
    ]
    evidence = [
        _write("a", ("a.py",)),
        _write("b", ("b.py",)),
        _write("c", ("c.py",)),
        _deliverable("a", ("a.py",)),
        _deliverable("b", ("b.py",)),
        _deliverable("c", ("c.py",)),
        _risk("a", WorkRisk.LOCAL_REVERSIBLE_WRITE),
        _risk("b", WorkRisk.LOCAL_REVERSIBLE_WRITE),
        _risk("c", WorkRisk.LOCAL_REVERSIBLE_WRITE),
        _context("a"),
        _context("b"),
        _context("c"),
        _coverage(tasks),
    ]
    assert max_wave_width(tasks) == 1
    assert ready_waves(tasks) == [("a",), ("b",), ("c",)]
    decision = route_delegation(
        _proposal(tasks, evidence), capabilities_from_catalog(WorkerCatalog())
    )
    assert decision.route is RouteKind.DIRECT
    assert REASON_NO_PARALLEL_WAVE in decision.reason_codes


def test_independent_read_only_tasks_are_admitted() -> None:
    tasks, evidence = _read_only_pair()
    decision = route_delegation(
        _proposal(tasks, evidence), capabilities_from_catalog(WorkerCatalog())
    )
    assert decision.route is RouteKind.DELEGATE_PARALLEL
    assert decision.approved_task_ids == ("a", "b")
    assert REASON_DELEGATION_ADMITTED in decision.reason_codes


def test_disjoint_writes_without_interface_are_admitted() -> None:
    tasks, evidence = _write_pair()
    decision = route_delegation(
        _proposal(tasks, evidence), capabilities_from_catalog(WorkerCatalog())
    )
    assert decision.route is RouteKind.DELEGATE_PARALLEL


def test_shared_write_is_blocked() -> None:
    tasks, evidence = _write_pair(shared=True)
    decision = route_delegation(
        _proposal(tasks, evidence), capabilities_from_catalog(WorkerCatalog())
    )
    assert decision.route is RouteKind.DIRECT
    assert REASON_SHARED_WRITE_RISK in decision.reason_codes


def _merge_protocol_evidence() -> EvidenceItem:
    return EvidenceItem(
        evidence_id="merge",
        kind=EvidenceKind.MERGE_PROTOCOL,
        subject_task_ids=("a", "b"),
        value="merge step reads both state files",
        source_kind=SourceKind.PUBLIC_TASK,
        source_locator="task text",
    )


def test_concurrent_shared_write_is_blocked_even_with_a_merge_protocol() -> None:
    """Two writers of one file in the same wave: a merge step cannot resolve a race."""

    tasks, evidence = _write_pair(shared=True)
    evidence.append(_merge_protocol_evidence())
    decision = route_delegation(
        _proposal(tasks, evidence), capabilities_from_catalog(WorkerCatalog())
    )
    coupling = next(gate for gate in decision.gates if gate.gate.endswith("COUPLING"))
    assert REASON_SHARED_WRITE_RISK in coupling.reason_codes
    assert REASON_CONTROLLED_MERGE_PROTOCOL not in coupling.reason_codes
    assert coupling.evidence_refs
    assert decision.route is RouteKind.DIRECT


def test_serialised_shared_write_is_a_controlled_collaboration() -> None:
    tasks = [
        CandidateTask("a", "Implement shared.py part one.", (), "coding_worker"),
        CandidateTask("b", "Implement shared.py part two.", ("a",), "coding_worker"),
    ]
    evidence = [
        _write("a", ("shared.py",)),
        _write("b", ("shared.py",)),
        _deliverable("a", ("shared.py",)),
        _deliverable("b", ("shared.py",)),
        _risk("a", WorkRisk.LOCAL_REVERSIBLE_WRITE),
        _risk("b", WorkRisk.LOCAL_REVERSIBLE_WRITE),
        _context("a"),
        _context("b"),
        _coverage(tasks),
        _merge_protocol_evidence(),
    ]
    decision = route_delegation(
        _proposal(tasks, evidence), capabilities_from_catalog(WorkerCatalog())
    )
    coupling = next(gate for gate in decision.gates if gate.gate.endswith("COUPLING"))
    assert REASON_CONTROLLED_MERGE_PROTOCOL in coupling.reason_codes
    assert REASON_SHARED_WRITE_RISK not in coupling.reason_codes
    # A serialised plan is a legal DAG, but it offers no parallel wave.
    assert decision.route is RouteKind.DIRECT
    assert REASON_NO_PARALLEL_WAVE in decision.reason_codes


def test_unfrozen_interface_blocks_parallel() -> None:
    tasks, evidence = _write_pair(interface=("a",))
    decision = route_delegation(
        _proposal(tasks, evidence), capabilities_from_catalog(WorkerCatalog())
    )
    assert decision.route is RouteKind.DIRECT
    assert REASON_UNSTABLE_INTERFACE in decision.reason_codes


def test_frozen_interface_allows_parallel() -> None:
    tasks, evidence = _write_pair(interface=("a",), frozen=True)
    decision = route_delegation(
        _proposal(tasks, evidence), capabilities_from_catalog(WorkerCatalog())
    )
    assert decision.route is RouteKind.DELEGATE_PARALLEL


def test_context_insufficient_falls_back() -> None:
    tasks, evidence = _read_only_pair()
    evidence = [item for item in evidence if item.kind is not EvidenceKind.CONTEXT]
    decision = route_delegation(
        _proposal(tasks, evidence), capabilities_from_catalog(WorkerCatalog())
    )
    assert decision.route is RouteKind.DIRECT
    assert REASON_CONTEXT_INSUFFICIENT in decision.reason_codes


def test_missing_deliverable_blocks() -> None:
    tasks, evidence = _read_only_pair()
    evidence = [item for item in evidence if item.kind is not EvidenceKind.DELIVERABLE]
    decision = route_delegation(
        _proposal(tasks, evidence), capabilities_from_catalog(WorkerCatalog())
    )
    assert decision.route is RouteKind.DIRECT
    assert REASON_CONTEXT_INSUFFICIENT in decision.reason_codes


def test_cycle_is_rejected() -> None:
    tasks = [
        CandidateTask("a", "one", ("b",), "coding_worker"),
        CandidateTask("b", "two", ("a",), "coding_worker"),
    ]
    decision = route_delegation(
        _proposal(tasks, [_coverage(tasks)]), capabilities_from_catalog(WorkerCatalog())
    )
    assert decision.route is RouteKind.DIRECT
    assert REASON_CYCLIC_PLAN in decision.reason_codes


def test_dangling_dependency_is_rejected() -> None:
    tasks = [
        CandidateTask("a", "one", ("ghost",), "coding_worker"),
        CandidateTask("b", "two", (), "coding_worker"),
    ]
    decision = route_delegation(
        _proposal(tasks, [_coverage(tasks)]), capabilities_from_catalog(WorkerCatalog())
    )
    assert decision.route is RouteKind.DIRECT
    assert REASON_MISSING_DEPENDENCY in decision.reason_codes


def test_duplicate_task_ids_are_rejected() -> None:
    tasks = [
        CandidateTask("a", "one", (), "coding_worker"),
        CandidateTask("a", "two", (), "coding_worker"),
    ]
    decision = route_delegation(
        _proposal(tasks, [_coverage(tasks)]), capabilities_from_catalog(WorkerCatalog())
    )
    assert decision.route is RouteKind.DIRECT
    assert REASON_DUPLICATE_TASK_ID in decision.reason_codes


def test_unknown_profile_is_rejected() -> None:
    tasks = [
        CandidateTask("a", "one", (), "ghost_worker"),
        CandidateTask("b", "two", (), "coding_worker"),
    ]
    decision = route_delegation(
        _proposal(tasks, [_coverage(tasks)]), capabilities_from_catalog(WorkerCatalog())
    )
    assert decision.route is RouteKind.DIRECT
    assert REASON_UNKNOWN_PROFILE in decision.reason_codes


def test_missing_coverage_evidence_is_not_proven() -> None:
    tasks, evidence = _read_only_pair()
    evidence = [item for item in evidence if item.kind is not EvidenceKind.GOAL_COVERAGE]
    decision = route_delegation(
        _proposal(tasks, evidence), capabilities_from_catalog(WorkerCatalog())
    )
    assert decision.route is RouteKind.DIRECT
    assert REASON_UNCOVERED_GOAL in decision.reason_codes


def test_planner_claim_cannot_unlock_parallel() -> None:
    tasks, evidence = _write_pair(interface=("a",))
    for task in tasks:
        evidence.append(
            EvidenceItem(
                evidence_id=f"claim:{task.task_id}",
                kind=EvidenceKind.INTERFACE_CONTRACT,
                subject_task_ids=(task.task_id,),
                value="frozen",
                source_kind=SourceKind.PLANNER_CLAIM,
                source_locator="planner",
                status=EvidenceStatus.PROVEN,
            )
        )
    decision = route_delegation(
        _proposal(tasks, evidence), capabilities_from_catalog(WorkerCatalog())
    )
    assert decision.route is RouteKind.DIRECT
    assert REASON_UNSTABLE_INTERFACE in decision.reason_codes


def test_unknown_risk_blocks_parallel() -> None:
    tasks, evidence = _read_only_pair()
    evidence = [item for item in evidence if item.kind is not EvidenceKind.RISK_CLASS]
    decision = route_delegation(
        _proposal(tasks, evidence), capabilities_from_catalog(WorkerCatalog())
    )
    assert decision.route is RouteKind.DIRECT
    assert any(
        code in decision.reason_codes for code in ("UNKNOWN_RISK", "SEMANTIC_COUPLING_UNKNOWN")
    )


def test_disproven_evidence_is_ignored() -> None:
    tasks, evidence = _read_only_pair()
    evidence = [replace(item, status=EvidenceStatus.DISPROVEN) for item in evidence]
    decision = route_delegation(
        _proposal(tasks, evidence), capabilities_from_catalog(WorkerCatalog())
    )
    assert decision.route is RouteKind.DIRECT


def test_parallel_limit_is_enforced() -> None:
    tasks = [
        CandidateTask("a", "Implement a.py.", (), "coding_worker"),
        CandidateTask("b", "Implement b.py.", (), "coding_worker"),
        CandidateTask("c", "Implement c.py.", (), "coding_worker"),
    ]
    evidence = []
    for task in tasks:
        evidence.extend(
            [
                _write(task.task_id, (f"{task.task_id}.py",)),
                _deliverable(task.task_id, (f"{task.task_id}.py",)),
                _risk(task.task_id, WorkRisk.LOCAL_REVERSIBLE_WRITE),
                _context(task.task_id),
            ]
        )
    evidence.append(_coverage(tasks))
    strict = route_delegation(
        _proposal(tasks, evidence), capabilities_from_catalog(WorkerCatalog())
    )
    assert strict.route is RouteKind.DIRECT
    assert REASON_PARALLEL_LIMIT_EXCEEDED in strict.reason_codes

    wider = RoutingPolicy(max_parallel_workers=3)
    admitted = route_delegation(
        _proposal(tasks, evidence), capabilities_from_catalog(WorkerCatalog()), wider
    )
    assert admitted.route is RouteKind.DELEGATE_PARALLEL


def test_single_read_only_task_can_be_delegated_once() -> None:
    tasks = [CandidateTask("a", "Inspect module X and report findings.", (), "research_worker")]
    evidence = [
        _risk("a", WorkRisk.READ_ONLY),
        _deliverable("a", ("report-x.md",)),
        _context("a"),
        _coverage(tasks),
    ]
    decision = route_delegation(
        _proposal(tasks, evidence), capabilities_from_catalog(WorkerCatalog())
    )
    assert decision.route is RouteKind.DELEGATE_ONE


def test_proposal_and_tasks_are_not_mutated() -> None:
    tasks, evidence = _read_only_pair()
    proposal = _proposal(tasks, evidence)
    before = (tasks, tuple(evidence), proposal.candidate_tasks)
    route_delegation(proposal, capabilities_from_catalog(WorkerCatalog()))
    assert (tasks, tuple(evidence), proposal.candidate_tasks) == before


def test_decision_reports_why_nothing_was_delegated() -> None:
    tasks, evidence = _write_pair(shared=True)
    decision = route_delegation(
        _proposal(tasks, evidence), capabilities_from_catalog(WorkerCatalog())
    )
    coupling = next(gate for gate in decision.gates if gate.gate.endswith("COUPLING"))
    assert coupling.status.value == "block"
    assert REASON_SHARED_WRITE_RISK in coupling.reason_codes
    assert coupling.evidence_refs  # every blocker points at evidence


def test_empty_proposal_is_invalid() -> None:
    decision = route_delegation(_proposal([], []), capabilities_from_catalog(WorkerCatalog()))
    assert decision.route is RouteKind.DIRECT
    assert decision.reason_codes == ("INVALID_DAG",)


# --------------------------------------------------------------------------- capabilities


def test_capabilities_align_with_the_worker_catalog() -> None:
    catalog = WorkerCatalog()
    capabilities = capabilities_from_catalog(catalog)
    assert set(capabilities.profile_ids()) == set(catalog.profiles)


def test_read_only_profiles_are_marked_parallel_safe_but_not_write_capable() -> None:
    capabilities = capabilities_from_catalog(WorkerCatalog())
    research = capabilities.resolve("research_worker")
    coding = capabilities.resolve("coding_worker")
    assert research is not None and coding is not None
    assert "module_implementation" in research.avoid_work_types
    assert "module_implementation" in coding.preferred_work_types
    assert research.cost_tier == "cheap"
    assert coding.cost_tier == "standard"


def test_capability_catalog_rejects_unknown_profile() -> None:
    catalog: CapabilityCatalog = capabilities_from_catalog(WorkerCatalog())
    assert catalog.resolve("ghost_worker") is None
    assert catalog.resolve(None) is None


# --------------------------------------------------------------------------- text extraction


@pytest.mark.parametrize(
    ("text", "owned", "excluded"),
    [
        ("Implement report/parser.py.", ("report/parser.py",), ()),
        (
            "Read formatter.py then implement exporter.py using format_row.",
            ("exporter.py",),
            (),
        ),
        (
            'Write /workspace/project/state.json with exactly {"a": 1}. Do not create final.json.',
            ("state.json",),
            ("final.json",),
        ),
        ("Inspect module X and report findings.", (), ()),
    ],
)
def test_declared_path_extraction(
    text: str, owned: tuple[str, ...], excluded: tuple[str, ...]
) -> None:
    assert extract_declared_paths(text) == (owned, excluded)
