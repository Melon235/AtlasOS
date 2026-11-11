"""Paired A0/A1 Stage E1 runner with per-sample process isolation.

Stage G1 reuses the same per-sample CLI and tasks. Its ``g1`` suite runs the *same* A1
multi-worker variant twice per repetition: once with the AnyCode legacy 500-character handoff
(``H0``) and once with the AtlasOS dual-channel handoff (``H1``). Only the handoff mode differs.

Stage G2a adds the ``g2a`` suite: the same A1 variant with dual handoff in both arms, differing
only in ``--budget-mode`` (``B0`` legacy, ``B1`` guarded). Handoff is held constant on purpose so
the budget guard is the single changed variable.
"""

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
G1_RESULTS_ROOT = REPO_ROOT / "experiments" / "results" / "stage-g1"
G2A_RESULTS_ROOT = REPO_ROOT / "experiments" / "results" / "stage-g2a"
SMOKE_DEFINITIONS = ("e1-01-config", "e1-05-pairs", "e1-08-stats", "e1-12-conflict")
G1_DEFINITIONS = ("e1-05-pairs", "e1-07-export", "e1-12-conflict")
G1_VARIANTS = ("h0", "h1")
G1_HANDOFF_MODES = {"h0": "legacy", "h1": "dual"}
G2A_DEFINITIONS = ("e1-02-report", "e1-07-export")
G2A_VARIANTS = ("b0", "b1")
G2A_BUDGET_MODES = {"b0": "legacy", "b1": "guarded"}
G2B1_RESULTS_ROOT = REPO_ROOT / "experiments" / "results" / "stage-g2b1"
G2B1_DEFINITIONS = ("e1-02-report", "e1-10-search", "e1-12-conflict")
G2B1_VARIANTS = ("c0", "c1")
G2B1_COMPACTION_MODES = {"c0": "off", "c1": "rolling_2"}
G31_RESULTS_ROOT = REPO_ROOT / "experiments" / "results" / "stage-g3-1"
G31_DEFINITIONS = ("e1-01-config", "e1-12-conflict")
G31_VARIANTS = ("t0", "t1")
G31_TOOL_ERROR_MODES = {"t0": "legacy", "t1": "corrected"}
G32_RESULTS_ROOT = REPO_ROOT / "experiments" / "results" / "stage-g3-2"
G32_DEFINITIONS = ("e1-02-report", "e1-10-search", "e1-12-conflict")
G32_VARIANTS = ("o0", "o1")
G32_TOOL_RESULT_MODES = {"o0": "off", "o1": "ingress_offload"}
G33_RESULTS_ROOT = REPO_ROOT / "experiments" / "results" / "stage-g3-3"
G33_DEFINITIONS = ("e1-02-report", "e1-07-export", "e1-12-conflict")
G33_VARIANTS = ("c0", "c1")
G33_CONTRACT_MODES = {"c0": "legacy", "c1": "structured"}
G34A_RESULTS_ROOT = REPO_ROOT / "experiments" / "results" / "stage-g3-4a"
G34A_DEFINITIONS = ("e1-02-report", "e1-07-export", "e1-12-conflict")
G34A_VARIANTS = ("a0", "a1")
R0_RESULTS_ROOT = REPO_ROOT / "experiments" / "results" / "stage-r0"
R0_DEFINITIONS = ("e1-02-report", "e1-07-export", "e1-12-conflict")
R0_VARIANTS = ("a0", "a1", "a2")


