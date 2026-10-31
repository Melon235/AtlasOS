"""M6 P01-P04: real independent-process SQLite approval verification."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKER = REPO_ROOT / "tests" / "helpers" / "m6_approval_process_worker.py"
APPROVAL_ID = "e" * 64


def _args(db: Path, mode: str, **overrides: Any) -> list[str]:
    values = {
        "--db": str(db),
        "--mode": mode,
        "--approval-id": APPROVAL_ID,
    }
    values.update(overrides)
    args = [sys.executable, str(WORKER)]
    for key, value in values.items():
        args.extend([key, str(value)])
    return args


def _run(args: list[str], *, timeout: float = 20.0) -> tuple[dict[str, Any], int]:
    process = subprocess.Popen(
        args,
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        raise AssertionError("child process timed out") from None
    if process.returncode != 0:
        raise AssertionError(f"child failed rc={process.returncode}: {stderr!r}")
    if stderr.strip():
        raise AssertionError(f"unexpected child stderr: {stderr!r}")
    lines = [line for line in stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        raise AssertionError(f"expected one JSON line, got {stdout!r}")
    return json.loads(lines[0]), process.pid


def _start_race(db: Path, mode: str, root: Path, name: str) -> subprocess.Popen[str]:
    ready = root / f"ready-{name}"
    go = root / "go"
    return subprocess.Popen(
        _args(
            db,
            mode,
            **{"--ready": str(ready), "--go": str(go)},
        ),
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _finish_race(process: subprocess.Popen[str], *, ready: Path, go: Path) -> dict[str, Any]:
    deadline = time.monotonic() + 20.0
    while not ready.exists():
        if time.monotonic() > deadline:
            process.kill()
            raise AssertionError("race child did not become ready")
        time.sleep(0.01)
    return process  # type: ignore[return-value]


def _collect(process: subprocess.Popen[str]) -> dict[str, Any]:
    stdout, stderr = process.communicate(timeout=20.0)
    if process.returncode != 0:
        raise AssertionError(f"race child failed: {stderr!r}")
    lines = [line for line in stdout.splitlines() if line.strip()]
    assert len(lines) == 1, stdout
    return json.loads(lines[0])


def _row_count(db: Path) -> int:
    connection = sqlite3.connect(db)
    try:
        row = connection.execute("SELECT COUNT(*) FROM approval_challenges").fetchone()
        return int(row[0])
    finally:
        connection.close()


def test_p01_create_then_approve_then_receipt_in_three_processes(tmp_path: Path) -> None:
    db = tmp_path / "approvals.db"
    created, pid_a = _run(_args(db, "create"))
    assert created["created"] is True
    assert created["state"] == "PENDING"
    approved, pid_b = _run(_args(db, "approve"))
    assert approved["status"] == "resolved"
    assert approved["state"] == "APPROVED"
    assert approved["version"] == 1
    receipt, pid_c = _run(_args(db, "receipt"))
    assert receipt["state"] == "APPROVED"
    assert receipt["receipt"]["decision"] == "APPROVED"
    assert receipt["receipt"]["approval_id"] == APPROVAL_ID
    assert len({pid_a, pid_b, pid_c}) == 3
    assert _row_count(db) == 1


def test_p02_concurrent_reviewers_exactly_one_terminal(tmp_path: Path) -> None:
    db = tmp_path / "approvals.db"
    _run(_args(db, "create"))
    ready_a = tmp_path / "ready-approve"
    ready_b = tmp_path / "ready-reject"
    go = tmp_path / "go"
    approve = _start_race(db, "approve", tmp_path, "approve")
    reject = _start_race(db, "reject", tmp_path, "reject")
    deadline = time.monotonic() + 20.0
    while not (ready_a.exists() and ready_b.exists()):
        if time.monotonic() > deadline:
            approve.kill()
            reject.kill()
            raise AssertionError("reviewer race children did not become ready")
        time.sleep(0.01)
    go.write_text("go", encoding="utf-8")
    first = _collect(approve)
    second = _collect(reject)
    statuses = sorted([first["status"], second["status"]])
    assert statuses in (["already_resolved", "resolved"], ["resolved", "already_resolved"])
    assert first["pid"] != second["pid"]
    final, _ = _run(_args(db, "read"))
    assert final["state"] in ("APPROVED", "REJECTED")
    assert final["version"] == 1
    assert _row_count(db) == 1


def test_p03_duplicate_create_if_absent_one_durable_row(tmp_path: Path) -> None:
    db = tmp_path / "approvals.db"
    first, _ = _run(_args(db, "create"))
    second, _ = _run(_args(db, "create"))
    assert first["created"] is True
    assert second["created"] is False
    assert first["approval_id"] == second["approval_id"]
    assert _row_count(db) == 1


def test_p04_restart_after_approved_uses_existing_receipt(tmp_path: Path) -> None:
    db = tmp_path / "approvals.db"
    _run(_args(db, "create"))
    _run(_args(db, "approve"))
    # A fresh process after restart reads the durable APPROVED decision and constructs a
    # receipt without any reapproval step.
    receipt, _ = _run(_args(db, "receipt"))
    assert receipt["receipt"]["decision"] == "APPROVED"
    assert receipt["receipt"]["approval_id"] == APPROVAL_ID
    assert receipt["version"] == 1
    read, _ = _run(_args(db, "read"))
    assert read["state"] == "APPROVED"
    assert read["version"] == 1
