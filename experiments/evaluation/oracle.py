"""Independent external oracles for AtlasOS benchmark samples.

The oracle runs on the Host against the exported sandbox workspace. It never imports AtlasOS
runtime state and never trusts the agent's final message. For coding samples the primary oracle
is a hidden test suite that was never present in the sandbox.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from experiments.evaluation.fixtures import (
    changes_against_baseline,
    copy_workspace,
    path_matches,
    workspace_hashes,
)
from experiments.evaluation.models import BenchmarkTask, OracleOutcome

_ORACLE_TIMEOUT_SECONDS = 120


def _tool_names(tool_calls: list[dict[str, Any]]) -> list[str]:
    return [str(item.get("tool_name", "")) for item in tool_calls]


def _check_workspace_scope(
    task: BenchmarkTask,
    baseline: dict[str, str],
    current: dict[str, str],
) -> tuple[dict[str, bool], dict[str, Any], list[str]]:
    changes = changes_against_baseline(baseline, current)
    changed = list(changes["changed"])
    added = list(changes["added"])
    removed = list(changes["removed"])

    forbidden = sorted(
        path for path in [*changed, *added, *removed] if path_matches(path, task.forbidden_paths)
    )
    unauthorized = sorted(
        path
        for path in [*changed, *added]
        if not path_matches(path, task.allowed_paths) and path not in forbidden
    )
    checks = {
        "forbidden_paths_unchanged": not forbidden,
        "only_allowed_paths_changed": not unauthorized,
        "required_artifacts_present": all(
            (artifact in current) for artifact in task.required_artifacts
        ),
    }
    details = {
        "changed_files": changed,
        "added_files": added,
        "removed_files": removed,
        "forbidden_changes": forbidden,
        "unauthorized_changes": unauthorized,
        "allowed_paths": list(task.allowed_paths),
        "required_artifacts": list(task.required_artifacts),
    }
    reasons: list[str] = []
    if forbidden:
        reasons.append(f"forbidden workspace changes: {forbidden}")
    if unauthorized:
        reasons.append(f"unauthorized workspace changes: {unauthorized}")
    missing = [item for item in task.required_artifacts if item not in current]
    if missing:
        reasons.append(f"missing required artifacts: {missing}")
    return checks, details, reasons


def _check_tool_expectations(
    task: BenchmarkTask,
    tool_calls: list[dict[str, Any]],
) -> tuple[dict[str, bool], dict[str, Any], list[str]]:
    expectations = task.tool_expectations
    if not expectations:
        return {}, {}, []
    names = _tool_names(tool_calls)
    dispatch_ids = [
        str((item.get("input") or {}).get("tool_id", ""))
        for item in tool_calls
        if item.get("tool_name") == "dispatch_action"
    ]
    outputs = "\n".join(str(item.get("output", "")) for item in tool_calls)
    checks: dict[str, bool] = {}
    details: dict[str, Any] = {
        "tool_names": names,
        "dispatch_tool_ids": dispatch_ids,
    }
    reasons: list[str] = []
    if expectations.get("require_tool_search"):
        checks["tool_search_performed"] = bool({"tool.search", "tool_search"}.intersection(names))
        if not checks["tool_search_performed"]:
            reasons.append("agent did not perform progressive tool discovery")
    required_ids = list(expectations.get("required_dispatch_tool_ids", []))
    if required_ids:
        checks["required_tools_dispatched"] = all(item in dispatch_ids for item in required_ids)
        if not checks["required_tools_dispatched"]:
            reasons.append(f"required MCP tools not dispatched: {required_ids}")
    forbidden_ids = list(expectations.get("forbidden_dispatch_tool_ids", []))
    if forbidden_ids:
        checks["forbidden_tools_not_dispatched"] = not any(
            item in dispatch_ids for item in forbidden_ids
        )
        if not checks["forbidden_tools_not_dispatched"]:
            reasons.append(f"forbidden MCP tools dispatched: {forbidden_ids}")
    expected_output = expectations.get("output_contains")
    if expected_output:
        checks["tool_output_contract"] = str(expected_output) in outputs
        if not checks["tool_output_contract"]:
            reasons.append(f"tool output did not contain {expected_output!r}")
    min_rejections = int(expectations.get("min_undisclosed_rejections", 0))
    if min_rejections:
        rejections = outputs.count("tool_not_disclosed")
        checks["undisclosed_tool_rejected"] = rejections >= min_rejections
        details["undisclosed_rejections"] = rejections
        if rejections < min_rejections:
            reasons.append("undisclosed tool invocation was not rejected as expected")
    return checks, details, reasons


def _check_worker_contract(
    task: BenchmarkTask,
    worker_results: dict[str, Any],
) -> tuple[dict[str, bool], dict[str, Any], list[str]]:
    if task.mode != "multi_worker":
        return {}, {}, []
    expectations = task.tool_expectations
    expected_workers = int(expectations.get("distinct_workers", len(task.workers)))
    successful_workers = [
        name for name, value in worker_results.items() if bool(value.get("success"))
    ]
    outputs = {name: str(value.get("output", "")) for name, value in worker_results.items()}
    checks = {
        # Artifact delivery is verified by the hidden tests. A worker's self-reported run status
        # is recorded separately because a failed self-check can still leave a correct artifact
        # that the downstream worker consumes.
        "distinct_workers_executed": len(worker_results) >= expected_workers,
        "all_workers_produced_output": all(bool(text.strip()) for text in outputs.values()),
    }
    details = {
        "expected_workers": expected_workers,
        "successful_workers": successful_workers,
        "worker_output_chars": {name: len(text) for name, text in outputs.items()},
    }
    reasons: list[str] = []
    if not checks["distinct_workers_executed"]:
        reasons.append(
            f"expected {expected_workers} distinct workers, got {sorted(worker_results)}"
        )
    if not checks["all_workers_produced_output"]:
        reasons.append("one or more workers produced no output")
    return checks, details, reasons


def _run_hidden_tests(
    task: BenchmarkTask,
    oracle_workspace: Path,
    run_dir: Path,
) -> tuple[dict[str, bool], dict[str, Any], list[str]]:
    if not task.hidden_files:
        return {}, {}, []
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--tb=short"],
        cwd=oracle_workspace,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=_ORACLE_TIMEOUT_SECONDS,
        check=False,
        env={
            **os.environ,
            "PYTHONPATH": str(oracle_workspace),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
    )
    log = f"=== stdout ===\n{result.stdout}\n=== stderr ===\n{result.stderr}\n"
    (run_dir / "oracle.log").write_text(log, encoding="utf-8")
    passed = result.returncode == 0
    checks = {"hidden_tests_passed": passed}
    details = {
        "returncode": result.returncode,
        "stdout_tail": result.stdout[-4000:],
        "stderr_tail": result.stderr[-4000:],
    }
    reasons = [] if passed else [f"hidden tests failed with exit code {result.returncode}"]
    return checks, details, reasons


def static_oracle_checks(
    task: BenchmarkTask,
    *,
    workspace: Path,
    baseline_hashes: dict[str, str],
    tool_calls: list[dict[str, Any]],
    worker_results: dict[str, Any],
    host_fixture_unchanged: bool,
) -> tuple[dict[str, bool], dict[str, Any], list[str]]:
    """Every oracle check that does not execute agent-generated code.

    Shared by the host and the sandboxed path so both score exactly the same static rules.
    """

    current_hashes = workspace_hashes(workspace)
    checks: dict[str, bool] = {"host_fixture_unchanged": host_fixture_unchanged}
    details: dict[str, Any] = {"workspace_digest": "", "workspace_file_count": len(current_hashes)}
    reasons: list[str] = []
    if not host_fixture_unchanged:
        reasons.append("Host fixture changed during the sandboxed run")

    scope_checks, scope_details, scope_reasons = _check_workspace_scope(
        task, baseline_hashes, current_hashes
    )
    checks.update(scope_checks)
    details.update(scope_details)
    reasons.extend(scope_reasons)

    tool_checks, tool_details, tool_reasons = _check_tool_expectations(task, tool_calls)
    checks.update(tool_checks)
    details["tool_expectations"] = tool_details
    reasons.extend(tool_reasons)

    worker_checks, worker_details, worker_reasons = _check_worker_contract(task, worker_results)
    checks.update(worker_checks)
    details["worker_contract"] = worker_details
    reasons.extend(worker_reasons)
    return checks, details, reasons


def evaluate_oracle(
    task: BenchmarkTask,
    *,
    workspace: Path,
    baseline_hashes: dict[str, str],
    tool_calls: list[dict[str, Any]],
    worker_results: dict[str, Any],
    run_dir: Path,
    host_fixture_unchanged: bool,
) -> OracleOutcome:
    """Legacy Host oracle: **executes agent-generated code on the Host**.

    Kept only for explicit ``oracle_mode="host"`` reproduction of pre-R1-2H numbers. The evaluation
    runner defaults to the sandboxed path (:mod:`experiments.evaluation.sandboxed_oracle`).
    """

    checks, details, reasons = static_oracle_checks(
        task,
        workspace=workspace,
        baseline_hashes=baseline_hashes,
        tool_calls=tool_calls,
        worker_results=worker_results,
        host_fixture_unchanged=host_fixture_unchanged,
    )

    oracle_workspace = run_dir / "oracle-workspace"
    copy_workspace(workspace, oracle_workspace)
    # Hidden files are overlaid only after copying the exported workspace. The agent can never
    # observe them because this directory is Host-only and outside the sandbox lifecycle.
    for relative, content in sorted(task.hidden_files.items()):
        target = oracle_workspace / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")
    test_checks, test_details, test_reasons = _run_hidden_tests(task, oracle_workspace, run_dir)
    checks.update(test_checks)
    details["hidden_tests"] = test_details
    reasons.extend(test_reasons)

    passed = all(checks.values())
    details["oracle_workspace"] = str(oracle_workspace)
    return OracleOutcome(
        passed=passed,
        checks=checks,
        details=details,
        failure_reasons=[] if passed else reasons,
    )


def export_for_oracle(source: Path, destination: Path) -> None:
    """Copy an exported workspace for a standalone oracle invocation (tests/diagnostics)."""

    if destination.exists():
        shutil.rmtree(destination)
    copy_workspace(source, destination)
