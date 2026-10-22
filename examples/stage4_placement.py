"""Stage 4 offline placement benchmark: S0 static baseline, S1 capability, S2 locality.

    uv run python examples/stage4_placement.py

No LLM, no network, no sleeps: planning is deterministic.
"""

from __future__ import annotations

import json

from atlasos.placement import (
    CapabilityOnlyPolicy,
    ContextLocalityPolicy,
    PlacementWeights,
    StaticPlacementPolicy,
    placement_stress_workflow,
    plan_workflow,
)


def main() -> None:
    workflow = placement_stress_workflow()
    variants = (
        ("S0-static", StaticPlacementPolicy(), PlacementWeights()),
        ("S1-capability", CapabilityOnlyPolicy(), PlacementWeights(locality=0.0, load=0.2)),
        ("S2-locality", ContextLocalityPolicy(), PlacementWeights(locality=1.0, load=0.2)),
    )
    rows = []
    for label, policy, weights in variants:
        plan = plan_workflow(workflow, policy=policy, weights=weights)
        summary = plan.summary
        row = {
            "variant": label,
            "policy": summary.policy_name,
            "tasks": summary.total_tasks,
            "dependency_agent_switches": summary.dependency_agent_switches,
            "mean_assignment_locality": round(summary.mean_assignment_locality, 4),
            "total_logical_handoff_refs": summary.total_logical_handoff_refs,
            "assignment_distribution": dict(summary.assignment_distribution),
        }
        rows.append(row)
        print(
            f"{label:<14} switches={row['dependency_agent_switches']} "
            f"locality={row['mean_assignment_locality']:.3f} "
            f"handoff_refs={row['total_logical_handoff_refs']} "
            f"distribution={row['assignment_distribution']}"
        )
        if label == "S2-locality":
            for decision in plan.decisions:
                if decision.required_context_count:
                    print(
                        f"   {decision.task_id}->{decision.agent_id} "
                        f"locality={decision.context_locality:.2f} "
                        f"handoff={decision.logical_handoff_refs}"
                    )
    print(json.dumps(rows, indent=2))


if __name__ == "__main__":
    main()
