"""Deterministic no-LLM Stage C local ownership + real OpenSandbox smoke.

Roles:
  holder   acquire local ownership, create S1, hold the lock in a real OS process
  attempt  try the same workload while holder is alive; must report BUSY without provider calls
  recover  after holder death, acquire the OS lock, verify/reattach S1, read marker, clean up
  (no role) orchestrate the three subprocesses
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from anycode.identity.context import ExecutionContext

from atlasos.config import load_config
from atlasos.sandbox import (
    OpenSandboxOptions,
    ProjectSandboxManager,
    SandboxOwnershipBusy,
    SandboxProjectConfig,
    SQLiteSandboxBindingStore,
    WorkloadOwnershipLock,
    build_sandbox_provider,
    compute_sandbox_continuity_digest,
    require_capabilities,
    require_real_sandbox_provider,
)
from atlasos.sandbox.validation import probe_network_none

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = REPO_ROOT / "experiments" / "configs" / "v2-uat.yaml"
DEFAULT_FIXTURE = REPO_ROOT / "experiments" / "fixtures" / "uat_repo"
MARKER_PATH = "/workspace/project/owner-marker.txt"
MARKER_BYTES = b"atlas-stage-c-local-owner\n"


def _project_config(config) -> SandboxProjectConfig:
    sandbox = config.sandbox
    return SandboxProjectConfig(
        provider_name="opensandbox",
        image=sandbox.image,
        snapshot=sandbox.snapshot,
        workspace_root=sandbox.workspace_root,
        max_bundle_bytes=sandbox.max_bundle_bytes,
        network=sandbox.network,
    )


def _provider(config):
    sandbox = config.sandbox
    options = OpenSandboxOptions(
        endpoint=sandbox.endpoint,
        protocol=sandbox.protocol,
        timeout_minutes=sandbox.timeout_minutes,
        ready_timeout_seconds=sandbox.ready_timeout_seconds,
        cpu=sandbox.cpu,
        memory=sandbox.memory,
        use_server_proxy=sandbox.use_server_proxy,
    )
    provider = build_sandbox_provider("opensandbox", opensandbox_options=options)
    require_real_sandbox_provider(provider, expected_name="opensandbox")
    require_capabilities(provider.capabilities())
    return provider


def _context(run_id: str, task_id: str) -> ExecutionContext:
    return ExecutionContext(
        principal="user:stage-c-local-owner",
        attributes={"run_id": run_id, "task_id": task_id},
    )


def _prepare_source_fixture(source: Path, destination: Path) -> Path:
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(
        source,
        destination,
        ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"),
    )
    subprocess.run(["git", "init", "-q"], cwd=destination, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.test"], cwd=destination, check=True
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=destination, check=True)
    subprocess.run(["git", "add", "."], cwd=destination, check=True)
    subprocess.run(["git", "commit", "-qm", "stage-c fixture"], cwd=destination, check=True)
    return destination


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


class _ProbeProvider:
    """Never used if ownership stops the attempt before provider access."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def capabilities(self):
        self.calls.append("capabilities")
        raise AssertionError("provider.capabilities must not be called while ownership is busy")

    async def create(self, spec):  # noqa: ANN001
        del spec
        self.calls.append("create")
        raise AssertionError("provider.create must not be called while ownership is busy")

    async def inspect_existing(self, sandbox_id: str):
        del sandbox_id
        self.calls.append("inspect_existing")
        raise AssertionError("provider.inspect_existing must not be called")

    async def attach_existing(self, sandbox_id: str) -> None:
        del sandbox_id
        self.calls.append("attach_existing")
        raise AssertionError("provider.attach_existing must not be called")

    async def detach_local(self, sandbox_id: str) -> None:
        del sandbox_id
        self.calls.append("detach_local")

    async def verify_network_none(self, sandbox_id: str) -> None:
        del sandbox_id
        self.calls.append("verify_network_none")


