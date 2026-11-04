"""Stage C local workload ownership tests, including real OS subprocesses."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from tests.integration.test_project_sandbox import (
    context,
    exec_context,
    make_repo,
    setup_stack,
)

from atlasos.sandbox import (
    ProjectSandboxManager,
    SandboxLostError,
    SandboxOwnershipBusy,
    SandboxProjectConfig,
    SQLiteSandboxBindingStore,
    WorkloadOwnershipLock,
    workload_lock_identity,
)

ROOT = Path(__file__).resolve().parents[2]

_HOLDER_SCRIPT = """
import pathlib
import sys
import time
from atlasos.sandbox.ownership import WorkloadOwnershipLock

lock = WorkloadOwnershipLock(
    sys.argv[1],
    run_id=sys.argv[2],
    task_id=sys.argv[3],
)
lock.acquire()
pathlib.Path(sys.argv[4]).write_text("ready", encoding="utf-8")
while not pathlib.Path(sys.argv[5]).exists():
    time.sleep(0.02)
lock.release()
"""


def _spawn_holder(
    root: Path, *, run_id: str, task_id: str, ready: Path, stop: Path
) -> subprocess.Popen[bytes]:
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT / "src"), str(ROOT), env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _HOLDER_SCRIPT,
            str(root),
            run_id,
            task_id,
            str(ready),
            str(stop),
        ],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 20
    while not ready.exists():
        if process.poll() is not None:
            raise AssertionError(f"ownership holder exited with {process.returncode}")
        if time.monotonic() > deadline:
            process.kill()
            raise AssertionError("timed out waiting for ownership holder")
        time.sleep(0.02)
    return process


def _stop_holder(process: subprocess.Popen[bytes], stop: Path) -> None:
    stop.write_text("stop", encoding="utf-8")
    process.wait(timeout=20)


def test_lock_identity_is_path_safe() -> None:
    first = workload_lock_identity("run/with/../unsafe", "task with spaces")
    second = workload_lock_identity("run/with/../unsafe", "task with spaces")
    different = workload_lock_identity("run/with/../unsafe", "other task")
    assert first == second
    assert first != different
    assert len(first) == 64
    assert "task with spaces" not in first
    assert "run/with" not in first


def test_lock_acquire_release_reacquire(tmp_path: Path) -> None:
    lock = WorkloadOwnershipLock(tmp_path, run_id="run-1", task_id="task-1")
    lock.acquire()
    assert lock.held
    lock.assert_held()
    lock.release()
    assert not lock.held
    again = WorkloadOwnershipLock(tmp_path, run_id="run-1", task_id="task-1")
    again.acquire()
    again.release()


def test_c1_c4_real_process_exclusion_and_release(tmp_path: Path) -> None:
    ready = tmp_path / "ready"
    stop = tmp_path / "stop"
    process = _spawn_holder(tmp_path, run_id="run-1", task_id="task-1", ready=ready, stop=stop)
    try:
        contender = WorkloadOwnershipLock(tmp_path, run_id="run-1", task_id="task-1")
        with pytest.raises(SandboxOwnershipBusy):
            contender.acquire()
    finally:
        _stop_holder(process, stop)

    after_release = WorkloadOwnershipLock(tmp_path, run_id="run-1", task_id="task-1")
    after_release.acquire()
    after_release.release()


def test_c5_os_releases_lock_after_process_kill(tmp_path: Path) -> None:
    ready = tmp_path / "ready-kill"
    stop = tmp_path / "stop-kill"
    process = _spawn_holder(
        tmp_path, run_id="run-kill", task_id="task-kill", ready=ready, stop=stop
    )
    process.kill()
    process.wait(timeout=20)
    lock = WorkloadOwnershipLock(tmp_path, run_id="run-kill", task_id="task-kill")
    lock.acquire(timeout_seconds=5.0)
    lock.release()


def test_c9_distinct_workloads_do_not_block(tmp_path: Path) -> None:
    ready = tmp_path / "ready-c9"
    stop = tmp_path / "stop-c9"
    process = _spawn_holder(tmp_path, run_id="run-c9", task_id="task-a", ready=ready, stop=stop)
    try:
        other = WorkloadOwnershipLock(tmp_path, run_id="run-c9", task_id="task-b")
        other.acquire()
        other.release()
    finally:
        _stop_holder(process, stop)


async def test_c2_c3_second_manager_gets_no_provider_calls(tmp_path: Path) -> None:
    from tests.helpers.fake_sandbox_provider import FakeSandboxProvider

    ready = tmp_path / "ready-manager"
    stop = tmp_path / "stop-manager"
    process = _spawn_holder(
        tmp_path,
        run_id="run-m5",
        task_id="task-m5",
        ready=ready,
        stop=stop,
    )
    provider = FakeSandboxProvider(tmp_path / "provider")
    manager = ProjectSandboxManager(
        provider,
        SandboxProjectConfig(provider_name="fake", image="fake@sha256:0123456789abcdef"),
        run_id="run-m5",
        binding_store=SQLiteSandboxBindingStore(tmp_path / "sandbox_bindings.db"),
    )
    try:
        with pytest.raises(SandboxOwnershipBusy):
            await manager.ensure_binding(
                task_id="task-m5",
                source_repo=str(make_repo(tmp_path / "repo")),
                context=exec_context(),
                worker_id="w1",
            )
        assert provider.create_count == 0
        assert provider.inspect_calls == 0
        assert provider.execute_calls == 0
        assert provider.read_calls == 0
        assert provider.write_calls == 0
    finally:
        _stop_holder(process, stop)


async def test_c6_released_ownership_blocks_stale_runtime_cache(tmp_path: Path) -> None:
    store = SQLiteSandboxBindingStore(tmp_path / "sandbox_bindings.db")
    repo, provider, manager, binding, tools, runtime = await setup_stack(
        tmp_path, binding_store=store
    )
    del repo, binding, runtime
    manager.release_ownership("task-m5")
    before_reads = provider.read_calls
    result = await tools.file_read(
        type("V", (), {"path": "/workspace/project/app.py", "offset": None, "limit": None})(),
        context(),
    )
    assert result.is_error is True
    assert "ownership" in str(result.data).lower()
    assert provider.read_calls == before_reads


async def test_c8_new_owner_uses_verified_reattachment(tmp_path: Path) -> None:
    db_path = tmp_path / "sandbox_bindings.db"
    store = SQLiteSandboxBindingStore(db_path)
    repo, provider, manager, binding, tools, runtime = await setup_stack(
        tmp_path, binding_store=store
    )
    del repo, tools, runtime
    manager.release_ownership("task-m5")
    manager_b = ProjectSandboxManager(
        provider,
        manager.config,
        run_id="run-m5",
        binding_store=SQLiteSandboxBindingStore(db_path),
    )
    reattached = await manager_b.reattach_verified(
        task_id="task-m5",
        context=exec_context(),
    )
    assert reattached.sandbox_handle.id == binding.sandbox_handle.id
    assert provider.create_count == 1
    assert provider.attach_calls == 1
    assert await manager_b.close("task-m5") is True
    assert provider.create_count == 1


async def test_c7_terminal_durable_cannot_be_revived_by_new_owner(tmp_path: Path) -> None:
    db_path = tmp_path / "sandbox_bindings.db"
    store = SQLiteSandboxBindingStore(db_path)
    repo, provider, manager, binding, tools, runtime = await setup_stack(
        tmp_path, binding_store=store
    )
    del repo, tools, runtime
    await manager.mark_lost("task-m5")
    manager_b = ProjectSandboxManager(
        provider,
        manager.config,
        run_id="run-m5",
        binding_store=SQLiteSandboxBindingStore(db_path),
    )
    with pytest.raises(SandboxLostError, match="LOST"):
        await manager_b.ensure_binding(
            task_id="task-m5",
            source_repo=str(tmp_path / "unused"),
            context=exec_context(),
            worker_id="w1",
        )
    assert provider.create_count == 1
    durable = store.get(binding.binding_id)
    assert durable is not None
    assert durable.state == "LOST"


async def test_c10_network_policy_failure_keeps_ownership_from_enabling_runtime(
    tmp_path: Path,
) -> None:
    from atlasos.sandbox import SandboxNetworkPolicyViolation

    db_path = tmp_path / "sandbox_bindings.db"
    store = SQLiteSandboxBindingStore(db_path)
    repo, provider, manager, binding, tools, runtime = await setup_stack(
        tmp_path, binding_store=store
    )
    del repo, tools, runtime
    manager.release_ownership("task-m5")
    provider.network_policies[binding.sandbox_handle.id] = {
        "default_action": "deny",
        "egress": [{"action": "allow", "target": "example.com"}],
    }
    manager_b = ProjectSandboxManager(
        provider,
        manager.config,
        run_id="run-m5",
        binding_store=SQLiteSandboxBindingStore(db_path),
    )
    with pytest.raises(SandboxNetworkPolicyViolation):
        await manager_b.reattach_verified(
            task_id="task-m5",
            context=exec_context(),
        )
    assert manager_b.bindings == {}
    assert provider.create_count == 1
