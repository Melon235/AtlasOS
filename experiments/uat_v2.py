"""AtlasOS V2 Real User Acceptance harness.

The harness is not a runtime. It:
- snapshots the Host fixture and Host-only sentinel observations,
- launches the user-facing ``experiments/uat_cli.py`` subprocess against the trusted real provider,
- runs independent external acceptance checks against the exported sandbox workspace,
- verifies Host isolation and the M7 trajectory by run_id,
- classifies failures and emits a UAT report.

A live coding UAT PASS requires the real Daytona provider, a remote sandbox lifecycle, same-binding
project tools, an unchanged Host fixture, clean cleanup, and independent external acceptance. A
fake provider result is never presented as a real-isolation PASS.
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
from atlasos.sandbox import REAL_SANDBOX_PROVIDERS
from atlasos.trajectory.reader import TrajectoryReader

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "experiments" / "fixtures" / "uat_repo"
ACCEPTANCE = REPO_ROOT / "experiments" / "fixtures" / "uat_acceptance.py"
UAT_CONFIG = REPO_ROOT / "experiments" / "configs" / "v2-uat.yaml"
TASK = (
    "This project has a CSV parsing bug. Find the bug, fix it, add the necessary regression "
    "tests, and run the tests to confirm. Do not change unrelated code."
)


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
    host_fixture_digest_after: str
    changed_files: tuple[str, ...]
    unexpected_files: tuple[str, ...]
    removed_files: tuple[str, ...]
    patch_digest: str | None

    test_command: str
    test_exit_code: int | None
    acceptance_passed: bool
    acceptance_failures: tuple[str, ...]

    provider_name: str
    provider_is_real: bool
    provider_is_external_cloud: bool
    provider_is_self_hosted: bool
    opensandbox_server_health: str
    runtime_backend: str
    provider_capabilities: dict[str, Any] | None
    capabilities_digest: str | None
    sandbox_created: bool
    sandbox_ready: bool
    sandbox_logical_id: str | None
    binding_id: str | None
    sandbox_destroyed: bool
    same_binding_all_project_tools: bool
    commands_executed: int
    files_uploaded: int
    files_read: int
    files_written: int
    network_policy_requested: str
    network_policy_accepted: bool
    network_policy_verified: str
    host_only_sentinel_visible: bool | None
    sandbox_only_sentinel_visible_on_host: bool | None
    sandbox_only_sentinel_read: bool
    sandbox_lifecycle_states: tuple[str, ...]
    sandbox_lifecycle_binding_ids: tuple[str, ...]
    sandbox_lifecycle_sandbox_ids: tuple[str, ...]

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
    failure_stage: str | None
    final_status: str

    host_workspace_fallbacks: int
    unexpected_host_writes: int
    sandbox_recreations_after_lost: int
    model_selected_provider: int
    model_selected_workspace: int
    credentials_persisted: int
    fake_provider_used_in_real_uat: int


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
        "--config",
        str(UAT_CONFIG),
    ]
    if scripted:
        command.append("--scripted")
    else:
        command.extend(["--sandbox-provider", "opensandbox", "--require-real-sandbox"])
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(REPO_ROOT / "src"), str(REPO_ROOT)])
    result = subprocess.run(
        command,
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=1200,
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


def _run_acceptance(
    export: Path | None, *, scripted: bool
) -> tuple[bool, tuple[str, ...], int | None]:
    if scripted:
        return False, ("scripted smoke run does not claim coding acceptance",), None
    if export is None or not export.exists():
        return False, ("no exported sandbox workspace",), None
    import importlib.util

    spec = importlib.util.spec_from_file_location("uat_acceptance", ACCEPTANCE)
    if spec is None or spec.loader is None:
        return False, ("cannot load acceptance module",), None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    result = module.validate(export)
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


def _trajectory(
    run_dir: Path, run_id: str
) -> tuple[dict[str, int], bool, tuple[str, ...], dict[str, Any]]:
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
    sandbox_events = [event for event in view.events if event.event_type == "sandbox_lifecycle"]
    states = tuple(str(event.data.get("state")) for event in sandbox_events)
    binding_ids = tuple(
        sorted(
            {
                str(event.data["binding_id"])
                for event in sandbox_events
                if event.data.get("binding_id")
            }
        )
    )
    sandbox_ids = tuple(
        sorted(
            {
                str(event.data["sandbox_id"])
                for event in sandbox_events
                if event.data.get("sandbox_id")
            }
        )
    )
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
    evidence = {
        "states": states,
        "binding_ids": binding_ids,
        "sandbox_ids": sandbox_ids,
        "ready_count": states.count("READY"),
        "closed_count": states.count("CLOSED"),
        "lost_count": states.count("LOST"),
    }
    return (
        counts,
        not missing and view.metrics.evidence_gaps == 0 and view.metrics.evidence_conflicts == 0,
        tuple(sorted(missing)),
        evidence,
    )


def _sandbox_ok(sandbox: dict[str, Any], evidence: dict[str, Any]) -> bool:
    return bool(
        sandbox.get("sandbox_created")
        and sandbox.get("sandbox_ready")
        and sandbox.get("sandbox_destroyed")
        and sandbox.get("same_binding_all_project_tools")
        and sandbox.get("host_workspace_fallbacks", 0) == 0
        and sandbox.get("sandbox_recreations", 0) == 0
        and sandbox.get("host_only_sentinel_visible") is False
        and sandbox.get("sandbox_only_sentinel_visible_on_host") is False
        and sandbox.get("sandbox_only_sentinel_read") is True
        and sandbox.get("network_policy_requested") == "none"
        and sandbox.get("network_policy_accepted") is True
        and sandbox.get("network_policy_verified") != "FAIL"
        and evidence.get("ready_count") == 1
        and evidence.get("closed_count") == 1
        and evidence.get("lost_count") == 0
        and len(evidence.get("binding_ids", ())) == 1
        and len(evidence.get("sandbox_ids", ())) == 1
    )


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="atlas-uat-v2")
    parser.add_argument("--task", default=TASK)
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
    host_unchanged = host_before == host_after and host_digest_before == host_digest_after

    sandbox: dict[str, Any] = session.get("sandbox", {}) if session else {}
    provider_name = str(
        sandbox.get("provider_name") or (session or {}).get("sandbox_provider") or ""
    )
    provider_is_real = bool(sandbox.get("provider_is_real"))
    capabilities = sandbox.get("provider_capabilities")

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
    evidence: dict[str, Any] = {
        "states": (),
        "binding_ids": (),
        "sandbox_ids": (),
        "ready_count": 0,
        "closed_count": 0,
        "lost_count": 0,
    }
    run_id = session.get("run_id") if session else None
    export = (
        Path(session["workspace_export"]) if session and session.get("workspace_export") else None
    )
    if export is not None and export.exists():
        exported = _fixture_files(export)
        changed_map = {
            path: digest for path, digest in exported.items() if host_before.get(path) != digest
        }
        removed = tuple(sorted(set(host_before) - set(exported)))
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
        if "tests/test_csv_parser.py" not in exported:
            acceptance = False
            acceptance_failures = (*acceptance_failures, "existing test file was removed")
    if session and run_id:
        counts, trajectory_ok, missing_events, evidence = _trajectory(
            Path(session["run_dir"]), run_id
        )

    live_key = _has_live_key()
    real_llm = live_key and not args.scripted
    real_sandbox = provider_is_real and provider_name in REAL_SANDBOX_PROVIDERS
    sandbox_ok = _sandbox_ok(sandbox, evidence)
    session_failure = session.get("failure_class") if session else None
    session_stage = session.get("failure_stage") if session else None

    failure_class: str | None
    if exit_code != 0 and session is None:
        failure_class = "RUNTIME_FAILURE"
    elif session_failure is not None:
        failure_class = str(session_failure)
    elif not live_key and not args.scripted:
        failure_class = "CREDENTIAL_BLOCKED"
    elif args.scripted:
        failure_class = "SCRIPTED_SMOKE"
    elif not real_sandbox or provider_name not in REAL_SANDBOX_PROVIDERS:
        failure_class = "SANDBOX_PROVIDER_FAILURE"
    elif (
        not host_unchanged
        or sandbox.get("network_policy_verified") == "FAIL"
        or sandbox.get("host_only_sentinel_visible") is True
        or sandbox.get("sandbox_only_sentinel_visible_on_host") is True
    ):
        failure_class = "SANDBOX_ISOLATION_FAILURE"
    elif not (
        sandbox.get("sandbox_created")
        and sandbox.get("sandbox_ready")
        and sandbox.get("sandbox_destroyed")
    ):
        failure_class = "SANDBOX_PROVISION_FAILURE"
    elif not sandbox_ok:
        failure_class = "RUNTIME_FAILURE"
    elif unexpected or removed or not acceptance or not trajectory_ok:
        failure_class = "MODEL_FAILURE"
    else:
        failure_class = None

    fake_used = int(not args.scripted and provider_name == "fake")
    if args.scripted:
        final_status = "SCRIPTED_SMOKE"
    elif failure_class == "CREDENTIAL_BLOCKED":
        final_status = "BLOCKED_NO_CREDENTIALS"
    elif (
        real_llm
        and real_sandbox
        and provider_name == "opensandbox"
        and sandbox.get("provider_is_self_hosted") is True
        and sandbox.get("opensandbox_server_health") == "healthy"
        and failure_class is None
        and acceptance
        and host_unchanged
        and trajectory_ok
        and sandbox_ok
        and not unexpected
        and not removed
    ):
        final_status = "PASS"
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
        host_unchanged=host_unchanged,
        host_fixture_digest_after=host_digest_after,
        changed_files=changed,
        unexpected_files=unexpected,
        removed_files=removed,
        patch_digest=patch_digest,
        test_command="python -m pytest -q",
        test_exit_code=test_exit,
        acceptance_passed=acceptance,
        acceptance_failures=acceptance_failures,
        provider_name=provider_name,
        provider_is_real=provider_is_real,
        provider_is_external_cloud=bool(sandbox.get("provider_is_external_cloud")),
        provider_is_self_hosted=bool(sandbox.get("provider_is_self_hosted")),
        opensandbox_server_health=str(sandbox.get("opensandbox_server_health", "UNKNOWN")),
        runtime_backend=str(sandbox.get("runtime_backend", "unknown")),
        provider_capabilities=capabilities if isinstance(capabilities, dict) else None,
        capabilities_digest=sandbox.get("capabilities_digest"),
        sandbox_created=bool(sandbox.get("sandbox_created")),
        sandbox_ready=bool(sandbox.get("sandbox_ready")),
        sandbox_logical_id=sandbox.get("sandbox_logical_id"),
        binding_id=sandbox.get("binding_id"),
        sandbox_destroyed=bool(sandbox.get("sandbox_destroyed")),
        same_binding_all_project_tools=bool(sandbox.get("same_binding_all_project_tools")),
        commands_executed=int(sandbox.get("commands_executed", 0)),
        files_uploaded=int(sandbox.get("files_uploaded", 0)),
        files_read=int(sandbox.get("files_read", 0)),
        files_written=int(sandbox.get("files_written", 0)),
        network_policy_requested=str(sandbox.get("network_policy_requested", "none")),
        network_policy_accepted=bool(sandbox.get("network_policy_accepted")),
        network_policy_verified=str(sandbox.get("network_policy_verified", "UNKNOWN")),
        host_only_sentinel_visible=sandbox.get("host_only_sentinel_visible"),
        sandbox_only_sentinel_visible_on_host=sandbox.get("sandbox_only_sentinel_visible_on_host"),
        sandbox_only_sentinel_read=bool(sandbox.get("sandbox_only_sentinel_read")),
        sandbox_lifecycle_states=tuple(evidence.get("states", ())),
        sandbox_lifecycle_binding_ids=tuple(evidence.get("binding_ids", ())),
        sandbox_lifecycle_sandbox_ids=tuple(evidence.get("sandbox_ids", ())),
        **counts,
        trajectory_ok=trajectory_ok and not missing_events,
        real_llm=real_llm,
        real_sandbox_provider=real_sandbox,
        failure_class=failure_class,
        failure_stage=session_stage,
        final_status=final_status,
        host_workspace_fallbacks=int(sandbox.get("host_workspace_fallbacks", 0)),
        unexpected_host_writes=0 if host_unchanged else 1,
        sandbox_recreations_after_lost=int(sandbox.get("sandbox_recreations", 0)),
        model_selected_provider=0,
        model_selected_workspace=0,
        credentials_persisted=0,
        fake_provider_used_in_real_uat=fake_used,
    )
    print(record.model_dump_json(indent=2))
    if stderr.strip():
        print(stderr, file=sys.stderr)


if __name__ == "__main__":
    main()