def _run_one(
    *,
    sample_id: str,
    repetition: int,
    runs_root: Path,
    timeout_seconds: int,
    max_turns: int,
    handoff_mode: str = "legacy",
    budget_mode: str = "legacy",
    compaction_mode: str = "off",
    tool_error_mode: str = "legacy",
    tool_result_mode: str = "off",
    worker_contract_mode: str = "legacy",
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
        "--handoff-mode",
        handoff_mode,
        "--budget-mode",
        budget_mode,
        "--compaction-mode",
        compaction_mode,
        "--tool-error-mode",
        tool_error_mode,
        "--tool-result-mode",
        tool_result_mode,
        "--worker-contract-mode",
        worker_contract_mode,
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


def _g1_pairs(
    definitions: tuple[Any, ...],
    repetitions: int,
    **kwargs: Any,
) -> dict[str, Any]:
    """Run H0 (legacy) and H1 (dual) on the same A1 variant, alternating order."""

    round_id = f"g1-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    round_root = G1_RESULTS_ROOT / round_id
    runs_root = round_root / "runs"
    round_root.mkdir(parents=True, exist_ok=False)
    manifest: dict[str, Any] = {
        "round_id": round_id,
        "suite": "g1",
        "definitions": [item.sample_id for item in definitions],
        "handoff_modes": dict(G1_HANDOFF_MODES),
        "repetitions_requested": repetitions,
        "git_commit": git_commit(REPO_ROOT),
        "git_dirty": git_dirty(REPO_ROOT),
        "started_at": datetime.now(UTC).isoformat(),
        "pairs": [],
    }
    started = time.perf_counter()
    for repetition in range(repetitions):
        for definition in definitions:
            order = G1_VARIANTS if repetition % 2 == 0 else tuple(reversed(G1_VARIANTS))
            pair: dict[str, Any] = {
                "definition": definition.sample_id,
                "repetition": repetition,
                "runs": {},
            }
            for suffix in order:
                result = _run_one(
                    sample_id=f"{definition.sample_id}-a1",
                    repetition=repetition,
                    runs_root=runs_root,
                    handoff_mode=G1_HANDOFF_MODES[suffix],
                    **kwargs,
                )
                result["handoff_mode"] = G1_HANDOFF_MODES[suffix]
                pair["runs"][suffix] = result
                print(
                    f"{round_id} {definition.sample_id} rep={repetition} {suffix} "
                    f"({G1_HANDOFF_MODES[suffix]}) success={result.get('success')} "
                    f"class={result.get('failure_class')}",
                    flush=True,
                )
            manifest["pairs"].append(pair)
            (round_root / "manifest.json").write_text(
                json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
            )
    all_results = [result for pair in manifest["pairs"] for result in pair["runs"].values()]
    manifest["summary"] = {
        "runs": len(all_results),
        "h0_success": sum(
            1 for pair in manifest["pairs"] if pair["runs"].get("h0", {}).get("success")
        ),
        "h1_success": sum(
            1 for pair in manifest["pairs"] if pair["runs"].get("h1", {}).get("success")
        ),
        "pairs": len(manifest["pairs"]),
        "timed_out": sum(1 for item in all_results if item.get("timed_out")),
    }
    run_dirs = [
        str(item["run_dir"])
        for item in all_results
        if item.get("run_dir") and Path(item["run_dir"]).is_dir()
    ]
    if run_dirs:
        manifest["evidence"] = summarize_evidence(run_dirs)
    manifest["finished_at"] = datetime.now(UTC).isoformat()
    manifest["elapsed_seconds"] = round(time.perf_counter() - started, 3)
    (round_root / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )
    return manifest


def _g2b1_pairs(
    definitions: tuple[Any, ...],
    repetitions: int,
    **kwargs: Any,
) -> dict[str, Any]:
    """Run C0 (compaction off) and C1 (rolling_2); dual handoff and guarded budget in both."""

    round_id = f"g2b1-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    round_root = G2B1_RESULTS_ROOT / round_id
    runs_root = round_root / "runs"
    round_root.mkdir(parents=True, exist_ok=False)
    manifest: dict[str, Any] = {
        "round_id": round_id,
        "suite": "g2b1",
        "definitions": [item.sample_id for item in definitions],
        "compaction_modes": dict(G2B1_COMPACTION_MODES),
        "handoff_mode": "dual",
        "budget_mode": "guarded",
        "repetitions_requested": repetitions,
        "git_commit": git_commit(REPO_ROOT),
        "git_dirty": git_dirty(REPO_ROOT),
        "started_at": datetime.now(UTC).isoformat(),
        "pairs": [],
    }
    started = time.perf_counter()
    for repetition in range(repetitions):
        for definition in definitions:
            order = G2B1_VARIANTS if repetition % 2 == 0 else tuple(reversed(G2B1_VARIANTS))
            pair: dict[str, Any] = {
                "definition": definition.sample_id,
                "repetition": repetition,
                "runs": {},
            }
            for suffix in order:
                result = _run_one(
                    sample_id=f"{definition.sample_id}-a1",
                    repetition=repetition,
                    runs_root=runs_root,
                    handoff_mode="dual",
                    budget_mode="guarded",
                    compaction_mode=G2B1_COMPACTION_MODES[suffix],
                    **kwargs,
                )
                result["compaction_mode"] = G2B1_COMPACTION_MODES[suffix]
                pair["runs"][suffix] = result
                print(
                    f"{round_id} {definition.sample_id} rep={repetition} {suffix} "
                    f"({G2B1_COMPACTION_MODES[suffix]}) success={result.get('success')} "
                    f"class={result.get('failure_class')}",
                    flush=True,
                )
            manifest["pairs"].append(pair)
            (round_root / "manifest.json").write_text(
                json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
            )
    all_results = [result for pair in manifest["pairs"] for result in pair["runs"].values()]
    manifest["summary"] = {
        "runs": len(all_results),
        "c0_success": sum(
            1 for pair in manifest["pairs"] if pair["runs"].get("c0", {}).get("success")
        ),
        "c1_success": sum(
            1 for pair in manifest["pairs"] if pair["runs"].get("c1", {}).get("success")
        ),
        "pairs": len(manifest["pairs"]),
        "timed_out": sum(1 for item in all_results if item.get("timed_out")),
        "over_budget": sum(
            1 for item in all_results if item.get("failure_class") == "BUDGET_EXCEEDED"
        ),
    }
    run_dirs = [
        str(item["run_dir"])
        for item in all_results
        if item.get("run_dir") and Path(item["run_dir"]).is_dir()
    ]
    if run_dirs:
        manifest["evidence"] = summarize_evidence(run_dirs)
    manifest["finished_at"] = datetime.now(UTC).isoformat()
    manifest["elapsed_seconds"] = round(time.perf_counter() - started, 3)
    (round_root / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )
    return manifest


def _g2a_pairs(
    definitions: tuple[Any, ...],
    repetitions: int,
    **kwargs: Any,
) -> dict[str, Any]:
    """Run B0 (legacy budget) and B1 (guarded budget); dual handoff in both arms."""

    round_id = f"g2a-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    round_root = G2A_RESULTS_ROOT / round_id
    runs_root = round_root / "runs"
    round_root.mkdir(parents=True, exist_ok=False)
    manifest: dict[str, Any] = {
        "round_id": round_id,
        "suite": "g2a",
        "definitions": [item.sample_id for item in definitions],
        "budget_modes": dict(G2A_BUDGET_MODES),
        "handoff_mode": "dual",
        "repetitions_requested": repetitions,
        "git_commit": git_commit(REPO_ROOT),
        "git_dirty": git_dirty(REPO_ROOT),
        "started_at": datetime.now(UTC).isoformat(),
        "pairs": [],
    }
    started = time.perf_counter()
    for repetition in range(repetitions):
        for definition in definitions:
            order = G2A_VARIANTS if repetition % 2 == 0 else tuple(reversed(G2A_VARIANTS))
            pair: dict[str, Any] = {
                "definition": definition.sample_id,
                "repetition": repetition,
                "runs": {},
            }
            for suffix in order:
                result = _run_one(
                    sample_id=f"{definition.sample_id}-a1",
                    repetition=repetition,
                    runs_root=runs_root,
                    handoff_mode="dual",
                    budget_mode=G2A_BUDGET_MODES[suffix],
                    **kwargs,
                )
                result["budget_mode"] = G2A_BUDGET_MODES[suffix]
                pair["runs"][suffix] = result
                print(
                    f"{round_id} {definition.sample_id} rep={repetition} {suffix} "
                    f"({G2A_BUDGET_MODES[suffix]}) success={result.get('success')} "
                    f"class={result.get('failure_class')}",
                    flush=True,
                )
            manifest["pairs"].append(pair)
            (round_root / "manifest.json").write_text(
                json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
            )
    all_results = [result for pair in manifest["pairs"] for result in pair["runs"].values()]
    manifest["summary"] = {
        "runs": len(all_results),
        "b0_success": sum(
            1 for pair in manifest["pairs"] if pair["runs"].get("b0", {}).get("success")
        ),
        "b1_success": sum(
            1 for pair in manifest["pairs"] if pair["runs"].get("b1", {}).get("success")
        ),
        "pairs": len(manifest["pairs"]),
        "timed_out": sum(1 for item in all_results if item.get("timed_out")),
        "over_budget": sum(
            1 for item in all_results if item.get("failure_class") == "BUDGET_EXCEEDED"
        ),
    }
    run_dirs = [
        str(item["run_dir"])
        for item in all_results
        if item.get("run_dir") and Path(item["run_dir"]).is_dir()
    ]
    if run_dirs:
        manifest["evidence"] = summarize_evidence(run_dirs)
    manifest["finished_at"] = datetime.now(UTC).isoformat()
    manifest["elapsed_seconds"] = round(time.perf_counter() - started, 3)
    (round_root / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )
    return manifest


def _g31_pairs(
    definitions: tuple[Any, ...],
    repetitions: int,
    **kwargs: Any,
) -> dict[str, Any]:
    """Run T0 (legacy tool-error semantics) and T1 (corrected) on the same A1 variant.

    Handoff, budget and compaction are pinned so the only changed variable is the
    completed-error classification of the sandbox bash tool.
    """

    round_id = f"g31-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    round_root = G31_RESULTS_ROOT / round_id
    runs_root = round_root / "runs"
    round_root.mkdir(parents=True, exist_ok=False)
    manifest: dict[str, Any] = {
        "round_id": round_id,
        "suite": "g3-1",
        "definitions": [item.sample_id for item in definitions],
        "tool_error_modes": dict(G31_TOOL_ERROR_MODES),
        "handoff_mode": "dual",
        "budget_mode": "guarded",
        "compaction_mode": "off",
        "repetitions_requested": repetitions,
        "git_commit": git_commit(REPO_ROOT),
        "git_dirty": git_dirty(REPO_ROOT),
        "started_at": datetime.now(UTC).isoformat(),
        "pairs": [],
    }
    started = time.perf_counter()
    for repetition in range(repetitions):
        for definition in definitions:
            order = G31_VARIANTS if repetition % 2 == 0 else tuple(reversed(G31_VARIANTS))
            pair: dict[str, Any] = {
                "definition": definition.sample_id,
                "repetition": repetition,
                "runs": {},
            }
            for suffix in order:
                result = _run_one(
                    sample_id=f"{definition.sample_id}-a1",
                    repetition=repetition,
                    runs_root=runs_root,
                    handoff_mode="dual",
                    budget_mode="guarded",
                    compaction_mode="off",
                    tool_error_mode=G31_TOOL_ERROR_MODES[suffix],
                    **kwargs,
                )
                result["tool_error_mode"] = G31_TOOL_ERROR_MODES[suffix]
                pair["runs"][suffix] = result
                print(
                    f"{round_id} {definition.sample_id} rep={repetition} {suffix} "
                    f"({G31_TOOL_ERROR_MODES[suffix]}) success={result.get('success')} "
                    f"class={result.get('failure_class')}",
                    flush=True,
                )
            manifest["pairs"].append(pair)
            (round_root / "manifest.json").write_text(
                json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
            )
    all_results = [result for pair in manifest["pairs"] for result in pair["runs"].values()]
    manifest["summary"] = {
        "runs": len(all_results),
        "t0_success": sum(
            1 for pair in manifest["pairs"] if pair["runs"].get("t0", {}).get("success")
        ),
        "t1_success": sum(
            1 for pair in manifest["pairs"] if pair["runs"].get("t1", {}).get("success")
        ),
        "pairs": len(manifest["pairs"]),
        "timed_out": sum(1 for item in all_results if item.get("timed_out")),
        "over_budget": sum(
            1 for item in all_results if item.get("failure_class") == "BUDGET_EXCEEDED"
        ),
    }
    run_dirs = [
        str(item["run_dir"])
        for item in all_results
        if item.get("run_dir") and Path(item["run_dir"]).is_dir()
    ]
    if run_dirs:
        manifest["evidence"] = summarize_evidence(run_dirs)
    manifest["finished_at"] = datetime.now(UTC).isoformat()
    manifest["elapsed_seconds"] = round(time.perf_counter() - started, 3)
    (round_root / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )
    return manifest


def _g32_pairs(
    definitions: tuple[Any, ...],
    repetitions: int,
    **kwargs: Any,
) -> dict[str, Any]:
    """Run O0 (no ingress offloading) and O1 (ingress offloading) on the same A1 variant.

    Handoff, budget, tool-error semantics and compaction are pinned so the only changed variable is
    ``tool_result_mode``.
    """

    round_id = f"g32-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    round_root = G32_RESULTS_ROOT / round_id
    runs_root = round_root / "runs"
    round_root.mkdir(parents=True, exist_ok=False)
    manifest: dict[str, Any] = {
        "round_id": round_id,
        "suite": "g3-2",
        "definitions": [item.sample_id for item in definitions],
        "tool_result_modes": dict(G32_TOOL_RESULT_MODES),
        "handoff_mode": "dual",
        "budget_mode": "guarded",
        "tool_error_mode": "corrected",
        "compaction_mode": "off",
        "repetitions_requested": repetitions,
        "git_commit": git_commit(REPO_ROOT),
        "git_dirty": git_dirty(REPO_ROOT),
        "started_at": datetime.now(UTC).isoformat(),
        "pairs": [],
    }
    started = time.perf_counter()
    for repetition in range(repetitions):
        for definition in definitions:
            order = G32_VARIANTS if repetition % 2 == 0 else tuple(reversed(G32_VARIANTS))
            pair: dict[str, Any] = {
                "definition": definition.sample_id,
                "repetition": repetition,
                "runs": {},
            }
            for suffix in order:
                result = _run_one(
                    sample_id=f"{definition.sample_id}-a1",
                    repetition=repetition,
                    runs_root=runs_root,
                    handoff_mode="dual",
                    budget_mode="guarded",
                    compaction_mode="off",
                    tool_error_mode="corrected",
                    tool_result_mode=G32_TOOL_RESULT_MODES[suffix],
                    **kwargs,
                )
                result["tool_result_mode"] = G32_TOOL_RESULT_MODES[suffix]
                pair["runs"][suffix] = result
                print(
                    f"{round_id} {definition.sample_id} rep={repetition} {suffix} "
                    f"({G32_TOOL_RESULT_MODES[suffix]}) success={result.get('success')} "
                    f"class={result.get('failure_class')}",
                    flush=True,
                )
            manifest["pairs"].append(pair)
            (round_root / "manifest.json").write_text(
                json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
            )
    all_results = [result for pair in manifest["pairs"] for result in pair["runs"].values()]
    manifest["summary"] = {
        "runs": len(all_results),
        "o0_success": sum(
            1 for pair in manifest["pairs"] if pair["runs"].get("o0", {}).get("success")
        ),
        "o1_success": sum(
            1 for pair in manifest["pairs"] if pair["runs"].get("o1", {}).get("success")
        ),
        "pairs": len(manifest["pairs"]),
        "timed_out": sum(1 for item in all_results if item.get("timed_out")),
        "over_budget": sum(
            1 for item in all_results if item.get("failure_class") == "BUDGET_EXCEEDED"
        ),
    }
    run_dirs = [
        str(item["run_dir"])
        for item in all_results
        if item.get("run_dir") and Path(item["run_dir"]).is_dir()
    ]
    if run_dirs:
        manifest["evidence"] = summarize_evidence(run_dirs)
    manifest["finished_at"] = datetime.now(UTC).isoformat()
    manifest["elapsed_seconds"] = round(time.perf_counter() - started, 3)
    (round_root / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )
    return manifest


def _g33_pairs(
    definitions: tuple[Any, ...],
    repetitions: int,
    **kwargs: Any,
) -> dict[str, Any]:
    """Run C0 (original worker text) and C1 (structured contract) on the same A1 variant."""

    round_id = f"g33-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    round_root = G33_RESULTS_ROOT / round_id
    runs_root = round_root / "runs"
    round_root.mkdir(parents=True, exist_ok=False)
    manifest: dict[str, Any] = {
        "round_id": round_id,
        "suite": "g3-3",
        "definitions": [item.sample_id for item in definitions],
        "contract_modes": dict(G33_CONTRACT_MODES),
        "handoff_mode": "dual",
        "budget_mode": "guarded",
        "tool_error_mode": "corrected",
        "compaction_mode": "off",
        "tool_result_mode": "off",
        "repetitions_requested": repetitions,
        "git_commit": git_commit(REPO_ROOT),
        "git_dirty": git_dirty(REPO_ROOT),
        "started_at": datetime.now(UTC).isoformat(),
        "pairs": [],
    }
    started = time.perf_counter()
    for repetition in range(repetitions):
        for definition in definitions:
            order = G33_VARIANTS if repetition % 2 == 0 else tuple(reversed(G33_VARIANTS))
            pair: dict[str, Any] = {
                "definition": definition.sample_id,
                "repetition": repetition,
                "runs": {},
            }
            for suffix in order:
                result = _run_one(
                    sample_id=f"{definition.sample_id}-a1",
                    repetition=repetition,
                    runs_root=runs_root,
                    handoff_mode="dual",
                    budget_mode="guarded",
                    compaction_mode="off",
                    tool_error_mode="corrected",
                    tool_result_mode="off",
                    worker_contract_mode=G33_CONTRACT_MODES[suffix],
                    **kwargs,
                )
                result["worker_contract_mode"] = G33_CONTRACT_MODES[suffix]
                pair["runs"][suffix] = result
                print(
                    f"{round_id} {definition.sample_id} rep={repetition} {suffix} "
                    f"({G33_CONTRACT_MODES[suffix]}) success={result.get('success')} "
                    f"class={result.get('failure_class')}",
                    flush=True,
                )
            manifest["pairs"].append(pair)
            (round_root / "manifest.json").write_text(
                json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
            )
    all_results = [result for pair in manifest["pairs"] for result in pair["runs"].values()]
    manifest["summary"] = {
        "runs": len(all_results),
        "c0_success": sum(
            1 for pair in manifest["pairs"] if pair["runs"].get("c0", {}).get("success")
        ),
        "c1_success": sum(
            1 for pair in manifest["pairs"] if pair["runs"].get("c1", {}).get("success")
        ),
        "pairs": len(manifest["pairs"]),
        "timed_out": sum(1 for item in all_results if item.get("timed_out")),
        "over_budget": sum(
            1 for item in all_results if item.get("failure_class") == "BUDGET_EXCEEDED"
        ),
    }
    run_dirs = [
        str(item["run_dir"])
        for item in all_results
        if item.get("run_dir") and Path(item["run_dir"]).is_dir()
    ]
    if run_dirs:
        manifest["evidence"] = summarize_evidence(run_dirs)
    manifest["finished_at"] = datetime.now(UTC).isoformat()
    manifest["elapsed_seconds"] = round(time.perf_counter() - started, 3)
    (round_root / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )
    return manifest


def _g34a_pairs(
    definitions: tuple[Any, ...],
    repetitions: int,
    **kwargs: Any,
) -> dict[str, Any]:
    """Stage G3-4a: re-establish the trusted A0/A1 baseline after isolation changes.

    Every mechanism is pinned to the post-G3-3 defaults, so the only variable is the worker graph
    (single vs multi). No contract, no offloading, no compaction.
    """

    round_id = f"g34a-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    round_root = G34A_RESULTS_ROOT / round_id
    runs_root = round_root / "runs"
    round_root.mkdir(parents=True, exist_ok=False)
    manifest: dict[str, Any] = {
        "round_id": round_id,
        "suite": "g3-4a",
        "purpose": "trusted baseline after benchmark-integrity isolation",
        "definitions": [item.sample_id for item in definitions],
        "variants": list(G34A_VARIANTS),
        "handoff_mode": "dual",
        "budget_mode": "guarded",
        "tool_error_mode": "corrected",
        "compaction_mode": "off",
        "tool_result_mode": "off",
        "worker_contract_mode": "legacy",
        "repetitions_requested": repetitions,
        "git_commit": git_commit(REPO_ROOT),
        "git_dirty": git_dirty(REPO_ROOT),
        "started_at": datetime.now(UTC).isoformat(),
        "pairs": [],
    }
    started = time.perf_counter()
    for repetition in range(repetitions):
        for definition in definitions:
            order = G34A_VARIANTS if repetition % 2 == 0 else tuple(reversed(G34A_VARIANTS))
            pair: dict[str, Any] = {
                "definition": definition.sample_id,
                "repetition": repetition,
                "runs": {},
            }
            for variant in order:
                result = _run_one(
                    sample_id=f"{definition.sample_id}-{variant}",
                    repetition=repetition,
                    runs_root=runs_root,
                    handoff_mode="dual",
                    budget_mode="guarded",
                    compaction_mode="off",
                    tool_error_mode="corrected",
                    tool_result_mode="off",
                    worker_contract_mode="legacy",
                    **kwargs,
                )
                result["variant"] = variant
                pair["runs"][variant] = result
                print(
                    f"{round_id} {definition.sample_id} rep={repetition} {variant} "
                    f"success={result.get('success')} class={result.get('failure_class')}",
                    flush=True,
                )
            manifest["pairs"].append(pair)
            (round_root / "manifest.json").write_text(
                json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
            )
    all_results = [result for pair in manifest["pairs"] for result in pair["runs"].values()]
    manifest["summary"] = {
        "runs": len(all_results),
        "a0_success": sum(
            1 for pair in manifest["pairs"] if pair["runs"].get("a0", {}).get("success")
        ),
        "a1_success": sum(
            1 for pair in manifest["pairs"] if pair["runs"].get("a1", {}).get("success")
        ),
        "pairs": len(manifest["pairs"]),
        "timed_out": sum(1 for item in all_results if item.get("timed_out")),
        "over_budget": sum(
            1 for item in all_results if item.get("failure_class") == "BUDGET_EXCEEDED"
        ),
        "environment_failures": sum(
            1 for item in all_results if item.get("failure_class") == "ENVIRONMENT_FAILURE"
        ),
    }
    run_dirs = [
        str(item["run_dir"])
        for item in all_results
        if item.get("run_dir") and Path(item["run_dir"]).is_dir()
    ]
    if run_dirs:
        manifest["evidence"] = summarize_evidence(run_dirs)
    manifest["finished_at"] = datetime.now(UTC).isoformat()
    manifest["elapsed_seconds"] = round(time.perf_counter() - started, 3)
    (round_root / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )
    return manifest


def _r0_pairs(
    definitions: tuple[Any, ...],
    repetitions: int,
    *,
    timeout_seconds: int,
    max_turns: int,
) -> dict[str, Any]:
    """Stage R0: A0 (forced single) vs A1 (forced DAG) vs A2 (selective routing)."""

    import asyncio
    from dataclasses import replace

    from experiments.evaluation.runner import RunnerSettings, run_sample
    from experiments.evaluation.stage_e_tasks import variants

    round_id = f"r0-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    round_root = R0_RESULTS_ROOT / round_id
    runs_root = round_root / "runs"
    round_root.mkdir(parents=True, exist_ok=False)
    manifest: dict[str, Any] = {
        "round_id": round_id,
        "suite": "r0",
        "definitions": [item.sample_id for item in definitions],
        "arms": list(R0_VARIANTS),
        "handoff_mode": "dual",
        "budget_mode": "guarded",
        "tool_error_mode": "corrected",
        "compaction_mode": "off",
        "tool_result_mode": "off",
        "worker_contract_mode": "legacy",
        "router_policy_version": None,
        "router_policy_digest": None,
        "git_commit": git_commit(REPO_ROOT),
        "git_dirty": git_dirty(REPO_ROOT),
        "started_at": datetime.now(UTC).isoformat(),
        "pairs": [],
    }
    from experiments.evaluation.stage_r0_analysis import audit_rows
    from experiments.evaluation.stage_r0_routing import routing_policy

    policy = routing_policy()
    manifest["router_policy_version"] = policy.version
    manifest["router_policy_digest"] = policy.digest()
    manifest["shadow_audit"] = {
        row["sample_id"]: {
            "route": row["route_kind"],
            "reasons": row["reason_codes"],
            "policy_version": row["policy_version"],
        }
        for row in audit_rows()
    }

    started = time.perf_counter()
    for repetition in range(repetitions):
        for definition in definitions:
            a0_task, a1_task = variants(definition)
            a2_task = replace(a1_task, sample_id=f"{definition.sample_id}-a2")
            tasks = {"a0": a0_task, "a1": a1_task, "a2": a2_task}
            order = R0_VARIANTS if repetition % 2 == 0 else tuple(reversed(R0_VARIANTS))
            pair: dict[str, Any] = {
                "definition": definition.sample_id,
                "repetition": repetition,
                "runs": {},
            }
            for arm in order:
                settings = RunnerSettings(
                    benchmark_id="atlasos-stage-e1",
                    dataset_version="1.0.0",
                    scorer_version="1.0.0",
                    runs_root=runs_root,
                    timeout_seconds=timeout_seconds,
                    max_turns=max_turns,
                    max_tool_calls=60,
                    variant=arm.upper(),
                    capture_evidence=True,
                    handoff_mode="dual",
                    budget_mode="guarded",
                    compaction_mode="off",
                    tool_error_mode="corrected",
                    tool_result_mode="off",
                    worker_contract_mode="legacy",
                    delegation_route_mode="selective" if arm == "a2" else "off",
                )
                result = asyncio.run(
                    run_sample(tasks[arm], settings=settings, repetition_id=repetition)
                )
                routing = (result.metrics.extra or {}).get("routing") or {}
                pair["runs"][arm] = {
                    "arm": arm,
                    "sample_id": result.task.sample_id,
                    "success": result.success,
                    "failure_class": result.failure_class,
                    "run_id": result.identity.run_id,
                    "run_dir": str(result.run_dir),
                    "tool_calls": result.metrics.tool_calls,
                    "prompt_tokens": result.metrics.prompt_tokens,
                    "cost": result.metrics.estimated_cost_usd,
                    "route": (routing.get("decision") or {}).get("route"),
                    "route_reasons": (routing.get("decision") or {}).get("reason_codes"),
                    "executed_worker_count": routing.get("executed_worker_count"),
                }
                print(
                    f"{round_id} {definition.sample_id} rep={repetition} {arm} "
                    f"success={result.success} class={result.failure_class} "
                    f"route={(routing.get('decision') or {}).get('route')} "
                    f"workers={routing.get('executed_worker_count')}",
                    flush=True,
                )
            manifest["pairs"].append(pair)
            (round_root / "manifest.json").write_text(
                json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
            )
    all_results = [run for pair in manifest["pairs"] for run in pair["runs"].values()]
    manifest["summary"] = {
        "runs": len(all_results),
        "a0_success": sum(1 for r in all_results if r["arm"] == "a0" and r["success"]),
        "a1_success": sum(1 for r in all_results if r["arm"] == "a1" and r["success"]),
        "a2_success": sum(1 for r in all_results if r["arm"] == "a2" and r["success"]),
        "routes": {
            arm: sorted({str(r["route"]) for r in all_results if r["arm"] == arm})
            for arm in R0_VARIANTS
        },
    }
    run_dirs = [str(r["run_dir"]) for r in all_results if Path(str(r["run_dir"])).is_dir()]
    if run_dirs:
        manifest["evidence"] = summarize_evidence(run_dirs)
    manifest["finished_at"] = datetime.now(UTC).isoformat()
    manifest["elapsed_seconds"] = round(time.perf_counter() - started, 3)
    (round_root / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="atlas-stage-e1-round")
    parser.add_argument(
        "suite",
        choices=(
            "smoke",
            "formal",
            "g1",
            "g2a",
            "g2b1",
            "g3-1",
            "g3-2",
            "g3-3",
            "g3-4a",
            "r0",
        ),
    )
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--max-turns", type=int, default=14)
    parser.add_argument("--timeout", type=int, default=150)
    parser.add_argument("--handoff-mode", choices=("legacy", "dual"), default="legacy")
    args = parser.parse_args(argv)
    if args.suite == "r0":
        definitions = tuple(item for item in DEFINITIONS if item.sample_id in R0_DEFINITIONS)
        manifest = _r0_pairs(
            definitions,
            args.repetitions,
            timeout_seconds=args.timeout,
            max_turns=args.max_turns,
        )
        print(json.dumps(manifest["summary"], indent=2, sort_keys=True))
        return 0
    if args.suite == "g3-4a":
        definitions = tuple(item for item in DEFINITIONS if item.sample_id in G34A_DEFINITIONS)
        manifest = _g34a_pairs(
            definitions,
            args.repetitions,
            timeout_seconds=args.timeout,
            max_turns=args.max_turns,
        )
        print(json.dumps(manifest["summary"], indent=2, sort_keys=True))
        return 0 if manifest["summary"]["timed_out"] == 0 else 1
    if args.suite == "g3-3":
        definitions = tuple(item for item in DEFINITIONS if item.sample_id in G33_DEFINITIONS)
        manifest = _g33_pairs(
            definitions,
            args.repetitions,
            timeout_seconds=args.timeout,
            max_turns=args.max_turns,
        )
        print(json.dumps(manifest["summary"], indent=2, sort_keys=True))
        return 0 if manifest["summary"]["timed_out"] == 0 else 1
    if args.suite == "g3-2":
        definitions = tuple(item for item in DEFINITIONS if item.sample_id in G32_DEFINITIONS)
        manifest = _g32_pairs(
            definitions,
            args.repetitions,
            timeout_seconds=args.timeout,
            max_turns=args.max_turns,
        )
        print(json.dumps(manifest["summary"], indent=2, sort_keys=True))
        return 0 if manifest["summary"]["timed_out"] == 0 else 1
    if args.suite == "g3-1":
        definitions = tuple(item for item in DEFINITIONS if item.sample_id in G31_DEFINITIONS)
        manifest = _g31_pairs(
            definitions,
            args.repetitions,
            timeout_seconds=args.timeout,
            max_turns=args.max_turns,
        )
        print(json.dumps(manifest["summary"], indent=2, sort_keys=True))
        return 0 if manifest["summary"]["timed_out"] == 0 else 1
    if args.suite == "g2b1":
        definitions = tuple(item for item in DEFINITIONS if item.sample_id in G2B1_DEFINITIONS)
        manifest = _g2b1_pairs(
            definitions,
            args.repetitions,
            timeout_seconds=args.timeout,
            max_turns=args.max_turns,
        )
        print(json.dumps(manifest["summary"], indent=2, sort_keys=True))
        return 0 if manifest["summary"]["timed_out"] == 0 else 1
    if args.suite == "g2a":
        definitions = tuple(item for item in DEFINITIONS if item.sample_id in G2A_DEFINITIONS)
        manifest = _g2a_pairs(
            definitions,
            args.repetitions,
            timeout_seconds=args.timeout,
            max_turns=args.max_turns,
        )
        print(json.dumps(manifest["summary"], indent=2, sort_keys=True))
        return 0 if manifest["summary"]["timed_out"] == 0 else 1
    if args.suite == "g1":
        definitions = tuple(item for item in DEFINITIONS if item.sample_id in G1_DEFINITIONS)
        manifest = _g1_pairs(
            definitions,
            args.repetitions,
            timeout_seconds=args.timeout,
            max_turns=args.max_turns,
        )
        print(json.dumps(manifest["summary"], indent=2, sort_keys=True))
        return 0 if manifest["summary"]["timed_out"] == 0 else 1
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
        handoff_mode=args.handoff_mode,
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
