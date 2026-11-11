"""R1-2A observation-mode round: ``off`` vs ``observe`` under identical conditions.

The two arms differ in exactly one setting. The verifier adds findings and metrics; it never touches
the worker prompt, the tool surface, the budget, the workspace or the oracle verdict.

Run with ``python -m experiments.evaluation.stage_r1_2_rounds --repetitions 1``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from experiments.evaluation.runner import REPO_ROOT, RunnerSettings, run_sample
from experiments.evaluation.stage_r05_tasks import definition_by_id, variants

RESULTS_ROOT = REPO_ROOT / "experiments" / "results" / "stage-r1-2"
ARMS = ("v0", "obs")
ARM_CONDITIONS = {"v0": {"verification_mode": "off"}, "obs": {"verification_mode": "observe"}}
DEFAULT_DEFINITIONS = ("r05-p2-modules", "r05-p3-frozen-interface")

FIXED: dict[str, Any] = {
    "benchmark_id": "atlasruntime-stage-r1-2",
    "dataset_version": "1.0.0",
    "scorer_version": "1.0.0",
    "config_path": REPO_ROOT / "experiments" / "configs" / "eval-benchmark.yaml",
    "temperature": 0.0,
    "max_turns": 14,
    "token_budget": 120_000,
    "budget_mode": "guarded",
    "max_tool_calls": 60,
    "handoff_mode": "dual",
    "tool_error_mode": "corrected",
    "tool_result_mode": "off",
    "compaction_mode": "off",
    "worker_contract_mode": "legacy",
    "ownership_mode": "off",
    "execution_integrity_mode": "record",
}


def settings_for(arm: str) -> RunnerSettings:
    return replace(RunnerSettings(**FIXED), **ARM_CONDITIONS[arm])


def _row(result: Any, arm: str) -> dict[str, Any]:
    verification = (result.metrics.extra or {}).get("verification") or {}
    return {
        "arm": arm,
        "sample_id": result.task.sample_id,
        "success": result.success,
        "oracle_pass": result.oracle.passed,
        "failure_class": result.failure_class,
        "failure_reason": (result.failure_reason or "")[:400],
        "model_turns": result.metrics.model_turns,
        "tool_calls": result.metrics.tool_calls,
        "prompt_tokens": result.metrics.prompt_tokens,
        "completion_tokens": result.metrics.completion_tokens,
        "cost_usd": result.metrics.estimated_cost_usd,
        "latency_ms": result.metrics.latency_ms,
        "worker_success": {
            name: bool((value or {}).get("success"))
            for name, value in (result.worker_results or {}).items()
        },
        "verification_overall": verification.get("verification_overall"),
        "deliverable_presence": verification.get("deliverable_presence"),
        "structural_validity": verification.get("structural_validity"),
        "public_contract_status": verification.get("public_contract_status"),
        "integration_status": verification.get("integration_status"),
        "spec_digest": verification.get("spec_digest"),
        "artifact_digest": verification.get("artifact_digest"),
        "verification_files": sorted(
            path.name for path in Path(result.run_dir).glob("verification*.json")
        ),
        "run_dir": result.run_dir,
    }


async def run_round(
    definitions: tuple[str, ...], *, repetitions: int, stamp: str | None = None
) -> dict[str, Any]:
    stamp = stamp or time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    round_dir = RESULTS_ROOT / f"r12obs-{stamp}"
    pairs: list[dict[str, Any]] = []
    for sample in definitions:
        definition = definition_by_id(sample)
        for repetition in range(repetitions):
            for task in variants(definition):
                runs: dict[str, Any] = {}
                for arm in ARMS:
                    started = time.perf_counter()
                    result = await run_sample(
                        task, settings=settings_for(arm), repetition_id=repetition
                    )
                    runs[arm] = _row(result, arm)
                    runs[arm]["round_wall_ms"] = round((time.perf_counter() - started) * 1000.0, 3)
                    print(
                        f"{round_dir.name} {sample} rep={repetition} {arm} "
                        f"oracle={result.oracle.passed} cls={result.failure_class} "
                        f"verification={runs[arm]['verification_overall']}",
                        flush=True,
                    )
                pairs.append({"definition": sample, "repetition": repetition, "runs": runs})
    summary = _summary(pairs)
    payload = {
        "stage": "r1-2a",
        "suite": "r1-2-observation",
        "round_id": round_dir.name,
        "started_at": stamp,
        "repetitions": repetitions,
        "arms": list(ARMS),
        "arm_conditions": ARM_CONDITIONS,
        "fixed_conditions": {
            key: (str(value) if isinstance(value, Path) else value) for key, value in FIXED.items()
        },
        "pairs": pairs,
        "summary": summary,
    }
    round_dir.mkdir(parents=True, exist_ok=True)
    (round_dir / "manifest.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))
    print(f"round written to {round_dir}")
    return payload


def _summary(pairs: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {"runs": sum(len(pair["runs"]) for pair in pairs)}
    for arm in ARMS:
        rows = [pair["runs"][arm] for pair in pairs if arm in pair["runs"]]
        if not rows:
            continue
        summary[f"{arm}_oracle_pass"] = sum(1 for row in rows if row["oracle_pass"])
        summary[f"{arm}_verification"] = sorted({str(row["verification_overall"]) for row in rows})
        summary[f"{arm}_model_turns"] = [row["model_turns"] for row in rows]
        summary[f"{arm}_tool_calls"] = [row["tool_calls"] for row in rows]
        summary[f"{arm}_cost_usd"] = round(sum(row["cost_usd"] or 0.0 for row in rows), 6)
        summary[f"{arm}_latency_ms"] = [round(row["latency_ms"] or 0.0, 1) for row in rows]
        summary[f"{arm}_verification_files"] = sorted(
            {tuple(row["verification_files"]) for row in rows}
        )
    return summary


def main(argv: list[str] | None = None) -> dict[str, Any]:
    parser = argparse.ArgumentParser(description="R1-2A observation round")
    parser.add_argument("--definitions", default=",".join(DEFAULT_DEFINITIONS))
    parser.add_argument("--repetitions", type=int, default=1)
    args = parser.parse_args(argv)
    definitions = tuple(item.strip() for item in args.definitions.split(",") if item.strip())
    return asyncio.run(run_round(definitions, repetitions=args.repetitions))


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    main()
