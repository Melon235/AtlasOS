"""Offline R1-2A harness: verify historical exports without touching them.

Reads finished run directories, verifies the exported workspace against the trusted contract for
that sample, and writes everything into a new analysis directory. Historical runs are opened
read-only; nothing under ``experiments/results/stage-r0-5`` or ``stage-r1-1`` is modified.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from experiments.evaluation.stage_r1_2_contracts import spec_for_sample

from atlasos.verification.dynamic import SandboxContractRunner
from atlasos.verification.engine import VerificationEngine
from atlasos.verification.files import snapshot_digest

REPO_ROOT = Path(__file__).resolve().parents[2]
RESULTS_ROOT = REPO_ROOT / "experiments" / "results"
ANALYSIS_ROOT = RESULTS_ROOT / "stage-r1-2"


@dataclass(frozen=True)
class HistoricalRun:
    stage: str
    round_id: str
    run_key: str
    sample_id: str
    arm: str | None
    run_dir: Path
    workspace: Path | None
    oracle_pass: bool
    failure_class: str
    fixture_dir: Path | None
    recorded_artifact_digest: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "round_id": self.round_id,
            "run_key": self.run_key,
            "sample_id": self.sample_id,
            "arm": self.arm,
            "run_dir": str(self.run_dir),
            "workspace": str(self.workspace) if self.workspace else None,
            "oracle_pass": self.oracle_pass,
            "failure_class": self.failure_class,
        }


def discover(
    stage: str,
    *,
    required_samples: Iterable[str] | None = None,
    rounds: Iterable[str] | None = None,
) -> list[HistoricalRun]:
    """Collect R0.5 / R1-1 runs that exported a workspace."""

    wanted = set(required_samples or ())
    wanted_rounds = set(rounds or ())
    runs: list[HistoricalRun] = []
    for manifest in sorted((RESULTS_ROOT / stage).glob("*/manifest.json")):
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        round_id = manifest.parent.name
        if wanted_rounds and round_id not in wanted_rounds:
            continue
        for pair in payload.get("pairs") or []:
            sample = pair.get("definition") or pair.get("sample_id") or ""
            if wanted and sample not in wanted:
                continue
            for arm, run in (pair.get("runs") or {}).items():
                run_dir = Path(run.get("run_dir") or "")
                workspace = run_dir / "workspace"
                runs.append(
                    HistoricalRun(
                        stage=stage,
                        round_id=round_id,
                        run_key=f"{sample}-r{pair.get('repetition', 0)}-{arm}",
                        sample_id=sample,
                        arm=None if arm in {"run", ""} else arm,
                        run_dir=run_dir,
                        workspace=workspace if workspace.is_dir() else None,
                        oracle_pass=bool(run.get("oracle_pass")),
                        failure_class=str(run.get("failure_class") or ""),
                        fixture_dir=run.get("fixture_dir"),
                        recorded_artifact_digest=run.get("patch_digest"),
                    )
                )
    return runs


async def verify_runs(
    runs: list[HistoricalRun],
    *,
    dynamic_runner: SandboxContractRunner | None = None,
    dynamic_limit: int | None = None,
) -> list[dict[str, Any]]:
    static_engine = VerificationEngine(None)
    dynamic_engine = VerificationEngine(dynamic_runner) if dynamic_runner is not None else None
    records: list[dict[str, Any]] = []
    dynamic_budget = dynamic_limit if dynamic_limit is not None else len(runs)
    for index, run in enumerate(runs):
        spec = spec_for_sample(run.sample_id)
        if spec is None:
            records.append({**run.as_dict(), "verification": None, "unavailable": "NO_SPEC"})
            continue
        if run.workspace is None:
            records.append({**run.as_dict(), "verification": None, "unavailable": "UNAVAILABLE"})
            continue
        started = time.perf_counter()
        engine = (
            dynamic_engine
            if dynamic_engine is not None and index < dynamic_budget
            else static_engine
        )
        result = await engine.verify_async(
            spec,
            run.workspace,
            baseline_digests=_baseline_digests(run.sample_id),
        )
        records.append(
            {
                **run.as_dict(),
                "verification": result.as_dict(),
                "unavailable": None,
                "analysis_wall_ms": round((time.perf_counter() - started) * 1000.0, 3),
                "snapshot_digest": snapshot_digest(run.workspace),
            }
        )
    return records


def _baseline_digests(sample_id: str) -> dict[str, str]:
    """Digest the published fixture from the task definition itself.

    Using the definition rather than a historical run directory means a frozen-contract check never
    depends on the run it is judging, and old run directories stay untouched.
    """

    import hashlib

    from experiments.evaluation.stage_r05_tasks import definition_by_id

    try:
        definition = definition_by_id(sample_id)
    except KeyError:
        return {}
    return {
        path: hashlib.sha256(content.encode("utf-8")).hexdigest()
        for path, content in definition.visible_files.items()
    }


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Confusion matrix per verification area against the independent oracle."""

    areas = (
        "deliverable_presence",
        "structural_validity",
        "public_contract_status",
        "integration_status",
        "verification_overall",
    )
    matrix: dict[str, Counter[str]] = {area: Counter() for area in areas}
    reasons: Counter[str] = Counter()
    unavailable = Counter()
    overhead: list[float] = []
    by_sample: dict[str, Counter[str]] = {}
    for record in records:
        if record.get("unavailable"):
            unavailable[record["unavailable"]] += 1
            continue
        verification = record["verification"]
        oracle_pass = bool(record["oracle_pass"])
        for area in areas:
            status = verification[area]
            bucket = _bucket(area, status, oracle_pass)
            matrix[area][bucket] += 1
            if bucket in {"false_positive", "false_negative"}:
                by_sample.setdefault(record["sample_id"], Counter())[bucket] += 1
        for finding in verification["findings"]:
            if finding["status"] in {"FAIL", "UNKNOWN"}:
                reasons[finding["reason_code"]] += 1
        if record.get("analysis_wall_ms"):
            overhead.append(float(record["analysis_wall_ms"]))
    return {
        "runs": len(records),
        "verified": sum(1 for record in records if not record.get("unavailable")),
        "unavailable": dict(unavailable),
        "matrix": {area: dict(counts) for area, counts in matrix.items()},
        "reason_codes": dict(reasons.most_common()),
        "false_positive_or_negative_by_sample": {
            sample: dict(counts) for sample, counts in by_sample.items()
        },
        "verification_wall_ms": {
            "mean": round(sum(overhead) / len(overhead), 3) if overhead else None,
            "max": max(overhead) if overhead else None,
            "samples": len(overhead),
        },
    }


