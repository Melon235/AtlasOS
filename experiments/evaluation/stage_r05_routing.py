"""Stage R0.5: offline route audit for the positive and negative control families.

No model, no sandbox, no worker: this module only builds a :class:`DelegationProposal` from public
benchmark metadata and calls the *same* :func:`route_delegation` the live runner calls. Positive and
negative families go through one adapter and one router; there is no branch on family, sample id or
expected outcome anywhere in the decision path.

The expected route of every family was frozen before the first live call (see
``stage_r05_tasks.preregistration``). This module reports the difference, it never edits it.
"""

from __future__ import annotations

import json
from typing import Any

from experiments.evaluation.stage_r0_routing import build_proposal, routing_policy
from experiments.evaluation.stage_r05_tasks import DEFINITIONS, NEGATIVE_IDS, POSITIVE_IDS, variants

from atlasos.workers import WorkerCatalog
from atlasos.workers.routing import (
    EvidenceKind,
    RouteDecision,
    capabilities_from_catalog,
    decision_digest,
    max_wave_width,
    route_delegation,
)

EVIDENCE_GAP_REASONS = frozenset(
    {
        "UNSTABLE_INTERFACE",
        "CONTEXT_INSUFFICIENT",
        "SEMANTIC_COUPLING_UNKNOWN",
        "UNCOVERED_GOAL",
        "INSUFFICIENT_EVIDENCE",
        "UNKNOWN_RISK",
    }
)


def _evidence_status_counts(evidence: tuple[Any, ...]) -> dict[str, int]:
    counts = {"proven": 0, "unknown": 0, "disproven": 0}
    for item in evidence:
        counts[item.status.value] += 1
    return counts


def audit_definition(definition: Any, policy: Any | None = None) -> dict[str, Any]:
    task = variants(definition)[1]
    proposal = build_proposal(task, task.workers, proposal_id=f"{definition.sample_id}:candidate")
    decision: RouteDecision = route_delegation(
        proposal, capabilities_from_catalog(WorkerCatalog()), policy or routing_policy()
    )
    kinds = {kind.value for kind in (item.kind for item in proposal.evidence)}
    contract_items = [
        item for item in proposal.evidence if item.kind is EvidenceKind.INTERFACE_CONTRACT
    ]
    return {
        "sample_id": definition.sample_id,
        "family": definition.family,
        "expected_route": definition.expected_route,
        "expected_reason": definition.expected_reason,
        "actual_route": decision.route.value,
        "reason_codes": list(decision.reason_codes),
        "blocked_gates": list(decision.blocked_gates),
        "wave_width": max_wave_width(proposal.candidate_tasks),
        "task_count": len(proposal.candidate_tasks),
        "approved_task_ids": list(decision.approved_task_ids),
        "policy_version": decision.policy_version,
        "policy_digest": decision.policy_digest,
        "decision_digest": decision_digest(decision),
        "matches_expectation": decision.route.value == definition.expected_route
        and definition.expected_reason in decision.reason_codes,
        "dag_edges": [
            f"{dependency}->{step.task_id}"
            for step in task.workers
            for dependency in step.depends_on
        ],
        "evidence_kinds": sorted(kinds),
        "evidence_status": _evidence_status_counts(proposal.evidence),
        "contracts": [
            {
                "task_id": item.subject_task_ids[0],
                "status": item.status.value,
                "value": item.value,
            }
            for item in contract_items
        ],
        "decision": decision.as_dict(),
    }


def audit_all(policy: Any | None = None) -> dict[str, Any]:
    rows = [audit_definition(definition, policy) for definition in DEFINITIONS]
    summary = {
        "policy_version": (rows[0]["policy_version"] if rows else None),
        "policy_digest": (rows[0]["policy_digest"] if rows else None),
        "routes": {},
        "positive_admitted": 0,
        "positive_rejected": 0,
        "negative_admitted": 0,
        "negative_rejected": 0,
        "unknown_evidence_count": 0,
        "mismatches": [],
    }
    for row in rows:
        summary["routes"][row["sample_id"]] = row["actual_route"]
        positive = row["family"].startswith("P")
        if positive:
            if row["actual_route"] == "delegate_parallel":
                summary["positive_admitted"] += 1
            else:
                summary["positive_rejected"] += 1
        else:
            if row["actual_route"] == "delegate_parallel":
                summary["negative_admitted"] += 1
            else:
                summary["negative_rejected"] += 1
        if EVIDENCE_GAP_REASONS.intersection(row["reason_codes"]):
            summary["unknown_evidence_count"] += 1
        if not row["matches_expectation"]:
            summary["mismatches"].append(
                {
                    "sample_id": row["sample_id"],
                    "expected": [row["expected_route"], row["expected_reason"]],
                    "actual": [row["actual_route"], row["reason_codes"]],
                }
            )
    return {
        "rows": rows,
        "summary": summary,
        "positive_ids": list(POSITIVE_IDS),
        "negative_ids": list(NEGATIVE_IDS),
    }


def width_sensitivity_audit() -> dict[str, Any]:
    """Track B: the two Stage E1 plans, routed by r0.1.0 and r0.2.0 with the unchanged adapter."""

    from experiments.evaluation.stage_e_tasks import DEFINITIONS as E1_DEFINITIONS
    from experiments.evaluation.stage_e_tasks import variants as e1_variants

    rows: list[dict[str, Any]] = []
    catalog = capabilities_from_catalog(WorkerCatalog())
    for definition in E1_DEFINITIONS:
        if definition.sample_id not in {"e1-01-config", "e1-03-rbac"}:
            continue
        task = e1_variants(definition)[1]
        proposal = build_proposal(
            task, task.workers, proposal_id=f"{definition.sample_id}:candidate"
        )
        entry: dict[str, Any] = {
            "sample_id": definition.sample_id,
            "wave_width": max_wave_width(proposal.candidate_tasks),
            "dag_edges": [
                f"{dependency}->{step.task_id}"
                for step in task.workers
                for dependency in step.depends_on
            ],
            "candidate_task_ids": [step.task_id for step in task.workers],
            "interface_evidence_tasks": sorted(
                {
                    item.subject_task_ids[0]
                    for item in proposal.evidence
                    if item.kind is EvidenceKind.INTERFACE_SYMBOLS
                }
            ),
            "routes": {},
        }
        for version in ("r0.1.0", "r0.2.0"):
            decision = route_delegation(proposal, catalog, routing_policy(version))
            entry["routes"][version] = {
                "route": decision.route.value,
                "reasons": list(decision.reason_codes),
                "diagnostics": list(decision.diagnostics),
                "policy_version": decision.policy_version,
                "policy_digest": decision.policy_digest,
                "decision_digest": decision_digest(decision),
                "approved_task_ids": list(decision.approved_task_ids),
            }
        rows.append(entry)
    return {"rows": rows}


def main() -> int:
    payload = {
        "audit": audit_all(),
        "width_sensitivity": width_sensitivity_audit(),
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
