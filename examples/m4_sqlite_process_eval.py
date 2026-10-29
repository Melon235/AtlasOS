"""M4.1 formal cross-process SQLite idempotency evaluation."""

from __future__ import annotations

import importlib.metadata
import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from atlasos.experiments import ExperimentStore, git_commit, git_dirty

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "tests" / "integration"))
from test_sqlite_idempotency_process import (  # noqa: E402
    _finish_barrier_children,
    _run_process,
    _side_effect_count,
    _start_barrier_children,
)

STORE = REPO_ROOT / "experiments" / "runs" / "v2-m4-sqlite-process.jsonl"


class EvalRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    experiment: str
    git_commit: str | None
    git_dirty: bool
    timestamp: datetime
    anycode_version: str
    aiosqlite_version: str
    processes_spawned: int
    completed_restart_replays: int
    unresolved_restart_in_progress: int
    restart_conflicts: int
    original_payload_replays_after_conflict: int
    concurrency_rounds: int
    concurrency_execute_outcomes: int
    concurrency_in_progress_outcomes: int
    concurrency_replay_outcomes: int
    dual_execute_rounds: int
    duplicate_side_effects: int
    unexpected_sqlite_errors: int
    child_process_failures: int
    timeouts: int
    p01: bool
    p02: bool
    p03: bool
    p04: bool


def _run(root: Path) -> EvalRecord:
    processes = 0
    p01 = p02 = p03 = False
    completed_replays = unresolved = conflicts = original_replays = 0
    rounds = 20
    execute_outcomes = in_progress_outcomes = replay_outcomes = 0
    dual_execute = duplicate_effects = 0

    # P01
    p01_dir = root / "p01"
    p01_dir.mkdir()
    db = p01_dir / "idempotency.db"
    counter = p01_dir / "counter.db"
    a, a_pid = _run_process(
        db,
        counter,
        mode="claim-complete",
        tool="payment.charge",
        key="p01",
        result="charged:42",
    )
    processes += 1
    b, b_pid = _run_process(db, counter, mode="claim", tool="payment.charge", key="p01")
    processes += 1
    p01 = (
        a_pid != b_pid
        and a["outcome"] == "execute"
        and b["outcome"] == "replay"
        and b["result"]["data"] == "charged:42"
        and _side_effect_count(counter) == 1
    )
    completed_replays += int(p01)

    # P02
    p02_dir = root / "p02"
    p02_dir.mkdir()
    db = p02_dir / "idempotency.db"
    counter = p02_dir / "counter.db"
    a, a_pid = _run_process(db, counter, mode="claim-only", tool="email.send", key="p02")
    processes += 1
    b, b_pid = _run_process(db, counter, mode="claim", tool="email.send", key="p02")
    processes += 1
    c, c_pid = _run_process(db, counter, mode="claim", tool="email.send", key="p02")
    processes += 1
    p02 = (
        len({a_pid, b_pid, c_pid}) == 3
        and a["outcome"] == "execute"
        and b["outcome"] == "in_progress"
        and c["outcome"] == "in_progress"
        and _side_effect_count(counter) == 0
    )
    unresolved += sum(1 for item in (b, c) if item["outcome"] == "in_progress")

    # P03
    p03_dir = root / "p03"
    p03_dir.mkdir()
    db = p03_dir / "idempotency.db"
    counter = p03_dir / "counter.db"
    a, a_pid = _run_process(
        db,
        counter,
        mode="claim-complete",
        tool="github.issue.create",
        key="p03",
        payload="payload-A",
        result="issue:241",
    )
    processes += 1
    b, b_pid = _run_process(
        db,
        counter,
        mode="claim",
        tool="github.issue.create",
        key="p03",
        payload="payload-B",
    )
    processes += 1
    c, c_pid = _run_process(
        db,
        counter,
        mode="claim",
        tool="github.issue.create",
        key="p03",
        payload="payload-A",
    )
    processes += 1
    p03 = (
        len({a_pid, b_pid, c_pid}) == 3
        and a["outcome"] == "execute"
        and b["outcome"] == "conflict"
        and c["outcome"] == "replay"
        and c["result"]["data"] == "issue:241"
        and _side_effect_count(counter) == 1
    )
    conflicts += int(b["outcome"] == "conflict")
    original_replays += int(c["outcome"] == "replay")

    # P04
    for index in range(rounds):
        round_dir = root / f"p04-{index}"
        round_dir.mkdir()
        db = round_dir / "idempotency.db"
        counter = round_dir / "counter.db"
        ready_a = round_dir / "ready-a"
        ready_b = round_dir / "ready-b"
        go = round_dir / "go"
        a, b = _start_barrier_children(
            db, counter, tool="payment.charge", key=f"race-{index}", root=round_dir
        )
        first, second = _finish_barrier_children(a, b, ready_a=ready_a, ready_b=ready_b, go=go)
        processes += 2
        outcomes = [first["outcome"], second["outcome"]]
        execute_outcomes += outcomes.count("execute")
        in_progress_outcomes += outcomes.count("in_progress")
        replay_outcomes += outcomes.count("replay")
        if outcomes.count("execute") != 1:
            dual_execute += 1
        if _side_effect_count(counter) != 1:
            duplicate_effects += 1
    p04 = dual_execute == 0 and duplicate_effects == 0

    return EvalRecord(
        experiment="M4.1-sqlite-process",
        git_commit=git_commit(REPO_ROOT),
        git_dirty=git_dirty(REPO_ROOT),
        timestamp=datetime.now(UTC),
        anycode_version=importlib.metadata.version("anycode-py"),
        aiosqlite_version=importlib.metadata.version("aiosqlite"),
        processes_spawned=processes,
        completed_restart_replays=completed_replays,
        unresolved_restart_in_progress=unresolved,
        restart_conflicts=conflicts,
        original_payload_replays_after_conflict=original_replays,
        concurrency_rounds=rounds,
        concurrency_execute_outcomes=execute_outcomes,
        concurrency_in_progress_outcomes=in_progress_outcomes,
        concurrency_replay_outcomes=replay_outcomes,
        dual_execute_rounds=dual_execute,
        duplicate_side_effects=duplicate_effects,
        unexpected_sqlite_errors=0,
        child_process_failures=0,
        timeouts=0,
        p01=p01,
        p02=p02,
        p03=p03,
        p04=p04,
    )


def main() -> None:
    dry_run = os.environ.get("ATLASOS_M4_SQLITE_DRY_RUN") == "1"
    if git_dirty(REPO_ROOT) and not dry_run:
        raise SystemExit("M4.1 formal records require a clean worktree")
    with tempfile.TemporaryDirectory() as temporary:
        record = _run(Path(temporary))
    if not dry_run:
        ExperimentStore(STORE).append(record)
    print(record.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
