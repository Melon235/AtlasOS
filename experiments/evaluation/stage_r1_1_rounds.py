"""Stage R1-1 controlled comparison: ownership off versus ownership enforced.

Two arms over the same frozen tasks, repetitions and conditions:

``O0``  ``ownership_mode=off``      ``execution_integrity_mode=record``
``O1``  ``ownership_mode=enforce``  ``execution_integrity_mode=record``

The integrity mode is held constant so the only difference between arms is the ownership policy.
Fixtures, oracles, worker prompts, the DAG, the budget and the scheduler are untouched.

The live model is not guaranteed to repeat the historical ``git checkout --`` rollback, so a run
without denials proves *compatibility*, not protection. Interception is proven deterministically by
``tests/unit/test_r1_1_ownership_policy.py``; this round measures cost and behavioural impact.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from experiments.evaluation.runner import RunnerSettings, run_sample
from experiments.evaluation.stage_r05_tasks import definition_by_id, variants

from atlasos.experiments import git_commit, git_dirty

REPO_ROOT = Path(__file__).resolve().parents[2]
RESULTS_ROOT = REPO_ROOT / "experiments" / "results" / "stage-r1-1"

ARMS = ("o0", "o1")
DEFAULT_TASKS = ("r05-p2-modules", "r05-p3-frozen-interface")

#: Frozen conditions. Every value is identical to Stage R0.5 except the two R1-1 switches.
FIXED: dict[str, Any] = {
    "benchmark_id": "atlasruntime-stage-r1-1",
    "dataset_version": "1.0.0",
    "scorer_version": "1.0.0",
    "timeout_seconds": 300,
    "max_turns": 14,
    "max_tool_calls": 60,
    "capture_evidence": True,
    "handoff_mode": "dual",
    "budget_mode": "guarded",
    "compaction_mode": "off",
    "tool_error_mode": "corrected",
    "tool_result_mode": "off",
    "worker_contract_mode": "legacy",
    "execution_integrity_mode": "record",
    "token_budget": 120_000,
}

OWNERSHIP_MODE_BY_ARM = {"o0": "off", "o1": "enforce"}


def _settings(arm: str, runs_root: Path) -> RunnerSettings:
    if arm not in ARMS:
        raise ValueError(f"unknown arm: {arm}")
    return RunnerSettings(
        **FIXED,
        runs_root=runs_root,
        variant=arm.upper(),
        ownership_mode=OWNERSHIP_MODE_BY_ARM[arm],
    )


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def _artifact_evidence(result: Any) -> dict[str, Any]:
    """Deliverable facts recomputed from the exported workspace and the fixture baseline.

    ``workspace.patch`` is not used: the M5 export writes it as one line, so a diff-based parser
    cannot attribute changes to files. The exported tree is the authoritative comparison.
    """

    checks = dict(result.oracle.checks)
    export = Path(result.workspace_export) if result.workspace_export else None
    fixture = Path(result.fixture_dir)
    artifacts = sorted(result.task.required_artifacts)
    present: list[str] = []
    changed: list[str] = []
    stub: list[str] = []
    for artifact in artifacts:
        target = export / artifact if export is not None else None
        if target is None or not target.is_file():
            continue
        present.append(artifact)
        content = target.read_text(encoding="utf-8", errors="replace")
        baseline_path = fixture / artifact
        baseline = (
            baseline_path.read_text(encoding="utf-8", errors="replace")
            if baseline_path.is_file()
            else ""
        )
        if content != baseline:
            changed.append(artifact)
            if "NotImplementedError" in content:
                stub.append(artifact)
    return {
        "deliverable_present": bool(checks.get("required_artifacts_present"))
        and len(present) == len(artifacts),
        "deliverable_present_paths": present,
        "deliverable_changed": changed,
        "deliverable_stub": stub,
        "deliverable_stub_note": "stub = changed artifact that still raises NotImplementedError",
    }


def run_round(
    sample_ids: tuple[str, ...],
    repetitions: int,
) -> dict[str, Any]:
    round_id = f"r11-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    round_root = RESULTS_ROOT / round_id
    runs_root = round_root / "runs"
    round_root.mkdir(parents=True, exist_ok=False)

    manifest: dict[str, Any] = {
        "round_id": round_id,
        "stage": "r1-1",
        "suite": "r1-1-ownership-integrity",
        "sample_ids": list(sample_ids),
        "arms": list(ARMS),
        "arm_conditions": {arm: {"ownership_mode": OWNERSHIP_MODE_BY_ARM[arm]} for arm in ARMS},
        "repetitions": repetitions,
        "fixed_conditions": dict(FIXED),
        "git_commit": git_commit(REPO_ROOT),
        "git_dirty": git_dirty(REPO_ROOT),
        "started_at": datetime.now(UTC).isoformat(),
        "pairs": [],
    }
    started = time.perf_counter()

    for repetition in range(repetitions):
        for sample_id in sample_ids:
            a0, a1 = variants(definition_by_id(sample_id))
            del a0  # both arms execute the same multi-worker plan
            pair: dict[str, Any] = {
                "definition": sample_id,
                "repetition": repetition,
                "runs": {},
            }
            order = ARMS if repetition % 2 == 0 else tuple(reversed(ARMS))
            for arm in order:
                settings = _settings(arm, runs_root)
                result = asyncio.run(run_sample(a1, settings=settings, repetition_id=repetition))
                extra = result.metrics.extra or {}
                ownership = extra.get("ownership") or {}
                trace = extra.get("trace") or {}
                pair["runs"][arm] = {
                    "arm": arm,
                    "sample_id": result.task.sample_id,
                    "run_id": result.identity.run_id,
                    "run_dir": str(result.run_dir),
                    "config_digest": result.identity.config_digest,
                    "success": result.success,
                    "failure_class": result.failure_class,
                    "failure_reason": result.failure_reason,
                    "oracle_pass": result.oracle.passed,
                    "oracle_checks": dict(result.oracle.checks),
                    "worker_success": {
                        name: bool(value.get("success"))
                        for name, value in result.worker_results.items()
                    },
                    "workers_started": len(result.worker_results),
                    "ownership_mode": ownership.get("mode"),
                    "ownership_checked_calls": ownership.get("checked_calls"),
                    "ownership_denials": ownership.get("denied_calls", 0),
                    "ownership_audit_findings": len(ownership.get("audit_findings") or ()),
                    "ownership_denial_reasons": sorted(
                        {entry.get("reason_code") for entry in ownership.get("denials") or ()}
                    ),
                    "trace_status": trace.get("status"),
                    "trace_coverage": trace.get("input_digest_coverage"),
                    "trace_proposed": trace.get("proposed_actions"),
                    "trace_completed": trace.get("completed_actions"),
                    "trace_unknown": trace.get("unknown_actions"),
                    "trace_recovered_vs_result": (trace.get("result_comparison") or {}).get(
                        "recovered_from_trajectory"
                    ),
                    "prompt_tokens": result.metrics.prompt_tokens,
                    "completion_tokens": result.metrics.completion_tokens,
                    "cost_usd": result.metrics.estimated_cost_usd,
                    "latency_ms": result.metrics.latency_ms,
                    "model_turns": result.metrics.model_turns,
                    "tool_calls": result.metrics.tool_calls,
                    **_artifact_evidence(result),
                }
                print(
                    f"{round_id} {sample_id} rep={repetition} {arm} "
                    f"success={result.success} oracle={result.oracle.passed} "
                    f"denials={pair['runs'][arm]['ownership_denials']} "
                    f"trace={trace.get('status')}",
                    flush=True,
                )
            manifest["pairs"].append(pair)
            _write_json(round_root / "manifest.json", manifest)

    runs = [run for pair in manifest["pairs"] for run in pair["runs"].values()]
    manifest["summary"] = _summary(runs)
    manifest["finished_at"] = datetime.now(UTC).isoformat()
    manifest["elapsed_seconds"] = round(time.perf_counter() - started, 3)
    _write_json(round_root / "manifest.json", manifest)
    return manifest


def _summary(runs: list[dict[str, Any]]) -> dict[str, Any]:
    def per_arm(selector) -> dict[str, Any]:  # noqa: ANN001
        return {arm: [selector(run) for run in runs if run["arm"] == arm] for arm in ARMS}

    return {
        "runs": len(runs),
        "oracle_pass_by_arm": {
            arm: sum(1 for run in runs if run["arm"] == arm and run["oracle_pass"]) for arm in ARMS
        },
        "ownership_denials_by_arm": {
            arm: sum(int(run.get("ownership_denials") or 0) for run in runs if run["arm"] == arm)
            for arm in ARMS
        },
        "trace_status_by_arm": {
            arm: sorted({str(run.get("trace_status")) for run in runs if run["arm"] == arm})
            for arm in ARMS
        },
        "trace_coverage_by_arm": per_arm(lambda run: run.get("trace_coverage")),
        "cost_usd_by_arm": per_arm(lambda run: run.get("cost_usd")),
        "model_turns_by_arm": per_arm(lambda run: run.get("model_turns")),
        "failure_classes": {
            arm: sorted(
                {
                    str(run.get("failure_class"))
                    for run in runs
                    if run["arm"] == arm and run.get("failure_class")
                }
            )
            for arm in ARMS
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="atlas-stage-r11-round")
    parser.add_argument("--definitions", default="")
    parser.add_argument("--repetitions", type=int, default=2)
    args = parser.parse_args(argv)

    sample_ids = (
        tuple(item.strip() for item in args.definitions.split(",") if item.strip())
        if args.definitions
        else DEFAULT_TASKS
    )
    manifest = run_round(sample_ids, args.repetitions)
    print(json.dumps(manifest["summary"], indent=2, sort_keys=True))
    return 0 if manifest["summary"]["runs"] else 1


if __name__ == "__main__":
    sys.exit(main())
