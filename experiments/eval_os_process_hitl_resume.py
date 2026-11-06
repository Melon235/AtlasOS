"""Stage D acceptance: real OS-process M6 HITL recovery over S1.

No LLM is called. Process A creates a real OpenSandbox S1 and a durable pre-execution M6
suspension. The evaluator kills Process A with the OS process API, a separate reviewer process
resolves the durable ApprovalStore challenge, and Process B/C reconstruct the trusted runtime in
new Python processes. Recovery delegates to the existing M6 settlement path and M5 reattachment.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from atlasos.sandbox import WorkloadOwnershipLock

REPO_ROOT = Path(__file__).resolve().parents[1]
CHILD = REPO_ROOT / "experiments" / "stage_d_process.py"
FIXTURE = REPO_ROOT / "experiments" / "fixtures" / "uat_repo"
CONFIG = REPO_ROOT / "experiments" / "configs" / "stage-d-hitl.yaml"
DEFAULT_RUNS_ROOT = REPO_ROOT / "experiments" / "runs" / "stage-d"


@dataclass
class Scenario:
    name: str
    passed: bool
    a_pid: int | None = None
    b_pid: int | None = None
    c_pid: int | None = None
    sandbox_id: str | None = None
    binding_id: str | None = None
    continuity_digest: str | None = None
    status: str | None = None
    details: dict[str, Any] | None = None
    error: str | None = None


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def _read_json(path: Path, timeout: float = 90.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while not path.is_file():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {path}")
        time.sleep(0.05)
    return json.loads(path.read_text(encoding="utf-8"))


def _read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _policy_payload(*, write: str = "REQUIRE_HUMAN") -> dict[str, Any]:
    return {
        "name": "stage-d",
        "default": "DENY",
        "rules": [
            {"id": "allow-search", "decision": "ALLOW", "action": "control.search"},
            {"id": "allow-dispatch", "decision": "ALLOW", "action": "control.dispatch"},
            {"id": "allow-read", "decision": "ALLOW", "action": "filesystem.read"},
            {"id": "allow-shell", "decision": "ALLOW", "action": "shell.execute"},
            {"id": "write", "decision": write, "action": "filesystem.write"},
        ],
    }


def _write_policy(path: Path, *, write: str = "REQUIRE_HUMAN") -> None:
    path.write_text(json.dumps(_policy_payload(write=write)), encoding="utf-8")


def _stage_fixture(destination: Path) -> Path:
    if destination.is_dir():
        return destination
    destination.mkdir(parents=True)
    for entry in FIXTURE.iterdir():
        if entry.name in {".git", "__pycache__", ".pytest_cache"}:
            continue
        target = destination / entry.name
        if entry.is_dir():
            shutil.copytree(
                entry,
                target,
                ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"),
            )
        else:
            shutil.copy2(entry, target)
    commands = (
        ["git", "init", "-q"],
        ["git", "config", "core.autocrlf", "false"],
        ["git", "config", "core.safecrlf", "false"],
        ["git", "add", "-A"],
        [
            "git",
            "-c",
            "user.email=stage-d@example.test",
            "-c",
            "user.name=StageD",
            "commit",
            "-qm",
            "stage d",
        ],
    )
    for command in commands:
        result = subprocess.run(
            command,
            cwd=destination,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        if result.returncode != 0:
            raise AssertionError(f"cannot stage fixture: {result.stderr or result.stdout}")
    return destination


def _env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO_ROOT / "src"), str(REPO_ROOT), env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    return env


def _base_args(
    mode: str,
    *,
    state_root: Path,
    run_id: str,
    task_id: str,
    worker_id: str,
    policy: Path,
    source_repo: Path,
    **extra: Any,
) -> list[str]:
    args = [
        sys.executable,
        "-u",
        str(CHILD),
        "--mode",
        mode,
        "--state-root",
        str(state_root),
        "--run-id",
        run_id,
        "--task-id",
        task_id,
        "--worker-id",
        worker_id,
        "--profile-id",
        "coding_worker",
        "--principal",
        "user:stage-d",
        "--tenant",
        "stage-d",
        "--config",
        str(CONFIG),
        "--policy",
        str(policy),
        "--source-repo",
        str(source_repo),
    ]
    for key, value in extra.items():
        flag = f"--{key.replace('_', '-')}"
        if isinstance(value, bool):
            if value:
                args.append(flag)
        else:
            args.extend([flag, str(value)])
    return args


def _launch(args: list[str]) -> subprocess.Popen[str]:
    return subprocess.Popen(
        args,
        cwd=REPO_ROOT,
        env=_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _kill(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        process.kill()
    with contextlib.suppress(Exception):
        process.wait(timeout=30)


def _wait_lock_free(state_root: Path, *, run_id: str, task_id: str) -> None:
    lock = WorkloadOwnershipLock(state_root, run_id=run_id, task_id=task_id)
    lock.acquire(timeout_seconds=30.0)
    lock.release()


def _wait_lease_expired(state_root: Path, suspension_id: str) -> None:
    db = state_root / "suspension.db"
    deadline = time.monotonic() + 30.0
    while True:
        with sqlite3.connect(db) as connection:
            row = connection.execute(
                "SELECT payload FROM execution_suspensions WHERE suspension_id=?",
                (suspension_id,),
            ).fetchone()
        if row is None:
            raise AssertionError("suspension disappeared while waiting for lease")
        payload = json.loads(row[0])
        lease = payload.get("settling_lease_until")
        if lease is None or datetime.fromisoformat(lease) <= datetime.now(UTC):
            return
        if time.monotonic() > deadline:
            raise AssertionError("settlement lease did not expire")
        time.sleep(0.05)


def _run_reviewer(
    state_root: Path,
    *,
    run_id: str,
    task_id: str,
    worker_id: str,
    policy: Path,
    source_repo: Path,
    suspension_id: str,
    decision: str,
) -> dict[str, Any]:
    output = state_root / f"review-{decision.lower()}.json"
    mode = "review-approve" if decision == "APPROVED" else "review-reject"
    process = _launch(
        _base_args(
            mode,
            state_root=state_root,
            run_id=run_id,
            task_id=task_id,
            worker_id=worker_id,
            policy=policy,
            source_repo=source_repo,
            suspension_id=suspension_id,
            output=str(output),
            pid_file=str(state_root / f"review-{decision.lower()}.pid"),
        )
    )
    stdout, stderr = process.communicate(timeout=90)
    del stdout
    if process.returncode != 0:
        raise AssertionError(f"reviewer failed: {stderr}")
    return _read_json(output)


def _run_recovery(
    state_root: Path,
    *,
    run_id: str,
    task_id: str,
    worker_id: str,
    policy: Path,
    source_repo: Path,
    suspension_id: str,
    dispose: bool,
    lease_seconds: float = 60.0,
    **extra: Any,
) -> tuple[dict[str, Any], int]:
    output = state_root / f"recover-{time.time_ns()}.json"
    pid_file = output.with_suffix(".pid")
    args = _base_args(
        "recover",
        state_root=state_root,
        run_id=run_id,
        task_id=task_id,
        worker_id=worker_id,
        policy=policy,
        source_repo=source_repo,
        suspension_id=suspension_id,
        output=str(output),
        pid_file=str(pid_file),
        dispose=dispose,
        lease_seconds=lease_seconds,
        **extra,
    )
    process = _launch(args)
    stdout, stderr = process.communicate(timeout=240)
    del stdout
    if process.returncode not in (0, 1):
        raise AssertionError(f"recovery process rc={process.returncode}: {stderr}")
    payload = _read_json(output, timeout=5) if output.is_file() else {}
    if payload and "pid" not in payload:
        payload["pid"] = _read_pid(pid_file)
    return payload, process.returncode


def _cleanup_remote(
    state_root: Path,
    *,
    run_id: str,
    task_id: str,
    worker_id: str,
    policy: Path,
    source_repo: Path,
) -> dict[str, Any]:
    output = state_root / "cleanup-remote.json"
    process = _launch(
        _base_args(
            "cleanup-remote",
            state_root=state_root,
            run_id=run_id,
            task_id=task_id,
            worker_id=worker_id,
            policy=policy,
            source_repo=source_repo,
            output=str(output),
            pid_file=str(state_root / "cleanup-remote.pid"),
        )
    )
    process.communicate(timeout=120)
    if not output.is_file():
        raise AssertionError("cleanup child did not write output")
    return _read_json(output)


def _start_and_suspend(
    state_root: Path,
    *,
    run_id: str,
    task_id: str,
    worker_id: str,
    policy: Path,
    source_repo: Path,
) -> tuple[subprocess.Popen[str], dict[str, Any], int]:
    ready = state_root / "process-a-ready.json"
    pid_file = state_root / "process-a.pid"
    process = _launch(
        _base_args(
            "process-a",
            state_root=state_root,
            run_id=run_id,
            task_id=task_id,
            worker_id=worker_id,
            policy=policy,
            source_repo=source_repo,
            ready=str(ready),
            pid_file=str(pid_file),
        )
    )
    try:
        payload = _read_json(ready, timeout=180)
    except BaseException:
        _kill(process)
        raise
    if process.poll() is not None:
        stderr = process.stderr.read() if process.stderr else ""
        raise AssertionError(f"Process A exited before ready: {stderr}")
    return process, payload, int(payload["pid"])


def _new_state(root: Path, name: str) -> tuple[Path, str, str, str, Path]:
    state = root / name
    state.mkdir(parents=True, exist_ok=True)
    run_id = f"run-{name}"
    task_id = f"task-{name}"
    worker_id = f"worker-{name}"
    policy = state / "policy.json"
    _write_policy(policy)
    return state, run_id, task_id, worker_id, policy


def _common(state_root: Path, name: str, source_repo: Path):
    state, run_id, task_id, worker_id, policy = _new_state(state_root, name)
    a, ready, a_pid = _start_and_suspend(
        state,
        run_id=run_id,
        task_id=task_id,
        worker_id=worker_id,
        policy=policy,
        source_repo=source_repo,
    )
    return state, run_id, task_id, worker_id, policy, a, ready, a_pid


def _scenario_main(root: Path, source_repo: Path) -> Scenario:
    state, run_id, task_id, worker_id, policy, a, ready, a_pid = _common(root, "main", source_repo)
    _kill(a)
    _wait_lock_free(state, run_id=run_id, task_id=task_id)
    _run_reviewer(
        state,
        run_id=run_id,
        task_id=task_id,
        worker_id=worker_id,
        policy=policy,
        source_repo=source_repo,
        suspension_id=ready["suspension_id"],
        decision="APPROVED",
    )
    recovered, rc = _run_recovery(
        state,
        run_id=run_id,
        task_id=task_id,
        worker_id=worker_id,
        policy=policy,
        source_repo=source_repo,
        suspension_id=ready["suspension_id"],
        dispose=True,
    )
    checks = {
        "recovery_rc_zero": rc == 0,
        "outcome_settled": recovered.get("outcome_status") == "settled",
        "suspension_cleared": recovered.get("suspension_state") == "CLEARED",
        "same_sandbox": recovered.get("sandbox_id") == ready["sandbox_id"],
        "same_binding": recovered.get("binding_id") == ready["binding_id"],
        "same_continuity": recovered.get("continuity_digest") == ready["continuity_digest"],
        "recovery_create_zero": recovered.get("provider_counts", {}).get("create") == 0,
        "recovery_attach_once": recovered.get("provider_counts", {}).get("attach_existing") == 1,
        "write_effect_once": recovered.get("target_content") == "approved recovery\n",
        "settlement_executed_once": [
            item["outcome"] for item in recovered.get("settlement_results", [])
        ]
        == ["EXECUTED"],
        "ledger_completed": recovered.get("ledger")
        == [{"tool_name": "file_write", "state": "COMPLETED"}],
        "continuation_present": recovered.get("continuation_present") is True,
        "sandbox_destroyed": recovered.get("disposed") is True,
        "pid_distinct": a_pid != recovered.get("pid"),
    }
    if not all(checks.values()):
        raise AssertionError(f"main checks failed: {checks}")
    repeated, repeat_rc = _run_recovery(
        state,
        run_id=run_id,
        task_id=task_id,
        worker_id=worker_id,
        policy=policy,
        source_repo=source_repo,
        suspension_id=ready["suspension_id"],
        dispose=True,
    )
    repeat_checks = {
        "repeat_rc_zero": repeat_rc == 0,
        "repeat_cleared": repeated.get("outcome_status") == "cleared",
        "repeat_no_create": repeated.get("provider_counts", {}).get("create") == 0,
        "repeat_no_write": repeated.get("provider_counts", {}).get("write_file") == 0,
        "repeat_no_inspect": repeated.get("provider_counts", {}).get("inspect_existing") == 0,
    }
    if not all(repeat_checks.values()):
        raise AssertionError(f"repeat checks failed: {repeat_checks}")
    return Scenario(
        name="D3-main-approved",
        passed=True,
        a_pid=a_pid,
        b_pid=recovered.get("pid"),
        sandbox_id=ready["sandbox_id"],
        binding_id=ready["binding_id"],
        continuity_digest=ready["continuity_digest"],
        status=recovered.get("outcome_status"),
        details={"main": checks, "repeat": repeat_checks, "repeated": repeated},
    )


def _scenario_pending(root: Path, source_repo: Path) -> Scenario:
    state, run_id, task_id, worker_id, policy, a, ready, a_pid = _common(
        root, "pending", source_repo
    )
    _kill(a)
    _wait_lock_free(state, run_id=run_id, task_id=task_id)
    pending, _ = _run_recovery(
        state,
        run_id=run_id,
        task_id=task_id,
        worker_id=worker_id,
        policy=policy,
        source_repo=source_repo,
        suspension_id=ready["suspension_id"],
        dispose=False,
    )
    counts = pending.get("provider_counts", {})
    checks = {
        "pending_waiting": pending.get("outcome_status") == "waiting",
        "pending_no_inspect": counts.get("inspect_existing") == 0,
        "pending_no_attach": counts.get("attach_existing") == 0,
        "pending_no_create": counts.get("create") == 0,
        "pending_no_write": counts.get("write_file") == 0,
    }
    if not all(checks.values()):
        raise AssertionError(f"pending checks failed: {checks}")
    _run_reviewer(
        state,
        run_id=run_id,
        task_id=task_id,
        worker_id=worker_id,
        policy=policy,
        source_repo=source_repo,
        suspension_id=ready["suspension_id"],
        decision="APPROVED",
    )
    recovered, rc = _run_recovery(
        state,
        run_id=run_id,
        task_id=task_id,
        worker_id=worker_id,
        policy=policy,
        source_repo=source_repo,
        suspension_id=ready["suspension_id"],
        dispose=True,
    )
    if rc != 0 or recovered.get("target_content") != "approved recovery\n":
        raise AssertionError("pending scenario did not recover after approval")
    return Scenario(
        name="D3-1-approval-pending",
        passed=True,
        a_pid=a_pid,
        b_pid=recovered.get("pid"),
        sandbox_id=ready["sandbox_id"],
        binding_id=ready["binding_id"],
        continuity_digest=ready["continuity_digest"],
        status="waiting_then_settled",
        details={"pending": checks},
    )


def _scenario_rejected(root: Path, source_repo: Path) -> Scenario:
    state, run_id, task_id, worker_id, policy, a, ready, a_pid = _common(
        root, "rejected", source_repo
    )
    _kill(a)
    _wait_lock_free(state, run_id=run_id, task_id=task_id)
    _run_reviewer(
        state,
        run_id=run_id,
        task_id=task_id,
        worker_id=worker_id,
        policy=policy,
        source_repo=source_repo,
        suspension_id=ready["suspension_id"],
        decision="REJECTED",
    )
    recovered, _ = _run_recovery(
        state,
        run_id=run_id,
        task_id=task_id,
        worker_id=worker_id,
        policy=policy,
        source_repo=source_repo,
        suspension_id=ready["suspension_id"],
        dispose=True,
    )
    checks = {
        "rejected_settled": recovered.get("outcome_status") == "settled",
        "rejected_no_target": recovered.get("target_content") is None,
        "rejected_no_write": recovered.get("provider_counts", {}).get("write_file") == 0,
        "rejected_no_ledger": recovered.get("ledger") == [],
    }
    if not all(checks.values()):
        raise AssertionError(f"rejected checks failed: {checks}")
    return Scenario(
        name="D3-2-approval-rejected",
        passed=True,
        a_pid=a_pid,
        b_pid=recovered.get("pid"),
        sandbox_id=ready["sandbox_id"],
        binding_id=ready["binding_id"],
        continuity_digest=ready["continuity_digest"],
        status=recovered.get("outcome_status"),
        details={"rejected": checks},
    )


def _scenario_busy(root: Path, source_repo: Path) -> Scenario:
    state, run_id, task_id, worker_id, policy, a, ready, a_pid = _common(root, "busy", source_repo)
    _run_reviewer(
        state,
        run_id=run_id,
        task_id=task_id,
        worker_id=worker_id,
        policy=policy,
        source_repo=source_repo,
        suspension_id=ready["suspension_id"],
        decision="APPROVED",
    )
    busy, _ = _run_recovery(
        state,
        run_id=run_id,
        task_id=task_id,
        worker_id=worker_id,
        policy=policy,
        source_repo=source_repo,
        suspension_id=ready["suspension_id"],
        dispose=False,
    )
    _kill(a)
    _wait_lock_free(state, run_id=run_id, task_id=task_id)
    _cleanup_remote(
        state,
        run_id=run_id,
        task_id=task_id,
        worker_id=worker_id,
        policy=policy,
        source_repo=source_repo,
    )
    counts = busy.get("provider_counts", {})
    checks = {
        "busy_failed": busy.get("status") == "ERROR",
        "busy_type": busy.get("error_type") == "SandboxOwnershipBusy",
        "busy_no_inspect": counts.get("inspect_existing") == 0,
        "busy_no_attach": counts.get("attach_existing") == 0,
        "busy_no_create": counts.get("create") == 0,
        "busy_no_write": counts.get("write_file") == 0,
    }
    if not all(checks.values()):
        raise AssertionError(f"busy checks failed: {checks}; busy={busy}")
    return Scenario(
        name="D3-3-ownership-busy",
        passed=True,
        a_pid=a_pid,
        b_pid=busy.get("pid"),
        sandbox_id=ready["sandbox_id"],
        binding_id=ready["binding_id"],
        continuity_digest=ready["continuity_digest"],
        status="BUSY",
        details={"busy": checks},
    )


def _scenario_lost(root: Path, source_repo: Path) -> Scenario:
    state, run_id, task_id, worker_id, policy, a, ready, a_pid = _common(root, "lost", source_repo)
    _kill(a)
    _wait_lock_free(state, run_id=run_id, task_id=task_id)
    _cleanup_remote(
        state,
        run_id=run_id,
        task_id=task_id,
        worker_id=worker_id,
        policy=policy,
        source_repo=source_repo,
    )
    _run_reviewer(
        state,
        run_id=run_id,
        task_id=task_id,
        worker_id=worker_id,
        policy=policy,
        source_repo=source_repo,
        suspension_id=ready["suspension_id"],
        decision="APPROVED",
    )
    lost, _ = _run_recovery(
        state,
        run_id=run_id,
        task_id=task_id,
        worker_id=worker_id,
        policy=policy,
        source_repo=source_repo,
        suspension_id=ready["suspension_id"],
        dispose=False,
    )
    counts = lost.get("provider_counts", {})
    checks = {
        "lost_failed": lost.get("status") == "ERROR",
        "lost_type": lost.get("error_type") == "SandboxLostError",
        "lost_no_create": counts.get("create") == 0,
        "lost_no_write": counts.get("write_file") == 0,
    }
    if not all(checks.values()):
        raise AssertionError(f"lost checks failed: {checks}; lost={lost}")
    return Scenario(
        name="D3-4-sandbox-lost",
        passed=True,
        a_pid=a_pid,
        b_pid=lost.get("pid"),
        sandbox_id=ready["sandbox_id"],
        binding_id=ready["binding_id"],
        continuity_digest=ready["continuity_digest"],
        status="LOST",
        details={"lost": checks},
    )


def _scenario_policy(root: Path, source_repo: Path) -> Scenario:
    state, run_id, task_id, worker_id, policy, a, ready, a_pid = _common(
        root, "policy", source_repo
    )
    _kill(a)
    _wait_lock_free(state, run_id=run_id, task_id=task_id)
    _run_reviewer(
        state,
        run_id=run_id,
        task_id=task_id,
        worker_id=worker_id,
        policy=policy,
        source_repo=source_repo,
        suspension_id=ready["suspension_id"],
        decision="APPROVED",
    )
    _write_policy(policy, write="ALLOW")
    changed, _ = _run_recovery(
        state,
        run_id=run_id,
        task_id=task_id,
        worker_id=worker_id,
        policy=policy,
        source_repo=source_repo,
        suspension_id=ready["suspension_id"],
        dispose=False,
    )
    _cleanup_remote(
        state,
        run_id=run_id,
        task_id=task_id,
        worker_id=worker_id,
        policy=policy,
        source_repo=source_repo,
    )
    checks = {
        "policy_stale": changed.get("outcome_status") == "stale",
        "policy_no_write": changed.get("provider_counts", {}).get("write_file") == 0,
        "policy_no_create": changed.get("provider_counts", {}).get("create") == 0,
    }
    if not all(checks.values()):
        raise AssertionError(f"policy checks failed: {checks}; changed={changed}")
    return Scenario(
        name="D3-5-policy-changed",
        passed=True,
        a_pid=a_pid,
        b_pid=changed.get("pid"),
        sandbox_id=ready["sandbox_id"],
        binding_id=ready["binding_id"],
        continuity_digest=ready["continuity_digest"],
        status="STALE",
        details={"policy": checks},
    )


def _scenario_identity(root: Path, source_repo: Path) -> Scenario:
    state, run_id, task_id, worker_id, policy, a, ready, a_pid = _common(
        root, "identity", source_repo
    )
    _kill(a)
    _wait_lock_free(state, run_id=run_id, task_id=task_id)
    _run_reviewer(
        state,
        run_id=run_id,
        task_id=task_id,
        worker_id=worker_id,
        policy=policy,
        source_repo=source_repo,
        suspension_id=ready["suspension_id"],
        decision="APPROVED",
    )
    mismatch, _ = _run_recovery(
        state,
        run_id=run_id,
        task_id=task_id,
        worker_id="wrong-worker-identity",
        policy=policy,
        source_repo=source_repo,
        suspension_id=ready["suspension_id"],
        dispose=False,
    )
    _cleanup_remote(
        state,
        run_id=run_id,
        task_id=task_id,
        worker_id=worker_id,
        policy=policy,
        source_repo=source_repo,
    )
    counts = mismatch.get("provider_counts", {})
    checks = {
        "identity_failed": mismatch.get("status") == "ERROR",
        "identity_type": mismatch.get("error_type") == "TrustedRecoveryError",
        "identity_no_inspect": counts.get("inspect_existing") == 0,
        "identity_no_attach": counts.get("attach_existing") == 0,
        "identity_no_create": counts.get("create") == 0,
        "identity_no_write": counts.get("write_file") == 0,
    }
    if not all(checks.values()):
        raise AssertionError(f"identity checks failed: {checks}; mismatch={mismatch}")
    return Scenario(
        name="D3-5-worker-identity-changed",
        passed=True,
        a_pid=a_pid,
        b_pid=mismatch.get("pid"),
        sandbox_id=ready["sandbox_id"],
        binding_id=ready["binding_id"],
        continuity_digest=ready["continuity_digest"],
        status="IDENTITY_MISMATCH",
        details={"identity": checks},
    )


def _scenario_settlement_crash(root: Path, source_repo: Path) -> Scenario:
    state, run_id, task_id, worker_id, policy, a, ready, a_pid = _common(
        root, "settlement-crash", source_repo
    )
    _kill(a)
    _wait_lock_free(state, run_id=run_id, task_id=task_id)
    _run_reviewer(
        state,
        run_id=run_id,
        task_id=task_id,
        worker_id=worker_id,
        policy=policy,
        source_repo=source_repo,
        suspension_id=ready["suspension_id"],
        decision="APPROVED",
    )
    output = state / "crash-recover.json"
    pid_file = state / "crash-recover.pid"
    process = _launch(
        _base_args(
            "recover",
            state_root=state,
            run_id=run_id,
            task_id=task_id,
            worker_id=worker_id,
            policy=policy,
            source_repo=source_repo,
            suspension_id=ready["suspension_id"],
            output=str(output),
            pid_file=str(pid_file),
            crash_after_settlement=True,
            lease_seconds=60,
        )
    )
    process.communicate(timeout=180)
    b_pid = _read_pid(pid_file)
    if process.returncode != 70:
        raise AssertionError(f"settlement-crash process rc={process.returncode}")
    _wait_lock_free(state, run_id=run_id, task_id=task_id)
    backfill, _ = _run_recovery(
        state,
        run_id=run_id,
        task_id=task_id,
        worker_id=worker_id,
        policy=policy,
        source_repo=source_repo,
        suspension_id=ready["suspension_id"],
        dispose=True,
    )
    checks = {
        "backfill_settled": backfill.get("outcome_status") == "settled",
        "backfill_no_write": backfill.get("provider_counts", {}).get("write_file") == 0,
        "backfill_no_create": backfill.get("provider_counts", {}).get("create") == 0,
        "backfill_continuation": backfill.get("continuation_present") is True,
        "backfill_target": backfill.get("target_content") == "approved recovery\n",
    }
    if not all(checks.values()):
        raise AssertionError(f"settlement-crash checks failed: {checks}; {backfill}")
    return Scenario(
        name="D3-6-crash-after-settlement",
        passed=True,
        a_pid=a_pid,
        b_pid=b_pid,
        c_pid=backfill.get("pid"),
        sandbox_id=ready["sandbox_id"],
        binding_id=ready["binding_id"],
        continuity_digest=ready["continuity_digest"],
        status="SETTLED_BACKFILL",
        details={"backfill": checks},
    )


def _scenario_execution_crash(root: Path, source_repo: Path) -> Scenario:
    state, run_id, task_id, worker_id, policy, a, ready, a_pid = _common(
        root, "execution-crash", source_repo
    )
    _kill(a)
    _wait_lock_free(state, run_id=run_id, task_id=task_id)
    _run_reviewer(
        state,
        run_id=run_id,
        task_id=task_id,
        worker_id=worker_id,
        policy=policy,
        source_repo=source_repo,
        suspension_id=ready["suspension_id"],
        decision="APPROVED",
    )
    signal = state / "side-effect-entered.json"
    output = state / "uncertain-recover.json"
    pid_file = state / "uncertain-recover.pid"
    process = _launch(
        _base_args(
            "recover",
            state_root=state,
            run_id=run_id,
            task_id=task_id,
            worker_id=worker_id,
            policy=policy,
            source_repo=source_repo,
            suspension_id=ready["suspension_id"],
            output=str(output),
            pid_file=str(pid_file),
            fault_write_block=True,
            fault_signal=str(signal),
            lease_seconds=1.0,
        )
    )
    deadline = time.monotonic() + 180.0
    while not signal.is_file():
        if process.poll() is not None:
            stderr = process.stderr.read() if process.stderr else ""
            raise AssertionError(f"fault-write process exited before side effect: {stderr}")
        if time.monotonic() > deadline:
            _kill(process)
            raise AssertionError("timed out waiting for fault-write side effect")
        time.sleep(0.05)
    b_pid = _read_pid(pid_file)
    _kill(process)
    _wait_lock_free(state, run_id=run_id, task_id=task_id)
    _wait_lease_expired(state, ready["suspension_id"])
    backfill, _ = _run_recovery(
        state,
        run_id=run_id,
        task_id=task_id,
        worker_id=worker_id,
        policy=policy,
        source_repo=source_repo,
        suspension_id=ready["suspension_id"],
        dispose=True,
    )
    outcomes = [item.get("outcome") for item in backfill.get("settlement_results", [])]
    refusal = backfill.get("disposal_refused") or ""
    checks = {
        "uncertain_settled": backfill.get("outcome_status") == "settled",
        "uncertain_outcome": outcomes == ["UNCERTAIN"],
        "uncertain_no_second_write": backfill.get("provider_counts", {}).get("write_file") == 0,
        "uncertain_no_create": backfill.get("provider_counts", {}).get("create") == 0,
        "uncertain_target": backfill.get("target_content") == "approved recovery\n",
        # S1.1: an UNCERTAIN settlement must not be destroyed by the recovery wrapper. The child
        # records the refusal instead of turning it into a successful cleanup.
        "uncertain_disposal_refused": backfill.get("disposed") is False and "UNCERTAIN" in refusal,
        "uncertain_no_destroy": backfill.get("provider_counts", {}).get("destroy") == 0,
        "uncertain_ownership_retained": "ownership_unresolved=True"
        in (backfill.get("disposal_cleanup") or ""),
    }
    # The evaluator explicitly removes the residual S1 only after recording the refused automatic
    # disposal. This is an operator cleanup step, not recovery success.
    cleanup = _cleanup_remote(
        state,
        run_id=run_id,
        task_id=task_id,
        worker_id=worker_id,
        policy=policy,
        source_repo=source_repo,
    )
    checks["operator_cleanup"] = cleanup.get("status") == "DESTROYED" and bool(
        cleanup.get("cleanup")
    )
    if not all(checks.values()):
        raise AssertionError(f"execution-crash checks failed: {checks}; {backfill}; {cleanup}")
    return Scenario(
        name="D3-7-crash-during-side-effect",
        passed=True,
        a_pid=a_pid,
        b_pid=b_pid,
        c_pid=backfill.get("pid"),
        sandbox_id=ready["sandbox_id"],
        binding_id=ready["binding_id"],
        continuity_digest=ready["continuity_digest"],
        status="UNCERTAIN_DISPOSAL_REFUSED",
        details={"uncertain": checks, "operator_cleanup": cleanup},
    )


def run(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    load_dotenv(REPO_ROOT / ".env", override=False)
    root = Path(args.runs_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    source_repo = _stage_fixture(root / "source-repo")
    scenarios: list[Scenario] = []
    for name, function in (
        ("main", _scenario_main),
        ("pending", _scenario_pending),
        ("rejected", _scenario_rejected),
        ("busy", _scenario_busy),
        ("lost", _scenario_lost),
        ("policy", _scenario_policy),
        ("identity", _scenario_identity),
        ("settlement-crash", _scenario_settlement_crash),
        ("execution-crash", _scenario_execution_crash),
    ):
        scenario_root = root / name
        try:
            scenarios.append(function(scenario_root, source_repo))
        except BaseException as error:  # noqa: BLE001 - evaluator surfaces every scenario
            scenarios.append(
                Scenario(
                    name=f"stage-d-{name}",
                    passed=False,
                    status="FAIL",
                    error=f"{type(error).__name__}: {error}",
                )
            )
    payload = {
        "experiment": "V2-stage-d-os-process-hitl-resume",
        "timestamp": datetime.now(UTC).isoformat(),
        "python": sys.version,
        "provider": "opensandbox",
        "scenarios": [asdict(item) for item in scenarios],
        "all_passed": all(item.passed for item in scenarios),
        "status": "PASS" if all(item.passed for item in scenarios) else "FAIL",
    }
    _write_json(Path(args.output), payload)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return payload, 0 if payload["all_passed"] else 1


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs-root", default=str(DEFAULT_RUNS_ROOT))
    parser.add_argument("--output", default="")
    args = parser.parse_args()
    args.output = Path(args.output) if args.output else Path(args.runs_root) / "stage-d-result.json"
    _, code = run(args)
    raise SystemExit(code)


if __name__ == "__main__":
    main()
