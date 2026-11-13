"""Final-4 supplement: run the new offline workloads and emit machine-readable results.

Writes, under ``experiments/results/final4/``:

* ``module_supplement.json`` — the Final-3 rows plus evidence levels and Final-4 updates
* ``pilot_results.json`` / ``.csv`` — the planned live matrix, each cell with its real status
* ``router_comparison.json`` — the Router comparison frame, with no fabricated numbers

Live cells are emitted as ``NOT_RUN`` with the blocking reason. Nothing here invents a model result.
"""

from __future__ import annotations

import csv
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from experiments.evaluation.final4 import tool_search_scale
from experiments.evaluation.final4.context_topologies import run as run_topologies
from experiments.evaluation.neutral_env import adapter_status, capability_report

REPO_ROOT = Path(__file__).resolve().parents[3]
FINAL3 = REPO_ROOT / "experiments" / "results" / "final3" / "module_acceptance.json"
OUT = REPO_ROOT / "experiments" / "results" / "final4"

LIVE_BLOCKER = "LIVE_PROVIDER_BLOCKED: no DEEPSEEK_API_KEY in this workspace"
SANDBOX_BLOCKER = "SANDBOX_BLOCKED: opensandbox raises SandboxCredentialError"


def _final3_rows() -> list[dict[str, Any]]:
    payload = json.loads(FINAL3.read_text(encoding="utf-8"))
    return list(payload["modules"])


def _levels(
    module: dict[str, Any], topologies: dict[str, Any], scale: dict[str, Any]
) -> dict[str, Any]:
    """Evidence level, epoch and the three result columns for one module."""

    module_id = module["module_id"]
    final3_measured = module["evidence_path"].startswith("experiments/results/final3/")
    updated: dict[str, Any] = {
        "functional_evidence_level": "E1_REGRESSION",
        "benefit_evidence_level": "NONE",
        "evidence_epoch": "final3@93cbd98",
        "baseline_type": "none",
        "current_independent_result": None,
        "historical_result": None,
        "live_result": None,
    }
    if final3_measured:
        updated["benefit_evidence_level"] = "E3_CONTROLLED_BENEFIT"
        updated["evidence_epoch"] = "final3@93cbd98"
        updated["baseline_type"] = "ablation"
        updated["current_independent_result"] = {
            "primary_metric": module["primary_metric"],
            "baseline": module["baseline_value"],
            "treatment": module["treatment_value"],
            "workload_id": module["workload_id"],
        }
    if module_id in {"A3-09", "A4-18", "A5-01", "A5-03", "A5-08", "A5-09"}:
        updated["historical_result"] = module["limitations"]
        updated["baseline_type"] = "historical"
    if module_id in {"A1-03", "A1-04"}:
        updated.update(
            {
                "functional_evidence_level": "E2_INDEPENDENT_FUNCTIONAL",
                "benefit_evidence_level": "E3_CONTROLLED_BENEFIT",
                "evidence_epoch": "final4@offline",
                "baseline_type": "ablation",
                "current_independent_result": {
                    "workload_id": "final4-topologies-5",
                    "dependency_coverage": topologies["dependency_coverage"],
                    "irrelevant_injected": topologies["irrelevant_injected_total"],
                    "stable_equality_cells": topologies["stable_equality_cells"],
                    "naive_equality_cells": topologies["naive_equality_cells"],
                    "cells": topologies["cells_total"],
                },
            }
        )
    if module_id == "A1-05":
        updated.update(
            {
                "functional_evidence_level": "E2_INDEPENDENT_FUNCTIONAL",
                "benefit_evidence_level": "E3_CONTROLLED_BENEFIT",
                "evidence_epoch": "final4@e2",
                "baseline_type": "identity_transform",
                "current_independent_result": {
                    "workload_id": "final4-d0-d3",
                    "multiline_char_reduction": 0.3184,
                    "singleline_char_reduction": 0.0,
                    "classification": "PAYLOAD_DEPENDENT_BENEFIT",
                },
            }
        )
    if module_id in {"A2-04", "A2-05"}:
        updated.update(
            {
                "functional_evidence_level": "E2_INDEPENDENT_FUNCTIONAL",
                "benefit_evidence_level": "E3_CONTROLLED_BENEFIT",
                "evidence_epoch": "final4@offline",
                "baseline_type": "keyword_backend",
                "current_independent_result": {
                    "workload_id": "final4-tool-scale-64-128",
                    "cells": [
                        {
                            "catalog_size": cell["catalog_size"],
                            "keyword_recall_at_1": cell["keyword"]["recall_at_1"],
                            "semantic_recall_at_1": cell["semantic"]["recall_at_1"],
                            "semantic_recall_at_5": cell["semantic"]["recall_at_5"],
                            "keyword_mrr": cell["keyword"]["mrr"],
                            "semantic_mrr": cell["semantic"]["mrr"],
                        }
                        for cell in scale["cells"]
                    ],
                    "query_digest": scale["query_digest"],
                },
            }
        )
    return updated