async def _run_holder(args) -> int:
    config = load_config(args.config)
    provider = _provider(config)
    manager = ProjectSandboxManager(
        provider,
        _project_config(config),
        run_id=args.run_id,
        binding_store=SQLiteSandboxBindingStore(Path(args.state_root) / "sandbox_bindings.db"),
        ownership_root=Path(args.state_root),
    )
    binding = await manager.ensure_binding(
        task_id=args.task_id,
        source_repo=args.source_repo,
        context=_context(args.run_id, args.task_id),
        worker_id=None,
    )
    write = await provider.write_file(binding.sandbox_handle, MARKER_PATH, MARKER_BYTES)
    if not write.ok:
        raise RuntimeError(f"marker write failed: {write.error}")
    durable = SQLiteSandboxBindingStore(Path(args.state_root) / "sandbox_bindings.db").get_for_task(
        args.run_id, args.task_id
    )
    if durable is None:
        raise RuntimeError("durable binding missing after create")
    _write_json(
        Path(args.handoff),
        {
            "status": "READY",
            "pid": os.getpid(),
            "sandbox_id": binding.sandbox_handle.id,
            "binding_id": binding.binding_id,
            "provider_created_at": (
                durable.provider_created_at.isoformat()
                if durable.provider_created_at is not None
                else None
            ),
            "continuity_digest": compute_sandbox_continuity_digest(durable),
            "create_count": manager.create_count,
        },
    )
    Path(args.ready).write_text("ready", encoding="utf-8")  # noqa: ASYNC240
    # Deliberately do not call close/destroy: the parent kills this process and the remote S1
    # must survive. OS process termination releases the ownership lock.
    while True:
        time.sleep(0.1)  # noqa: ASYNC251


async def _run_attempt(args) -> int:
    config = load_config(args.config)
    provider = _ProbeProvider()
    result_path = Path(args.result)
    manager = ProjectSandboxManager(
        provider,  # type: ignore[arg-type]
        _project_config(config),
        run_id=args.run_id,
        binding_store=SQLiteSandboxBindingStore(Path(args.state_root) / "sandbox_bindings.db"),
        ownership_root=Path(args.state_root),
    )
    try:
        await manager.ensure_binding(
            task_id=args.task_id,
            source_repo=args.source_repo,
            context=_context(args.run_id, args.task_id),
            worker_id=None,
        )
    except SandboxOwnershipBusy as error:
        _write_json(
            result_path,
            {
                "status": "BUSY",
                "error": str(error),
                "provider_calls": list(provider.calls),
            },
        )
        return 0
    _write_json(
        result_path,
        {"status": "UNEXPECTED_SUCCESS", "provider_calls": list(provider.calls)},
    )
    return 1


async def _run_recover(args) -> int:
    config = load_config(args.config)
    provider = _provider(config)
    state_root = Path(args.state_root)
    db_path = state_root / "sandbox_bindings.db"
    manager = ProjectSandboxManager(
        provider,
        _project_config(config),
        run_id=args.run_id,
        binding_store=SQLiteSandboxBindingStore(db_path),
        ownership_root=state_root,
    )
    binding = None
    try:
        binding = await manager.reattach_verified(
            task_id=args.task_id,
            context=_context(args.run_id, args.task_id),
        )
        read = await provider.read_file(binding.sandbox_handle, MARKER_PATH)
        network = await probe_network_none(provider, binding.sandbox_handle)
        handoff = json.loads(Path(args.handoff).read_text(encoding="utf-8"))  # noqa: ASYNC240
        durable = SQLiteSandboxBindingStore(db_path).get_for_task(args.run_id, args.task_id)
        if durable is None:
            raise RuntimeError("durable binding disappeared")
        result = {
            "status": "PASS",
            "sandbox_id": binding.sandbox_handle.id,
            "binding_id": binding.binding_id,
            "same_sandbox": binding.sandbox_handle.id == handoff["sandbox_id"],
            "marker_read": bool(read.ok and read.data == MARKER_BYTES),
            "network_none": network,
            "continuity_digest": compute_sandbox_continuity_digest(durable),
            "handoff_continuity_digest": handoff["continuity_digest"],
            "recover_create_count": manager.create_count,
        }
    except Exception as error:  # noqa: BLE001 - report failure to parent
        result = {
            "status": "FAIL",
            "error": f"{type(error).__name__}: {error}",
            "create_count": manager.create_count,
        }
        _write_json(Path(args.result), result)
        return 1
    finally:
        if binding is not None:
            with contextlib.suppress(Exception):
                await manager.close(args.task_id)
        _write_json(Path(args.result), result)
    return 0 if result["status"] == "PASS" else 1


def _wait_ready(path: Path, process: subprocess.Popen[Any], timeout: float = 180.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        if process.poll() is not None:
            raise RuntimeError(f"holder exited early with {process.returncode}")
        if time.monotonic() > deadline:
            process.kill()
            raise RuntimeError("timed out waiting for holder")
        time.sleep(0.05)


def _wait_ownership_available(state_root: Path, run_id: str, task_id: str) -> None:
    lock = WorkloadOwnershipLock(state_root, run_id=run_id, task_id=task_id)
    lock.acquire(timeout_seconds=10.0)
    lock.release()


def _run_subprocess(role: str, args: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO_ROOT / "src"), str(REPO_ROOT), env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    return subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), role, *args],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


