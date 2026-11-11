"""Stage R0.5 round runner: positive-delegation benchmark (Track A) and width sensitivity (Track B).

Every arm is executed by the *unchanged* evaluation runner:

* ``a0`` forced single worker (``delegation_route_mode=off``, task is the A0 variant),
* ``a1`` forced candidate DAG (``delegation_route_mode=off``, task is the A1 variant),
* ``a2`` selective routing (``delegation_route_mode=selective``, same A1 variant).

Track A uses policy ``r0.1.0`` (``max_parallel_workers=2``). Track B uses ``r0.2.0``, which differs
from ``r0.1.0`` in that single field only. A run never mixes the two versions inside one round.

The pre-registration file is written once, before the first live call, and every round refuses to
start if the frozen digests no longer match it.
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
from experiments.evaluation.stage_r05_tasks import (
    DEFINITIONS as R05_DEFINITIONS,
)
from experiments.evaluation.stage_r05_tasks import (
    benchmark_digest,
    definition_by_id,
    preregistration,
    variants,
)

from atlasos.experiments import git_commit, git_dirty

REPO_ROOT = Path(__file__).resolve().parents[2]
RESULTS_ROOT = REPO_ROOT / "experiments" / "results" / "stage-r0-5"
PREREG_PATH = RESULTS_ROOT / "preregistration.json"

TRACK_A = "a"
TRACK_B = "b"
ARMS = ("a0", "a1", "a2")

#: Stage E1 tasks used by Track B: the two plans that only fail on the width limit.
TRACK_B_TASKS = ("e1-01-config", "e1-03-rbac")

POLICY_VERSION_BY_TRACK = {TRACK_A: "r0.1.0", TRACK_B: "r0.2.0"}

#: Fixed conditions, identical to Stage R0.
FIXED = {
    "benchmark_id": "atlasos-stage-r05",
    "dataset_version": "1.0.0",
    "scorer_version": "1.0.0",
    "timeout_seconds": 150,
    "max_turns": 14,
    "max_tool_calls": 60,
    "capture_evidence": True,
    "handoff_mode": "dual",
    "budget_mode": "guarded",
    "compaction_mode": "off",
    "tool_error_mode": "corrected",
    "tool_result_mode": "off",
    "worker_contract_mode": "legacy",
}


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def write_preregistration(*, supersede: bool = False) -> dict[str, Any]:
    """Freeze the benchmark. A different, already frozen definition set is never overwritten
    silently: it must be archived explicitly with ``supersede``, which also bumps the version."""

    payload = preregistration()
    if PREREG_PATH.is_file():
        existing = json.loads(PREREG_PATH.read_text(encoding="utf-8"))
        previous = str(existing.get("preregistration_digest") or "")
        if previous != payload["preregistration_digest"]:
            if not supersede:
                raise SystemExit(
                    "pre-registration mismatch: the frozen benchmark changed. Bump "
                    "BENCHMARK_VERSION and pass --supersede to archive the previous version."
                )
            archive = RESULTS_ROOT / f"preregistration-superseded-{previous[:12]}.json"
            if not archive.exists():
                _write_json(archive, existing)
    _write_json(PREREG_PATH, payload)
    return payload


def assert_preregistered() -> dict[str, Any]:
    if not PREREG_PATH.is_file():
        raise SystemExit("pre-registration is missing: run the 'prereg' command first")
    frozen = json.loads(PREREG_PATH.read_text(encoding="utf-8"))
    current = preregistration()
    if frozen.get("preregistration_digest") != current["preregistration_digest"]:
        raise SystemExit(
            "pre-registration mismatch: definitions, contracts, oracle or policy changed after the "
            "benchmark was frozen"
        )
    return frozen


def _settings(arm: str, policy_version: str, runs_root: Path) -> RunnerSettings:
    if arm not in ARMS:
        raise ValueError(f"unknown arm: {arm}")
    return RunnerSettings(
        **FIXED,
        runs_root=runs_root,
        variant=arm.upper(),
        delegation_route_mode="selective" if arm == "a2" else "off",
        delegation_policy_version=policy_version,
    )


def _tasks_for(track: str, sample_id: str) -> dict[str, Any]:
    if track == TRACK_A:
        a0, a1 = variants(definition_by_id(sample_id))
        return {"a0": a0, "a1": a1, "a2": a1}
    from experiments.evaluation.stage_e_tasks import definition_by_id as e1_definition
    from experiments.evaluation.stage_e_tasks import variants as e1_variants

    a0, a1 = e1_variants(e1_definition(sample_id))
    return {"a0": a0, "a1": a1, "a2": a1}


def run_round(
    track: str,
    sample_ids: tuple[str, ...],
    repetitions: int,
) -> dict[str, Any]:
    if track not in POLICY_VERSION_BY_TRACK:
        raise ValueError(f"unknown track: {track}")
    frozen = assert_preregistered()
    policy_version = POLICY_VERSION_BY_TRACK[track]
    round_id = f"r05-{track}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    round_root = RESULTS_ROOT / round_id
    runs_root = round_root / "runs"
    round_root.mkdir(parents=True, exist_ok=False)

    manifest: dict[str, Any] = {
        "round_id": round_id,
        "track": track,
        "suite": "r0-5",
        "sample_ids": list(sample_ids),
        "arms": list(ARMS),
        "repetitions": repetitions,
        "policy_version": policy_version,
        "benchmark_version": frozen.get("benchmark_version"),
        "preregistration_digest": frozen["preregistration_digest"],
        "positive_digests": {
            item.sample_id: benchmark_digest(item)
            for item in R05_DEFINITIONS
            if item.sample_id in sample_ids
        },
        "fixed_conditions": dict(FIXED),
        "git_commit": git_commit(REPO_ROOT),
        "git_dirty": git_dirty(REPO_ROOT),
        "started_at": datetime.now(UTC).isoformat(),
        "pairs": [],
    }
    started = time.perf_counter()

    for repetition in range(repetitions):
        for sample_id in sample_ids:
            tasks = _tasks_for(track, sample_id)
            order = ARMS if repetition % 2 == 0 else tuple(reversed(ARMS))
            pair: dict[str, Any] = {
                "definition": sample_id,
                "repetition": repetition,
                "policy_version": policy_version,
                "runs": {},
            }
            for arm in order:
                task = tasks[arm]
                settings = _settings(arm, policy_version, runs_root)
                result = asyncio.run(run_sample(task, settings=settings, repetition_id=repetition))
                routing = (result.metrics.extra or {}).get("routing") or {}
                decision = routing.get("decision") or {}
                pair["runs"][arm] = {
                    "arm": arm,
                    "sample_id": result.task.sample_id,
                    "run_id": result.identity.run_id,
                    "run_dir": str(result.run_dir),
                    "success": result.success,
                    "failure_class": result.failure_class,
                    "oracle_passed": result.oracle.passed,
                    "route": decision.get("route"),
                    "route_reasons": decision.get("reason_codes"),
                    "route_digest": routing.get("route_digest"),
                    "policy_version": decision.get("policy_version"),
                    "executed_worker_count": int(
                        routing.get("executed_worker_count") or len(result.task.workers)
                    ),
                    "workers_started": len(result.worker_results),
                    "prompt_tokens": result.metrics.prompt_tokens,
                    "completion_tokens": result.metrics.completion_tokens,
                    "cache_hit_tokens": result.metrics.cache_hit_tokens,
                    "cache_miss_tokens": result.metrics.cache_miss_tokens,
                    "cost_usd": result.metrics.estimated_cost_usd,
                    "latency_ms": result.metrics.latency_ms,
                    "model_turns": result.metrics.model_turns,
                    "tool_calls": result.metrics.tool_calls,
                }
                print(
                    f"{round_id} {sample_id} rep={repetition} {arm} "
                    f"success={result.success} class={result.failure_class} "
                    f"route={decision.get('route')} workers={routing.get('executed_worker_count')}",
                    flush=True,
                )
            manifest["pairs"].append(pair)
            _write_json(round_root / "manifest.json", manifest)

    runs = [run for pair in manifest["pairs"] for run in pair["runs"].values()]
    manifest["summary"] = {
        "runs": len(runs),
        "success_by_arm": {
            arm: sum(1 for run in runs if run["arm"] == arm and run["success"]) for arm in ARMS
        },
        "oracle_by_arm": {
            arm: sum(1 for run in runs if run["arm"] == arm and run["oracle_passed"])
            for arm in ARMS
        },
        "routes": {
            arm: sorted({str(run["route"]) for run in runs if run["arm"] == arm}) for arm in ARMS
        },
        "executed_workers": {
            arm: sorted(
                {int(run["executed_worker_count"] or 0) for run in runs if run["arm"] == arm}
            )
            for arm in ARMS
        },
    }
    manifest["finished_at"] = datetime.now(UTC).isoformat()
    manifest["elapsed_seconds"] = round(time.perf_counter() - started, 3)
    _write_json(round_root / "manifest.json", manifest)
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="atlas-stage-r05-round")
    parser.add_argument("command", choices=("prereg", "a", "b"))
    parser.add_argument(
        "--definitions",
        default="",
        help="comma separated sample ids; defaults to the frozen matrix for the track",
    )
    parser.add_argument("--repetitions", type=int, default=2)
    parser.add_argument(
        "--supersede",
        action="store_true",
        help="archive a previous pre-registration after a documented benchmark version bump",
    )
    args = parser.parse_args(argv)

    if args.command == "prereg":
        payload = write_preregistration(supersede=args.supersede)
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    track = args.command
    if args.definitions:
        sample_ids = tuple(item.strip() for item in args.definitions.split(",") if item.strip())
    elif track == TRACK_A:
        sample_ids = tuple(
            item.sample_id for item in R05_DEFINITIONS if item.family.startswith("P")
        )
    else:
        sample_ids = TRACK_B_TASKS

    known = {item.sample_id for item in R05_DEFINITIONS} | set(TRACK_B_TASKS)
    unknown = [item for item in sample_ids if item not in known]
    if unknown:
        raise SystemExit(f"unknown sample ids: {unknown}")

    manifest = run_round(track, sample_ids, args.repetitions)
    print(json.dumps(manifest["summary"], indent=2, sort_keys=True))
    return 0 if manifest["summary"]["runs"] else 1


if __name__ == "__main__":
    sys.exit(main())