def _pilot_rows() -> list[dict[str, Any]]:
    """The planned matrix, every cell carrying its real (blocked) status."""

    rows: list[dict[str, Any]] = []
    tasks = [
        ("V-S", "validation-serial", "serial dependency"),
        ("V-P", "validation-parallel", "naturally parallel"),
        ("V-C", "validation-coupled", "strongly coupled"),
    ]
    for family, task_id, shape in tasks:
        for configuration in ("N0", "R0", "R1", "R2"):
            blocker = SANDBOX_BLOCKER if configuration == "N0" else LIVE_BLOCKER
            rows.append(
                {
                    "run_id": f"final4-{task_id}-{configuration}",
                    "task_id": task_id,
                    "task_family": family,
                    "task_shape": shape,
                    "configuration": configuration,
                    "runtime_sha": "93cbd98f9407e7358e997878238520dcac15827a",
                    "evaluation_sha": None,
                    "status": "NOT_RUN",
                    "blocked_reason": blocker,
                    "routing_decision": None,
                    "actual_worker_count": None,
                    "oracle_pass": None,
                    "prompt_tokens": None,
                    "cost": None,
                    "failure_class": "INFRASTRUCTURE_BLOCKED",
                }
            )
    for configuration in ("X0", "X1"):
        for task_id in ("validation-serial", "validation-parallel"):
            rows.append(
                {
                    "run_id": f"final4-{task_id}-{configuration}",
                    "task_id": task_id,
                    "task_family": "V-S" if "serial" in task_id else "V-P",
                    "task_shape": "context ablation",
                    "configuration": configuration,
                    "runtime_sha": "93cbd98f9407e7358e997878238520dcac15827a",
                    "evaluation_sha": None,
                    "status": "NOT_RUN",
                    "blocked_reason": LIVE_BLOCKER,
                    "routing_decision": None,
                    "actual_worker_count": None,
                    "oracle_pass": None,
                    "prompt_tokens": None,
                    "cost": None,
                    "failure_class": "INFRASTRUCTURE_BLOCKED",
                }
            )
    for configuration, purpose in (
        ("FULL_EXPOSURE", "tool disclosure ablation"),
        ("PROGRESSIVE", "tool disclosure ablation"),
    ):
        rows.append(
            {
                "run_id": f"final4-tool-disclosure-{configuration.lower()}",
                "task_id": "validation-parallel",
                "task_family": "V-P",
                "task_shape": purpose,
                "configuration": configuration,
                "runtime_sha": "93cbd98f9407e7358e997878238520dcac15827a",
                "evaluation_sha": None,
                "status": "NOT_RUN",
                "blocked_reason": LIVE_BLOCKER,
                "routing_decision": None,
                "actual_worker_count": None,
                "oracle_pass": None,
                "prompt_tokens": None,
                "cost": None,
                "failure_class": "INFRASTRUCTURE_BLOCKED",
            }
        )
    return rows


def _router_frame(pilot_rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_task: dict[str, dict[str, Any]] = {}
    for row in pilot_rows:
        by_task.setdefault(row["task_id"], {})[row["configuration"]] = row["status"]
    return {
        "router_policy_version": "r0.1.0 (read from source; not modified)",
        "candidate_plan_source": "externally provided, frozen before any run",
        "mechanism_checks": {
            "direct_replaces_plan_with_single_worker": "verified: runner.py:847 is_direct",
            "delegate_parallel_runs_candidate_dag": "verified in runner.py:860-862",
            "delegate_one_has_no_distinct_execution_semantics": "verified: is_direct only",
            "router_reads_oracle_data": False,
            "router_wired_into_app_coding": False,
        },
        "observations": {
            "delegation_rate": None,
            "actual_multi_execution_rate": None,
            "harmful_delegation": None,
            "beneficial_delegation": None,
            "avoided_delegation": None,
            "routing_regret": None,
        },
        "cells": by_task,
        "status": "NOT_RUN_BLOCKED",
        "note": (
            "no live run happened, so no Router observation exists; the mechanism statements above "
            "come from source reading, not from execution"
        ),
    }


def main() -> int:
    topologies = run_topologies()
    scale = tool_search_scale.run()
    OUT.mkdir(parents=True, exist_ok=True)

    modules = []
    for module in _final3_rows():
        augmented = dict(module)
        augmented.update(_levels(module, topologies, scale))
        modules.append(augmented)

    supplement = {
        "generated_at": datetime.now(UTC).isoformat(),
        "runtime_sha": "93cbd98f9407e7358e997878238520dcac15827a",
        "evidence_levels": {
            "functional": [
                "E1_REGRESSION",
                "E2_INDEPENDENT_FUNCTIONAL",
                "E3_CONTROLLED_BENEFIT",
                "E4_LIVE_INTEGRATED",
            ],
            "note": "levels are labels, not a monotone score",
        },
        "modules": modules,
        "new_studies": {
            "context_topologies": topologies,
            "tool_search_scale": scale,
            "contextpilot_diagnosis": "experiments/results/final4/contextpilot_diagnosis.json",
            "neutral_adapter": {
                "environment": capability_report().status,
                "adapter": adapter_status().status,
                "missing": list(adapter_status().missing),
            },
        },
    }
    (OUT / "module_supplement.json").write_text(
        json.dumps(supplement, indent=2, sort_keys=True), encoding="utf-8"
    )

    rows = _pilot_rows()
    (OUT / "pilot_results.json").write_text(
        json.dumps(
            {
                "generated_at": datetime.now(UTC).isoformat(),
                "runtime_sha": supplement["runtime_sha"],
                "live_blocker": LIVE_BLOCKER,
                "sandbox_blocker": SANDBOX_BLOCKER,
                "runs": rows,
                "summary": {
                    "planned": len(rows),
                    "executed": 0,
                    "blocked": len(rows),
                },
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    with (OUT / "pilot_results.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        for row in rows:
            writer.writerow(row)

    (OUT / "router_comparison.json").write_text(
        json.dumps(_router_frame(rows), indent=2, sort_keys=True), encoding="utf-8"
    )

    print(f"modules={len(modules)} pilot_rows={len(rows)} written={OUT}")
    print("tool_scale:", json.dumps([cell["catalog_size"] for cell in scale["cells"]]))
    print("topology_coverage:", topologies["dependency_coverage"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
