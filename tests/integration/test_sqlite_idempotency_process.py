"""P01-P04: real cross-process AnyCode SQLite idempotency verification."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKER = REPO_ROOT / "tests" / "helpers" / "sqlite_idempotency_process_worker.py"


def _run_process(
    db: Path,
    counter_db: Path,
    *,
    mode: str,
    tool: str,
    key: str,
    payload: str = "payload-A",
    result: str = "result:ok",
    ready: Path | None = None,
    go: Path | None = None,
    timeout: float = 20.0,
) -> tuple[dict[str, Any], int]:
    args = [
        sys.executable,
        str(WORKER),
        "--db",
        str(db),
        "--counter-db",
        str(counter_db),
        "--tool",
        tool,
        "--key",
        key,
        "--payload",
        payload,
        "--result",
        result,
        "--mode",
        mode,
    ]
    if ready is not None:
        args += ["--ready", str(ready), "--go", str(go)]
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
        raise AssertionError(f"child process failed rc={process.returncode}: stderr={stderr!r}")
    if stderr.strip():
        raise AssertionError(f"unexpected child stderr: {stderr!r}")
    lines = [line for line in stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        raise AssertionError(f"expected one JSON line, got stdout={stdout!r}")
    return json.loads(lines[0]), process.pid


def _side_effect_count(counter_db: Path) -> int:
    if not counter_db.exists():
        return 0
    connection = sqlite3.connect(counter_db)
    try:
        try:
            row = connection.execute("SELECT COUNT(*) FROM test_side_effects").fetchone()
        except sqlite3.OperationalError:
            return 0
        return int(row[0])
    finally:
        connection.close()


def _start_barrier_children(
    db: Path, counter_db: Path, *, tool: str, key: str, root: Path
) -> tuple[subprocess.Popen[str], subprocess.Popen[str]]:
    ready_a = root / "ready-a"
    ready_b = root / "ready-b"
    go = root / "go"
    common = [
        sys.executable,
        str(WORKER),
        "--db",
        str(db),
        "--counter-db",
        str(counter_db),
        "--tool",
        tool,
        "--key",
        key,
        "--payload",
        "payload-A",
        "--result",
        "result:ok",
        "--mode",
        "barrier-claim",
    ]
    a = subprocess.Popen(
        common + ["--ready", str(ready_a), "--go", str(go)],
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    b = subprocess.Popen(
        common + ["--ready", str(ready_b), "--go", str(go)],
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return a, b


def _finish_barrier_children(
    a: subprocess.Popen[str],
    b: subprocess.Popen[str],
    *,
    ready_a: Path,
    ready_b: Path,
    go: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    deadline = time.monotonic() + 20.0
    while not (ready_a.exists() and ready_b.exists()):
        if time.monotonic() > deadline:
            for process in (a, b):
                process.kill()
            raise AssertionError("barrier children did not both become ready")
        time.sleep(0.01)
    go.write_text("go", encoding="utf-8")
    results: list[dict[str, Any]] = []
    for process in (a, b):
        try:
            stdout, stderr = process.communicate(timeout=20.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise AssertionError("barrier child timed out") from None
        if process.returncode != 0:
            raise AssertionError(f"barrier child failed: {stderr!r}")
        if stderr.strip():
            raise AssertionError(f"unexpected barrier stderr: {stderr!r}")
        lines = [line for line in stdout.splitlines() if line.strip()]
        if len(lines) != 1:
            raise AssertionError(f"bad barrier stdout: {stdout!r}")
        results.append(json.loads(lines[0]))
    return results[0], results[1]


def test_sqlite_completed_claim_replays_across_process_restart(tmp_path: Path) -> None:
    db = tmp_path / "idempotency.db"
    counter = tmp_path / "side-effects.db"
    a, a_pid = _run_process(
        db,
        counter,
        mode="claim-complete",
        tool="payment.charge",
        key="stable-action-001",
        result="charged:42",
    )
    b, b_pid = _run_process(
        db,
        counter,
        mode="claim",
        tool="payment.charge",
        key="stable-action-001",
    )
    assert a_pid != b_pid
    assert a["outcome"] == "execute"
    assert b["outcome"] == "replay"
    assert b["result"]["data"] == "charged:42"
    assert _side_effect_count(counter) == 1


def test_sqlite_unresolved_claim_remains_in_progress_across_restart(tmp_path: Path) -> None:
    db = tmp_path / "idempotency.db"
    counter = tmp_path / "side-effects.db"
    a, a_pid = _run_process(
        db,
        counter,
        mode="claim-only",
        tool="email.send",
        key="stable-action-002",
    )
    b, b_pid = _run_process(
        db,
        counter,
        mode="claim",
        tool="email.send",
        key="stable-action-002",
    )
    c, c_pid = _run_process(
        db,
        counter,
        mode="claim",
        tool="email.send",
        key="stable-action-002",
    )
    assert len({a_pid, b_pid, c_pid}) == 3
    assert a["outcome"] == "execute"
    assert b["outcome"] == "in_progress"
    assert c["outcome"] == "in_progress"
    assert _side_effect_count(counter) == 0


def test_sqlite_conflict_survives_process_restart(tmp_path: Path) -> None:
    db = tmp_path / "idempotency.db"
    counter = tmp_path / "side-effects.db"
    a, a_pid = _run_process(
        db,
        counter,
        mode="claim-complete",
        tool="github.issue.create",
        key="stable-action-003",
        payload="payload-A",
        result="issue:241",
    )
    b, b_pid = _run_process(
        db,
        counter,
        mode="claim",
        tool="github.issue.create",
        key="stable-action-003",
        payload="payload-B",
    )
    c, c_pid = _run_process(
        db,
        counter,
        mode="claim",
        tool="github.issue.create",
        key="stable-action-003",
        payload="payload-A",
    )
    assert len({a_pid, b_pid, c_pid}) == 3
    assert a["outcome"] == "execute"
    assert b["outcome"] == "conflict"
    assert c["outcome"] == "replay"
    assert c["result"]["data"] == "issue:241"
    assert _side_effect_count(counter) == 1


def test_sqlite_concurrent_processes_allow_only_one_execute(tmp_path: Path) -> None:
    rounds = 20
    dual_execute_rounds = 0
    duplicate_effects = 0
    for index in range(rounds):
        round_dir = tmp_path / f"round-{index}"
        round_dir.mkdir()
        db = round_dir / "idempotency.db"
        counter = round_dir / "side-effects.db"
        ready_a = round_dir / "ready-a"
        ready_b = round_dir / "ready-b"
        go = round_dir / "go"
        a, b = _start_barrier_children(
            db, counter, tool="payment.charge", key=f"race-{index}", root=round_dir
        )
        first, second = _finish_barrier_children(a, b, ready_a=ready_a, ready_b=ready_b, go=go)
        outcomes = [first["outcome"], second["outcome"]]
        if outcomes.count("execute") != 1:
            dual_execute_rounds += 1
        if outcomes.count("execute") > 1:
            duplicate_effects += 1
        assert outcomes.count("execute") == 1
        assert set(outcomes) <= {"execute", "in_progress", "replay"}
        assert _side_effect_count(counter) == 1
        assert first["pid"] != second["pid"]
    assert dual_execute_rounds == 0
    assert duplicate_effects == 0