async def orchestrate(args) -> dict[str, Any]:
    config_path = Path(args.config)
    fixture = Path(args.fixture)
    runs_root = Path(args.runs_root)
    runs_root.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240
    run_id = f"stage-c-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    task_id = "task-stage-c"
    run_dir = Path(tempfile.mkdtemp(prefix="atlas-stage-c-", dir=str(runs_root)))
    state_root = run_dir / "state"
    state_root.mkdir()
    source_repo = _prepare_source_fixture(fixture, run_dir / "source")
    ready = run_dir / "holder.ready"
    handoff = run_dir / "holder.json"
    attempt_result = run_dir / "attempt.json"
    recover_result = run_dir / "recover.json"
    common = [
        "--config",
        str(config_path),
        "--state-root",
        str(state_root),
        "--run-id",
        run_id,
        "--task-id",
        task_id,
        "--source-repo",
        str(source_repo),
    ]
    result: dict[str, Any] = {
        "experiment": "V2-stage-c-local-ownership",
        "status": "FAIL",
        "run_id": run_id,
        "run_dir": str(run_dir),
        "holder": None,
        "attempt": None,
        "recover": None,
    }
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO_ROOT / "src"), str(REPO_ROOT), env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    holder = subprocess.Popen(  # noqa: ASYNC220
        [
            sys.executable,
            str(Path(__file__).resolve()),  # noqa: ASYNC240
            "holder",
            *common,
            "--ready",
            str(ready),
            "--handoff",
            str(handoff),
        ],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_ready(ready, holder)
        result["holder"] = json.loads(handoff.read_text(encoding="utf-8"))
        attempt = _run_subprocess(
            "attempt",
            [*common, "--result", str(attempt_result)],
            timeout=120,
        )
        if attempt.returncode != 0:
            raise RuntimeError(f"attempt failed: {attempt.stderr}")
        result["attempt"] = json.loads(attempt_result.read_text(encoding="utf-8"))
        holder.kill()
        holder.wait(timeout=30)
        _wait_ownership_available(state_root, run_id, task_id)
        recover = _run_subprocess(
            "recover",
            [
                "--config",
                str(config_path),
                "--state-root",
                str(state_root),
                "--run-id",
                run_id,
                "--task-id",
                task_id,
                "--handoff",
                str(handoff),
                "--result",
                str(recover_result),
            ],
            timeout=600,
        )
        if recover.returncode != 0:
            raise RuntimeError(
                f"recover failed rc={recover.returncode}: {recover.stderr} "
                f"{recover_result.read_text(encoding='utf-8') if recover_result.exists() else ''}"
            )
        result["recover"] = json.loads(recover_result.read_text(encoding="utf-8"))
        attempt_data = result["attempt"]
        holder_data = result["holder"]
        recover_data = result["recover"]
        assert result["holder"]["status"] == "READY"
        assert attempt_data["status"] == "BUSY"
        assert attempt_data["provider_calls"] == []
        assert recover_data["status"] == "PASS"
        assert recover_data["same_sandbox"] is True
        assert recover_data["marker_read"] is True
        assert recover_data["network_none"] == "PASS"
        assert recover_data["continuity_digest"] == holder_data["continuity_digest"]
        assert recover_data["recover_create_count"] == 0
        assert holder_data["create_count"] == 1
        result["status"] = "PASS"
    except Exception as error:  # noqa: BLE001 - report failure
        result["error"] = f"{type(error).__name__}: {error}"
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=30)
    _write_json(run_dir / "result.json", result)
    return result


async def _main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("role", nargs="?", choices=("holder", "attempt", "recover"))
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--runs-root", type=Path, default=REPO_ROOT / "experiments" / "runs")
    parser.add_argument("--state-root", type=Path)
    parser.add_argument("--source-repo")
    parser.add_argument("--run-id")
    parser.add_argument("--task-id")
    parser.add_argument("--ready")
    parser.add_argument("--handoff")
    parser.add_argument("--result")
    args = parser.parse_args()
    if args.role == "holder":
        return await _run_holder(args)
    if args.role == "attempt":
        return await _run_attempt(args)
    if args.role == "recover":
        return await _run_recover(args)
    outcome = await orchestrate(args)
    print(json.dumps(outcome, indent=2))
    return 0 if outcome["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
