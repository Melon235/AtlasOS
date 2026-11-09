"""Paired A0/A1 Stage E1 runner with per-sample process isolation."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from experiments.evaluation.stage_e_evidence import summarize_evidence
from experiments.evaluation.stage_e_tasks import DEFINITIONS

from atlasos.experiments import git_commit, git_dirty

REPO_ROOT = Path(__file__).resolve().parents[2]
RESULTS_ROOT = REPO_ROOT / "experiments" / "results" / "stage-e1"
SMOKE_DEFINITIONS = ("e1-01-config", "e1-05-pairs", "e1-08-stats", "e1-12-conflict")


def _run_one(
    *,
    sample_id: str,
    repetition: int,
    runs_root: Path,
    timeout_seconds: int,
    max_turns: int,
) -> dict[str, Any]:
    command = [
        sys.executable,
        "-m",
        "experiments.evaluation.stage_e_cli",
        "--sample-id",
        sample_id,
        "--repetition",
        str(repetition),
        "--runs-root",
        str(runs_root),
        "--max-turns",
        str(max_turns),
        "--timeout",
        str(timeout_seconds),
    ]
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(REPO_ROOT / "src"), str(REPO_ROOT)]),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    process = subprocess.Popen(  # noqa: S603 - fixed command
        command,
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds + 180)
    except subprocess.TimeoutExpired:
        timed_out = True
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                capture_output=True,
                check=False,
            )
        else:
            process.kill()
        stdout, stderr = process.communicate()
    parsed: dict[str, Any] | None = None
    for line in reversed((stdout or "").splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            candidate = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and "sample_id" in candidate:
            parsed = candidate
            break
    parsed = parsed or {"sample_id": sample_id}
    parsed["timed_out"] = timed_out
    parsed["returncode"] = process.returncode
    parsed["stderr_tail"] = (stderr or "")[-2000:]
    return parsed


def _paired(definitions: tuple[Any, ...], repetitions: int, **kwargs: Any) -> dict[str, Any]:
    round_id = f"e1-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    round_root = RESULTS_ROOT / round_id
    runs_root = round_root / "runs"
    round_root.mkdir(parents=True, exist_ok=False)
    manifest: dict[str, Any] = {
        "round_id": round_id,
        "suite": "smoke" if len(definitions) < len(DEFINITIONS) else "formal",
        "definitions": [item.sample_id for item in definitions],
        "repetitions_requested": repetitions,
        "git_commit": git_commit(REPO_ROOT),
        "git_dirty": git_dirty(REPO_ROOT),
        "started_at": datetime.now(UTC).isoformat(),
        "pairs": [],
    }
    started = time.perf_counter()
    for repetition in range(repetitions):
        for definition in definitions:
            order = ("a0", "a1") if repetition % 2 == 0 else ("a1", "a0")
            pair: dict[str, Any] = {
                "definition": definition.sample_id,
                "repetition": repetition,
                "runs": {},
            }
            for suffix in order:
                sample_id = f"{definition.sample_id}-{suffix}"
                result = _run_one(
                    sample_id=sample_id,
                    repetition=repetition,
                    runs_root=runs_root,
                    **kwargs,
                )
                pair["runs"][suffix] = result
                print(
                    f"{round_id} {definition.sample_id} rep={repetition} {suffix} "
                    f"success={result.get('success')} class={result.get('failure_class')}",
                    flush=True,
                )
            manifest["pairs"].append(pair)
            (round_root / "manifest.json").write_text(
                json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
            )
    all_results = [result for pair in manifest["pairs"] for result in pair["runs"].values()]
    a0 = [
        result for pair in manifest["pairs"] for key, result in pair["runs"].items() if key == "a0"
    ]
    a1 = [
        result for pair in manifest["pairs"] for key, result in pair["runs"].items() if key == "a1"
    ]
    manifest["summary"] = {
        "runs": len(all_results),
        "a0_success": sum(1 for item in a0 if item.get("success")),
        "a0_total": len(a0),
        "a1_success": sum(1 for item in a1 if item.get("success")),
        "a1_total": len(a1),
        "timed_out": sum(1 for item in all_results if item.get("timed_out")),
    }
    a1_dirs = [
        str(item["run_dir"])
        for item in a1
        if item.get("run_dir") and Path(item["run_dir"]).is_dir()
    ]
    if a1_dirs:
        manifest["a1_evidence"] = summarize_evidence(a1_dirs)
    manifest["finished_at"] = datetime.now(UTC).isoformat()
    manifest["elapsed_seconds"] = round(time.perf_counter() - started, 3)
    (round_root / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="atlas-stage-e1-round")
    parser.add_argument("suite", choices=("smoke", "formal"))
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--max-turns", type=int, default=14)
    parser.add_argument("--timeout", type=int, default=150)
    args = parser.parse_args(argv)
    definitions = (
        tuple(item for item in DEFINITIONS if item.sample_id in SMOKE_DEFINITIONS)
        if args.suite == "smoke"
        else DEFINITIONS
    )
    manifest = _paired(
        definitions,
        args.repetitions,
        timeout_seconds=args.timeout,
        max_turns=args.max_turns,
    )
    print(json.dumps(manifest["summary"], indent=2, sort_keys=True))
    return (
        0
        if manifest["summary"]["a0_success"] == manifest["summary"]["a0_total"]
        and manifest["summary"]["a1_success"] == manifest["summary"]["a1_total"]
        else 1
    )


if __name__ == "__main__":
    sys.exit(main())