def _bucket(area: str, status: str, oracle_pass: bool) -> str:
    """Treat oracle PASS as the positive class for detection quality."""

    if status in {"NOT_APPLICABLE"}:
        return "not_applicable"
    if status == "UNKNOWN":
        return "unknown"
    detector_fail = status == "FAIL"
    if detector_fail and not oracle_pass:
        return "true_positive"
    if detector_fail and oracle_pass:
        return "false_positive"
    if not detector_fail and oracle_pass:
        return "true_negative"
    return "false_negative"


_COLUMNS = (
    "stage | round | run | sample | oracle | overall | presence | structural | public | integration"
)


def _render(records: list[dict[str, Any]], summary: dict[str, Any]) -> str:
    lines = [_COLUMNS]
    for record in records:
        head = (
            f"{record['stage']} | {record['round_id']} | {record['run_key']} | "
            f"{record['sample_id']} | {record['oracle_pass']} | "
        )
        verification = record.get("verification")
        if verification is None:
            lines.append(f"{head}{record['unavailable']} | - | - | - | -")
            continue
        lines.append(
            f"{head}{verification['verification_overall']} | "
            f"{verification['deliverable_presence']} | "
            f"{verification['structural_validity']} | "
            f"{verification['public_contract_status']} | "
            f"{verification['integration_status']}"
        )
    return "\n".join(lines)


async def main_async(argv: list[str] | None = None) -> dict[str, Any]:
    parser = argparse.ArgumentParser(description="R1-2A offline verification harness")
    parser.add_argument("--stage", action="append", default=[])
    parser.add_argument(
        "--samples", default="r05-p1-dependency-audit,r05-p2-modules,r05-p3-frozen-interface"
    )
    parser.add_argument("--dynamic", choices=["off", "sandbox"], default="off")
    parser.add_argument("--dynamic-limit", type=int, default=4)
    parser.add_argument("--rounds", default="")
    args = parser.parse_args(argv)
    samples = [item.strip() for item in args.samples.split(",") if item.strip()]
    rounds = [item.strip() for item in args.rounds.split(",") if item.strip()]
    stages = args.stage or ["stage-r1-1"]
    runs: list[HistoricalRun] = []
    for stage in stages:
        runs.extend(discover(stage, required_samples=samples, rounds=rounds))
    runner = None
    if args.dynamic == "sandbox":
        from experiments.evaluation.runner import _build_provider

        from atlasos.config import load_config

        config = load_config(Path(REPO_ROOT / "experiments" / "configs" / "eval-benchmark.yaml"))
        provider = _build_provider(config)
        runner = SandboxContractRunner(provider, image=config.sandbox.image, timeout_seconds=180)
    records = await verify_runs(
        runs, dynamic_runner=runner, dynamic_limit=args.dynamic_limit if runner else None
    )
    summary = summarize(records)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    out = ANALYSIS_ROOT / f"r12-{stamp}"
    out.mkdir(parents=True, exist_ok=True)
    (out / "records.json").write_text(json.dumps(records, indent=2), encoding="utf-8")
    (out / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8"
    )
    (out / "records.txt").write_text(_render(records, summary), encoding="utf-8")
    print(_render(records, summary))
    print(json.dumps(summary, indent=2, sort_keys=True))
    print(f"analysis written to {out}")
    return summary


def main(argv: list[str] | None = None) -> dict[str, Any]:
    return asyncio.run(main_async(argv))


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    main()
