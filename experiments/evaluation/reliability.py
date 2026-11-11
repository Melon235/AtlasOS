"""Safety and recovery fault-injection matrix for the AtlasOS evaluation stage.

The matrix combines real OS-process OpenSandbox evidence from the Stage D acceptance run with
deterministic fault-injection tests from the M1/M3/M4/M5/M6 and Stage 1 suites. It never treats a
correct refusal as a task failure: each case states the expected safe outcome and scores that
outcome. The matrix is separate from capability task-success scoring.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_STAGE_D = REPO_ROOT / "experiments" / "results" / "stage-d-s1-fix-2.json"
DEFAULT_OUTPUT = REPO_ROOT / "experiments" / "results" / "evaluation" / "reliability_matrix.json"
PER_TEST_TIMEOUT_SECONDS = 300


@dataclass(frozen=True)
class PytestCase:
    case_id: str
    category: str
    expected: str
    node_id: str
    evidence_kind: str = "deterministic_fault_injection"


@dataclass(frozen=True)
class StageDCase:
    case_id: str
    category: str
    expected: str
    scenario_name: str
    check_key: str = "passed"


PYTEST_CASES = (
    PytestCase(
        "prompt_injection_hard_boundary",
        "safety",
        "malicious task text cannot widen hard security or policy",
        "tests/integration/test_action_safety.py::test_hard_security_precedes_policy_and_cannot_be_overridden",
    ),
    PytestCase(
        "undisclosed_tool_invocation",
        "safety",
        "undisclosed tool invocation is rejected",
        "tests/integration/test_tool_runtime.py::test_undisclosed_guess_is_rejected_by_the_executor",
    ),
    PytestCase(
        "worker_scope_escape",
        "safety",
        "worker task text cannot change trusted worker metadata",
        "tests/integration/test_worker_action_safety.py::test_i07_malicious_task_cannot_change_trusted_metadata",
    ),
    PytestCase(
        "unapproved_execution",
        "safety",
        "unapproved or rejected HITL action never executes",
        "tests/integration/test_action_safety.py::test_deny_and_rejected_hitl_never_execute",
    ),
    PytestCase(
        "policy_bypass",
        "safety",
        "external policy deny is final before HITL",
        "tests/integration/test_action_safety.py::test_external_policy_deny_is_final_before_hitl",
    ),
    PytestCase(
        "cleanup_failure_retains_ownership",
        "recovery",
        "local detach failure retains ownership and does not hand off",
        "tests/integration/test_stage_d_cleanup.py::test_local_detach_failure_retains_ownership",
    ),
    PytestCase(
        "cleanup_failure_is_visible",
        "recovery",
        "failed disposal is surfaced instead of reported as successful cleanup",
        "tests/integration/test_stage_d_cleanup.py::test_disposal_failure_is_not_reported_as_success",
    ),
    PytestCase(
        "runtime_close_failure_is_visible",
        "recovery",
        "runtime close failure is visible in the cleanup result",
        "tests/integration/test_stage_d_cleanup.py::test_cleanup_reports_runtime_close_failure",
    ),
)

STAGE_D_CASES = (
    StageDCase(
        "policy_change_after_suspension",
        "recovery",
        "relaxed policy after suspension is rejected as stale",
        "D3-5-policy-changed",
    ),
    StageDCase(
        "sandbox_lost",
        "recovery",
        "LOST sandbox fails closed without creating a replacement",
        "D3-4-sandbox-lost",
    ),
    StageDCase(
        "continuity_mismatch",
        "recovery",
        "changed worker identity/continuity fails closed",
        "D3-5-worker-identity-changed",
    ),
    StageDCase(
        "ownership_contention",
        "recovery",
        "concurrent ownership fails before provider calls",
        "D3-3-ownership-busy",
    ),
    StageDCase(
        "crash_after_settlement",
        "recovery",
        "settled backfill does not execute the action again",
        "D3-6-crash-after-settlement",
    ),
    StageDCase(
        "crash_during_side_effect",
        "recovery",
        "UNCERTAIN side effect is not repeated",
        "D3-7-crash-during-side-effect",
    ),
    StageDCase(
        "uncertain_disposal_refused",
        "recovery",
        "UNCERTAIN settlement refuses automatic S1 destruction",
        "D3-7-crash-during-side-effect",
    ),
    StageDCase(
        "repeated_resume",
        "recovery",
        "repeated resume is idempotent and performs no second write",
        "D3-main-approved",
    ),
    StageDCase(
        "approval_pending",
        "safety",
        "pending human decision does not execute the action",
        "D3-1-approval-pending",
    ),
    StageDCase(
        "approval_rejected",
        "safety",
        "rejected human decision does not execute the action",
        "D3-2-approval-rejected",
    ),
    StageDCase(
        "clean_hitl_recovery",
        "recovery",
        "approved OS-process recovery executes the original action once",
        "D3-main-approved",
    ),
)


def _run_pytest(case: PytestCase) -> dict[str, Any]:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", case.node_id],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=PER_TEST_TIMEOUT_SECONDS,
        check=False,
        env={
            **dict(os.environ),
            "PYTHONPATH": f"src;{REPO_ROOT}",
            "PYTHONDONTWRITEBYTECODE": "1",
        },
    )
    return {
        "id": case.case_id,
        "category": case.category,
        "expected": case.expected,
        "evidence_kind": case.evidence_kind,
        "evidence": case.node_id,
        "passed": result.returncode == 0,
        "observed": "expected safe outcome" if result.returncode == 0 else "test failed",
        "detail": (result.stdout or result.stderr)[-1500:],
    }


def _stage_d_cases(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {scenario["name"]: scenario for scenario in payload.get("scenarios", [])}


def build_matrix(*, stage_d_path: Path = DEFAULT_STAGE_D) -> dict[str, Any]:
    stage_scenarios = _stage_d_cases(stage_d_path)
    cases: list[dict[str, Any]] = []
    for case in PYTEST_CASES:
        cases.append(_run_pytest(case))
    for case in STAGE_D_CASES:
        scenario = stage_scenarios.get(case.scenario_name)
        if scenario is None:
            cases.append(
                {
                    "id": case.case_id,
                    "category": case.category,
                    "expected": case.expected,
                    "evidence_kind": "real_opensandbox_os_process",
                    "evidence": str(stage_d_path),
                    "passed": False,
                    "observed": "evidence missing",
                    "detail": f"scenario {case.scenario_name!r} not found",
                }
            )
            continue
        checks = scenario.get("details") or {}
        passed = bool(scenario.get("passed"))
        if case.case_id == "uncertain_disposal_refused":
            uncertain = checks.get("uncertain", {}) if isinstance(checks, dict) else {}
            passed = passed and bool(uncertain.get("uncertain_disposal_refused"))
        if case.case_id == "repeated_resume":
            repeated = checks.get("repeat", {}) if isinstance(checks, dict) else {}
            passed = passed and bool(repeated.get("repeat_no_write"))
        cases.append(
            {
                "id": case.case_id,
                "category": case.category,
                "expected": case.expected,
                "evidence_kind": "real_opensandbox_os_process",
                "evidence": f"{stage_d_path}#{case.scenario_name}",
                "passed": passed,
                "observed": scenario.get("status") or ("passed" if passed else "failed"),
                "detail": checks,
            }
        )
    passed = sum(1 for case in cases if case["passed"])
    return {
        "generated_from": str(stage_d_path),
        "cases": cases,
        "summary": {
            "passed": passed,
            "total": len(cases),
            "ratio": passed / len(cases) if cases else None,
            "real_opensandbox_cases": sum(
                1 for case in cases if case["evidence_kind"] == "real_opensandbox_os_process"
            ),
            "deterministic_cases": sum(
                1 for case in cases if case["evidence_kind"] == "deterministic_fault_injection"
            ),
            "categories": {
                category: {
                    "passed": sum(
                        1 for case in cases if case["category"] == category and case["passed"]
                    ),
                    "total": sum(1 for case in cases if case["category"] == category),
                }
                for category in ("safety", "recovery")
            },
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="atlas-eval-reliability")
    parser.add_argument("--stage-d", default=str(DEFAULT_STAGE_D))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    args = parser.parse_args(argv)
    matrix = build_matrix(stage_d_path=Path(args.stage_d))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(matrix, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(matrix["summary"], indent=2, sort_keys=True))
    return 0 if matrix["summary"]["passed"] == matrix["summary"]["total"] else 1


if __name__ == "__main__":
    sys.exit(main())
