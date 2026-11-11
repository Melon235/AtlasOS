"""Stage R0.5: the frozen router admits positive families and refuses negative ones.

The point of this file is *not* to re-test the Stage R0 gates. It pins three things:

* ``r0.1.0`` is unchanged (same digest, same decisions on the Stage E1 plans),
* ``r0.2.0`` differs from ``r0.1.0`` in exactly one field,
* the new positive families are admitted for evidence-based reasons, and the negatives are refused
  by the same router with the same adapter, without any branch on family or sample id.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from experiments.evaluation.models import BenchmarkTask, WorkerStep
from experiments.evaluation.stage_r0_routing import (
    build_proposal,
    plan_route,
    routing_policy,
)
from experiments.evaluation.stage_r05_tasks import DEFINITIONS, definition_by_id, variants

from atlasos.workers import WorkerCatalog
from atlasos.workers.routing import (
    REASON_DELEGATION_ADMITTED,
    REASON_NO_PARALLEL_WAVE,
    REASON_PARALLEL_LIMIT_EXCEEDED,
    REASON_SHARED_WRITE_RISK,
    REASON_UNSTABLE_INTERFACE,
    DelegationProposal,
    EvidenceItem,
    EvidenceKind,
    EvidenceStatus,
    RouteDecision,
    RouteKind,
    SourceKind,
    capabilities_from_catalog,
    decision_digest,
    route_delegation,
)

CAPABILITIES = capabilities_from_catalog(WorkerCatalog())
R0_1_0_DIGEST = "5e13c9106f8927fc116c00ff3d4ee91e9848205d0623925521ad6af12fa99244"
R0_2_0_DIGEST = "c5969f615fcf56d77a03b89a9073d2d0524be8aa281a6948c6e560f00331fcba"
E1_FIXTURE_DIGEST = "278a857e187e9fcf20af2e2078494df1530ad035efd289368c0b3d9af4542236"


def _route(sample_id: str, version: str = "r0.1.0") -> RouteDecision:
    task = variants(definition_by_id(sample_id))[1]
    proposal = build_proposal(task, task.workers, proposal_id=f"{sample_id}:candidate")
    return route_delegation(proposal, CAPABILITIES, routing_policy(version))


def _proposal(sample_id: str) -> DelegationProposal:
    task = variants(definition_by_id(sample_id))[1]
    return build_proposal(task, task.workers, proposal_id=f"{sample_id}:candidate")


# --------------------------------------------------------------------------- policy freeze


def test_r0_1_0_policy_is_unchanged() -> None:
    policy = routing_policy()
    assert policy.version == "r0.1.0"
    assert policy.max_parallel_workers == 2
    assert policy.digest() == R0_1_0_DIGEST


def test_r0_2_0_changes_only_the_parallel_width() -> None:
    narrow = routing_policy("r0.1.0")
    wide = routing_policy("r0.2.0")
    assert wide.digest() == R0_2_0_DIGEST
    fields = (
        "allow_shared_mutation",
        "allow_unknown_risk",
        "require_goal_coverage",
        "require_context_evidence",
        "min_tasks_for_delegation",
        "allow_single_read_only_delegation",
    )
    for field in fields:
        assert getattr(wide, field) == getattr(narrow, field)
    assert wide.max_parallel_workers == 3
    assert wide.version == "r0.2.0"


def test_unknown_policy_version_is_rejected() -> None:
    with pytest.raises(ValueError):
        routing_policy("r9.9.9")


def test_stage_e1_fixture_digest_is_unchanged() -> None:
    from experiments.evaluation.stage_e_tasks import DEFINITIONS as E1
    from experiments.evaluation.stage_e_tasks import variants as e1_variants

    assert e1_variants(E1[0])[1].fixture_digest == E1_FIXTURE_DIGEST


# --------------------------------------------------------------------------- admission


@pytest.mark.parametrize(
    "sample_id", ["r05-p1-analysis", "r05-p2-modules", "r05-p3-frozen-interface"]
)
def test_positive_families_are_admitted(sample_id: str) -> None:
    decision = _route(sample_id)
    expected = tuple(worker.task_id for worker in definition_by_id(sample_id).workers)
    assert decision.route is RouteKind.DELEGATE_PARALLEL
    assert REASON_DELEGATION_ADMITTED in decision.reason_codes
    assert decision.approved_task_ids == expected
    assert decision.policy_version == "r0.1.0"


@pytest.mark.parametrize(
    ("sample_id", "reason"),
    [
        ("r05-n1-shared-write", REASON_SHARED_WRITE_RISK),
        ("r05-n2-unfrozen-interface", REASON_UNSTABLE_INTERFACE),
        ("r05-n3-linear-chain", REASON_NO_PARALLEL_WAVE),
    ],
)
def test_negative_controls_are_refused(sample_id: str, reason: str) -> None:
    decision = _route(sample_id)
    assert decision.route is RouteKind.DIRECT
    assert reason in decision.reason_codes
    assert decision.approved_task_ids == ()


def test_every_family_matches_its_frozen_expectation() -> None:
    for definition in DEFINITIONS:
        decision = _route(definition.sample_id)
        assert decision.route.value == definition.expected_route
        assert definition.expected_reason in decision.reason_codes


def test_route_does_not_depend_on_the_sample_id() -> None:
    """Renaming a sample cannot change the decision: there is no id-based branch anywhere."""

    for definition in DEFINITIONS:
        a1 = variants(definition)[1]
        baseline = plan_route(a1, CAPABILITIES, routing_policy())
        renamed = replace(a1, sample_id="zzz-anonymous")
        other = plan_route(renamed, CAPABILITIES, routing_policy())
        assert other.decision.route is baseline.decision.route
        assert other.decision.reason_codes == baseline.decision.reason_codes
        assert other.decision.approved_task_ids == baseline.decision.approved_task_ids
        assert other.decision.policy_digest == baseline.decision.policy_digest
        assert decision_digest(other.decision) != decision_digest(baseline.decision)


# --------------------------------------------------------------------------- evidence trust


def test_frozen_contract_evidence_is_proven_only_when_the_file_is_staged() -> None:
    proposal = _proposal("r05-p3-frozen-interface")
    contracts = [item for item in proposal.evidence if item.kind is EvidenceKind.INTERFACE_CONTRACT]
    assert {item.subject_task_ids[0] for item in contracts} == {"clock", "summary"}
    assert all(item.status is EvidenceStatus.PROVEN for item in contracts)
    assert all(item.source_kind is SourceKind.TRUSTED_METADATA for item in contracts)

    failing = _proposal("r05-n2-unfrozen-interface")
    downgraded = [item for item in failing.evidence if item.kind is EvidenceKind.INTERFACE_CONTRACT]
    assert downgraded
    assert all(item.status is EvidenceStatus.UNKNOWN for item in downgraded)
    values = [item.value for item in downgraded]
    assert all(
        isinstance(value, dict) and value["missing"] == ["docs/INTERFACE.md"] for value in values
    )


def test_a_planner_claim_cannot_unlock_a_parallel_route() -> None:
    """A claim about the contract is not evidence, even when every task asks for it."""

    task = variants(definition_by_id("r05-p3-frozen-interface"))[1]
    proposal = build_proposal(task, task.workers, proposal_id="claim")
    stripped = tuple(
        item for item in proposal.evidence if item.kind is not EvidenceKind.INTERFACE_CONTRACT
    )
    claimed = tuple(
        EvidenceItem(
            evidence_id=f"claim:{task_id}",
            kind=EvidenceKind.INTERFACE_CONTRACT,
            subject_task_ids=(task_id,),
            value={"declared": ["docs/INTERFACE.md"]},
            source_kind=SourceKind.PLANNER_CLAIM,
            source_locator="planner:text",
        )
        for task_id in ("clock", "summary")
    )
    forged = replace(proposal, evidence=(*stripped, *claimed))
    decision = route_delegation(forged, CAPABILITIES, routing_policy())
    assert decision.route is RouteKind.DIRECT
    assert REASON_UNSTABLE_INTERFACE in decision.reason_codes


def test_removing_the_contract_downgrades_the_parallel_decision() -> None:
    task = variants(definition_by_id("r05-p3-frozen-interface"))[1]
    without = replace(task, frozen_contracts={})
    decision = plan_route(without, CAPABILITIES, routing_policy())
    assert decision.decision.route is RouteKind.DIRECT
    assert REASON_UNSTABLE_INTERFACE in decision.decision.reason_codes


# --------------------------------------------------------------------------- width


def test_width_sensitivity_only_affects_the_admission_limit() -> None:
    """Stage E1 plans that fail on width alone are admitted by r0.2.0, with the same evidence."""

    for sample_id in ("e1-01-config", "e1-03-rbac"):
        narrow = _e1_route(sample_id, "r0.1.0")
        wide = _e1_route(sample_id, "r0.2.0")
        assert narrow.route is RouteKind.DIRECT
        assert narrow.reason_codes == (REASON_PARALLEL_LIMIT_EXCEEDED,)
        assert wide.route is RouteKind.DELEGATE_PARALLEL
        assert wide.reason_codes == (REASON_DELEGATION_ADMITTED,)
        assert wide.max_parallel_wave_width == 3
        assert len(wide.approved_task_ids) == 3


def _e1_route(sample_id: str, version: str) -> RouteDecision:
    from experiments.evaluation.stage_e_tasks import DEFINITIONS as E1
    from experiments.evaluation.stage_e_tasks import variants as e1_variants

    definition = next(item for item in E1 if item.sample_id == sample_id)
    task = e1_variants(definition)[1]
    proposal = build_proposal(task, task.workers, proposal_id=f"{sample_id}:candidate")
    return route_delegation(proposal, CAPABILITIES, routing_policy(version))


# --------------------------------------------------------------------------- execution shape


def test_execution_shape_follows_the_decision() -> None:
    admitted = plan_route(
        variants(definition_by_id("r05-p2-modules"))[1], CAPABILITIES, routing_policy()
    )
    assert admitted.execution_mode == "a1"
    assert [step.task_id for step in admitted.steps] == ["entries", "money"]

    refused = plan_route(
        variants(definition_by_id("r05-n1-shared-write"))[1], CAPABILITIES, routing_policy()
    )
    assert refused.execution_mode == "a0"
    assert refused.is_direct


def test_adapter_does_not_rewrite_the_candidate_plan() -> None:
    task = variants(definition_by_id("r05-p2-modules"))[1]
    proposal = build_proposal(task, task.workers, proposal_id="shape")
    assert [item.description for item in proposal.candidate_tasks] == [
        step.prompt for step in task.workers
    ]
    assert [item.depends_on for item in proposal.candidate_tasks] == [
        step.depends_on for step in task.workers
    ]
    assert proposal.overall_goal == task.prompt


def test_single_task_plans_keep_the_stage_r0_behaviour() -> None:
    task = BenchmarkTask(
        sample_id="r05-single",
        category="multi_worker",
        mode="coding",
        prompt="Inspect one module.",
        target="report",
        visible_files={"a.py": ""},
        workers=(WorkerStep("solo", "r05-single-solo", "Inspect a.py and write report.md."),),
        allowed_paths=("report.md",),
        required_artifacts=("report.md",),
    )
    routed = plan_route(task, CAPABILITIES, routing_policy())
    assert routed.execution_mode == "a0"
    assert routed.diagnostics == ("single_candidate_task",)
