"""AtlasOS V2 Real User Acceptance harness.

The harness is not a runtime. It:
- snapshots the host fixture,
- launches the user-facing ``experiments/uat_cli.py`` subprocess,
- runs independent external acceptance checks against the exported sandbox workspace,
- verifies host isolation and the M7 trajectory by run_id,
- classifies failures and emits a UAT report.

A real UAT PASS additionally requires a production AnyCode sandbox provider. The current
environment has none, so the coding run uses the repository's deterministic local provider and is
reported as BLOCKED for the real-sandbox criterion rather than silently counted as PASS.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from anycode.runstore.store import FilesystemRunStore
from pydantic import BaseModel, ConfigDict

from atlasos.experiments import git_dirty
from atlasos.trajectory.reader import TrajectoryReader

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "experiments" / "fixtures" / "uat_repo"
ACCEPTANCE = REPO_ROOT / "experiments" / "fixtures" / "uat_acceptance.py"


class UATRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timestamp: datetime
    provider: str
    model: str
    task: str
    fixture_digest: str
    atlas_head: str | None
    atlas_dirty: bool
    run_id: str | None
    workspace_export: str | None

    user_entrypoint: str
    user_entrypoint_exit_code: int | None
    host_unchanged: bool
    changed_files: tuple[str, ...]
    unexpected_files: tuple[str, ...]
    removed_files: tuple[str, ...]
    patch_digest: str | None

    test_command: str
    test_exit_code: int | None
    acceptance_passed: bool
    acceptance_failures: tuple[str, ...]

    worker_events: int
    tools_disclosed: int
    action_proposed: int
    action_decisions: int
    execution_outcomes: int
    sandbox_events: int
    tool_observations: int
    run_terminal_events: int
    evidence_gaps: int
    evidence_conflicts: int
    trajectory_ok: bool

    real_llm: bool
    real_sandbox_provider: bool
    failure_class: str | None
    final_status: str


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    for item in sorted(path.rglob("*")):
        if item.is_file() and "__pycache__" not in item.parts:
            digest.update(item.relative_to(path).as_posix().encode())
            digest.update(item.read_bytes())
    return digest.hexdigest()


def _fixture_files(path: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    for item in sorted(path.rglob("*")):
        if (
            item.is_file()
            and "__pycache__" not in item.parts
            and ".pytest_cache" not in item.parts
            and ".git" not in item.parts
        ):
            files[item.relative_to(path).as_posix()] = hashlib.sha256(item.read_bytes()).hexdigest()
    return files


def _git_head() -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() or None


def _has_live_key() -> bool:
    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env", override=False)
    return bool(os.environ.get("DEEPSEEK_API_KEY"))


def _run_cli(
    *, scripted: bool, runs_root: Path, task: str
) -> tuple[int, dict[str, Any] | None, str]:
    command = [
        sys.executable,
        str(REPO_ROOT / "experiments" / "uat_cli.py"),
        task,
        "--fixture",
        str(FIXTURE),
        "--runs-root",
        str(runs_root),
        "--provider",
        "deepseek",
        "--model",
        "deepseek-chat",
    ]
    if scripted:
        command.append("--scripted")
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(REPO_ROOT / "src"), str(REPO_ROOT)])
    result = subprocess.run(
        command,
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    parsed: dict[str, Any] | None = None
    for line in reversed(result.stdout.splitlines()):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and "run_id" in value:
            parsed = value
            break
    return result.returncode, parsed, result.stderr


def _run_acceptance(export: Path, *, scripted: bool) -> tuple[bool, tuple[str, ...], int | None]:
    import importlib.util

    spec = importlib.util.spec_from_file_location("uat_acceptance", ACCEPTANCE)
    if spec is None or spec.loader is None:
        return False, ("cannot load acceptance module",), None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    result = module.validate(export)
    if scripted:
        return False, ("scripted smoke run does not claim coding acceptance",), None
    test = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=export,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    failures = list(result.failures)
    if test.returncode != 0:
        failures.append(f"pytest exited {test.returncode}")
    return result.passed and test.returncode == 0, tuple(failures), test.returncode


def _trajectory(run_dir: Path, run_id: str) -> tuple[dict[str, int], bool, tuple[str, ...]]:
    store = FilesystemRunStore(run_dir / "trajectory")
    view = TrajectoryReader(store).read(run_id)
    counts = {
        "worker_events": sum(1 for e in view.events if e.event_type == "worker_lifecycle"),
        "tools_disclosed": sum(1 for e in view.events if e.event_type == "tool_disclosed"),
        "action_proposed": sum(1 for e in view.events if e.event_type == "action_proposed"),
        "action_decisions": sum(1 for e in view.events if e.event_type == "action_decision"),
        "execution_outcomes": sum(1 for e in view.events if e.event_type == "execution_outcome"),
        "sandbox_events": sum(1 for e in view.events if e.event_type == "sandbox_lifecycle"),
        "tool_observations": sum(1 for e in view.events if e.event_type == "tool_observation"),
        "run_terminal_events": sum(1 for e in view.events if e.event_type == "run_terminal"),
        "evidence_gaps": view.metrics.evidence_gaps,
        "evidence_conflicts": view.metrics.evidence_conflicts,
    }
    required = {
        "worker_lifecycle",
        "action_proposed",
        "action_decision",
        "execution_outcome",
        "sandbox_lifecycle",
        "tool_observation",
        "run_terminal",
    }
    present = {event.event_type for event in view.events}
    missing = required - present
    return (
        counts,
        not missing and view.metrics.evidence_gaps == 0 and view.metrics.evidence_conflicts == 0,
        tuple(sorted(missing)),
    )


def main() -> None:
    parser = argparse.ArgumentParser(prog="atlas-uat-v2")
    parser.add_argument(
        "--task",
        default=(
            "This project has a CSV parsing bug. Find the bug, fix it, add the necessary "
            "regression tests, and run the tests to confirm. Do not change unrelated code."
        ),
    )
    parser.add_argument("--scripted", action="store_true")
    parser.add_argument("--runs-root", type=Path, default=None)
    args = parser.parse_args()

    runs_root = args.runs_root or Path(tempfile.mkdtemp(prefix="atlas-uat-v2-"))
    runs_root.mkdir(parents=True, exist_ok=True)
    host_before = _fixture_files(FIXTURE)
    host_digest_before = _digest(FIXTURE)
    head = _git_head()

    exit_code, session, stderr = _run_cli(
        scripted=args.scripted, runs_root=runs_root, task=args.task
    )
    host_after = _fixture_files(FIXTURE)
    host_digest_after = _digest(FIXTURE)
    host_unchanged = host_before == host_after

    changed: tuple[str, ...] = ()
    unexpected: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    patch_digest: str | None = None
    test_exit: int | None = None
    acceptance = False
    acceptance_failures: tuple[str, ...] = ()
    counts: dict[str, int] = {
        "worker_events": 0,
        "tools_disclosed": 0,
        "action_proposed": 0,
        "action_decisions": 0,
        "execution_outcomes": 0,
        "sandbox_events": 0,
        "tool_observations": 0,
        "run_terminal_events": 0,
        "evidence_gaps": 0,
        "evidence_conflicts": 0,
    }
    trajectory_ok = False
    missing_events: tuple[str, ...] = ()
    run_id = session.get("run_id") if session else None
    export = (
        Path(session["workspace_export"]) if session and session.get("workspace_export") else None
    )
    if export is not None and export.exists():
        changed_map = {
            path: digest
            for path, digest in _fixture_files(export).items()
            if host_before.get(path) != digest
        }
        removed = tuple(sorted(set(host_before) - set(_fixture_files(export))))
        changed = tuple(sorted(changed_map))
        unexpected = tuple(
            path
            for path in changed
            if path not in {"csv_parser.py"} and not path.startswith("tests/")
        )
        patch_digest = hashlib.sha256(
            json.dumps({"changed": changed_map, "removed": removed}, sort_keys=True).encode()
        ).hexdigest()
        acceptance, acceptance_failures, test_exit = _run_acceptance(export, scripted=args.scripted)
        if removed:
            acceptance = False
            acceptance_failures = (*acceptance_failures, f"removed files: {removed}")
        if "tests/test_csv_parser.py" not in _fixture_files(export):
            acceptance = False
            acceptance_failures = (
                *acceptance_failures,
                "existing test file was removed",
            )
    if session and run_id:
        counts, trajectory_ok, missing_events = _trajectory(Path(session["run_dir"]), run_id)

    live_key = _has_live_key()
    real_llm = live_key and not args.scripted
    real_sandbox = False  # no production AnyCode sandbox provider is installed/configured
    failure_class: str | None = None
    if exit_code != 0 or session is None:
        failure_class = "RUNTIME_FAILURE"
    elif not live_key and not args.scripted:
        failure_class = "CREDENTIAL_BLOCKED"
    elif args.scripted:
        failure_class = "SCRIPTED_SMOKE"
    elif not host_unchanged or unexpected or removed:
        failure_class = "RUNTIME_FAILURE"
    elif not acceptance or not trajectory_ok:
        failure_class = "MODEL_FAILURE"
    else:
        failure_class = None

    if (
        real_llm
        and real_sandbox
        and acceptance
        and host_unchanged
        and trajectory_ok
        and not unexpected
    ):
        final_status = "PASS"
    elif failure_class in (None, "CREDENTIAL_BLOCKED", "SCRIPTED_SMOKE"):
        final_status = "BLOCKED_NO_REAL_SANDBOX_PROVIDER"
    else:
        final_status = "FAIL"

    record = UATRecord(
        timestamp=datetime.now(UTC),
        provider="deepseek",
        model="deepseek-chat",
        task=args.task,
        fixture_digest=host_digest_before,
        atlas_head=head,
        atlas_dirty=git_dirty(REPO_ROOT),
        run_id=run_id,
        workspace_export=str(export) if export else None,
        user_entrypoint="experiments/uat_cli.py",
        user_entrypoint_exit_code=exit_code,
        host_unchanged=host_unchanged and host_digest_before == host_digest_after,
        changed_files=changed,
        unexpected_files=unexpected,
        removed_files=removed,
        patch_digest=patch_digest,
        test_command="python -m pytest -q",
        test_exit_code=test_exit,
        acceptance_passed=acceptance,
        acceptance_failures=acceptance_failures,
        **counts,
        trajectory_ok=trajectory_ok and not missing_events,
        real_llm=real_llm,
        real_sandbox_provider=real_sandbox,
        failure_class=failure_class,
        final_status=final_status,
    )
    print(record.model_dump_json(indent=2))
    if stderr.strip():
        print(stderr, file=sys.stderr)


if __name__ == "__main__":
    main()
