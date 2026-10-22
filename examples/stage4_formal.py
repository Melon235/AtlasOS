"""Stage 4 formal offline placement benchmark (deterministic, clean-worktree)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from atlasos.experiments import ExperimentStore, git_commit, git_dirty, package_version
from atlasos.placement import (
    CapabilityOnlyPolicy,
    ContextLocalityPolicy,
    PlacementWeights,
    StaticPlacementPolicy,
    placement_stress_workflow,
    plan_workflow,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

VARIANTS = (
    ("S0", "static_baseline", StaticPlacementPolicy, PlacementWeights()),
    ("S1", "capability_only", CapabilityOnlyPolicy, PlacementWeights(locality=0.0, load=0.2)),
    (
        "S2",
        "context_locality",
        ContextLocalityPolicy,
        PlacementWeights(locality=1.0, load=0.2),
    ),
)


class PlacementRunRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    timestamp: datetime
    variant: str
    placement_policy: str
    placement_weights: dict[str, float]
    workload_name: str

    git_commit: str | None
    git_dirty: bool
    anycode_version: str | None
    context_transform: str

    total_tasks: int
    dependency_agent_switches: int
    mean_assignment_locality: float
    total_logical_handoff_refs: int
    total_logical_handoff_chars: int
    assignment_distribution: dict[str, int]
    task_success: bool


def main() -> None:
    workflow = placement_stress_workflow()
    store = ExperimentStore(REPO_ROOT / "experiments" / "runs" / "stage4-formal.jsonl")
    for label, policy_name, policy_cls, weights in VARIANTS:
        plan = plan_workflow(workflow, policy=policy_cls(), weights=weights)
        summary = plan.summary
        store.append(
            PlacementRunRecord(
                run_id=uuid.uuid4().hex,
                timestamp=datetime.now(UTC),
                variant=label,
                placement_policy=policy_name,
                placement_weights={"locality": weights.locality, "load": weights.load},
                workload_name=workflow.name,
                git_commit=git_commit(REPO_ROOT),
                git_dirty=git_dirty(REPO_ROOT),
                anycode_version=package_version("anycode-py"),
                context_transform="identity",
                total_tasks=summary.total_tasks,
                dependency_agent_switches=summary.dependency_agent_switches,
                mean_assignment_locality=summary.mean_assignment_locality,
                total_logical_handoff_refs=summary.total_logical_handoff_refs,
                total_logical_handoff_chars=summary.total_logical_handoff_chars,
                assignment_distribution=dict(summary.assignment_distribution),
                task_success=True,
            )
        )
        print(
            f"{label} {policy_name}: switches={summary.dependency_agent_switches} "
            f"locality={summary.mean_assignment_locality:.3f} "
            f"handoff_refs={summary.total_logical_handoff_refs} "
            f"distribution={dict(summary.assignment_distribution)} "
            f"dirty={git_dirty(REPO_ROOT)}"
        )


if __name__ == "__main__":
    main()
