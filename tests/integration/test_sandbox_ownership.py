"""Stage C local workload ownership tests, including real OS subprocesses."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from tests.helpers.fake_sandbox_provider import FakeSandboxProvider
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
    SandboxOwnershipError,
    SandboxProjectConfig,
    SandboxWorkspaceTools,
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


_ATTEMPT_SCRIPT = """
import sys
from atlasos.sandbox.ownership import SandboxOwnershipBusy, WorkloadOwnershipLock

lock = WorkloadOwnershipLock(
    sys.argv[1],
    run_id=sys.argv[2],
    task_id=sys.argv[3],
)
try:
    lock.acquire()
except SandboxOwnershipBusy:
    sys.exit(2)
lock.release()
"""


def _attempt_lock_in_process(root: Path, *, run_id: str, task_id: str) -> bool:
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT / "src"), str(ROOT), env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    result = subprocess.run(
        [sys.executable, "-c", _ATTEMPT_SCRIPT, str(root), run_id, task_id],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode == 0:
        return True
    if result.returncode == 2:
        return False
    raise AssertionError(f"lock-attempt process failed: {result.returncode} {result.stderr}")


class GatedCreateProvider(FakeSandboxProvider):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.create_entered = asyncio.Event()
        self.create_release = asyncio.Event()
        self.fail_first_create = False
        self.create_calls = 0

    async def create(self, spec):
        self.create_calls += 1
        self.create_entered.set()
        await self.create_release.wait()
        if self.fail_first_create:
            self.fail_first_create = False
            raise RuntimeError("injected create failure")
        return await super().create(spec)


class GatedWriteProvider(FakeSandboxProvider):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.write_entered = asyncio.Event()
        self.write_release = asyncio.Event()
        self.write_started = 0
        self.write_active = 0
        self.write_max_active = 0

    async def create(self, spec):
        return await super().create(spec)

    async def write_file(self, handle, path, data):
        if not path.startswith("/workspace/project"):
            return await super().write_file(handle, path, data)
        self.write_started += 1
        self.write_active += 1
        self.write_max_active = max(self.write_max_active, self.write_active)
        self.write_entered.set()
        try:
            await self.write_release.wait()
            return await super().write_file(handle, path, data)
        finally:
            self.write_active -= 1


def _file_write_input(path: str, content: str):
    return type("V", (), {"path": path, "content": content})()


def _file_read_input(path: str):
    return type("V", (), {"path": path, "offset": None, "limit": None})()


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
    await manager.release_ownership("task-m5")
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
    await manager.release_ownership("task-m5")
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


def _manager_for(
    tmp_path: Path, provider: FakeSandboxProvider, db_path: Path | None = None
) -> ProjectSandboxManager:
    return ProjectSandboxManager(
        provider,
        SandboxProjectConfig(provider_name="fake", image="fake@sha256:0123456789abcdef"),
        run_id="run-m5",
        binding_store=SQLiteSandboxBindingStore(db_path or (tmp_path / "sandbox_bindings.db")),
        ownership_root=tmp_path,
    )


async def test_t1_cancel_waiting_coroutine_does_not_release_ownership(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path)
    provider = GatedCreateProvider(tmp_path / "provider")
    manager = _manager_for(tmp_path, provider)
    first = asyncio.create_task(
        manager.ensure_binding(
            task_id="task-m5",
            source_repo=str(repo),
            context=exec_context(),
            worker_id="w1",
        )
    )
    await provider.create_entered.wait()
    second = asyncio.create_task(
        manager.ensure_binding(
            task_id="task-m5",
            source_repo=str(repo),
            context=exec_context(),
            worker_id="w1",
        )
    )
    await asyncio.sleep(0)
    second.cancel()
    with pytest.raises(asyncio.CancelledError):
        await second
    assert _attempt_lock_in_process(tmp_path, run_id="run-m5", task_id="task-m5") is False
    provider.create_release.set()
    binding = await first
    assert binding.state == "READY"
    assert provider.create_count == 1
    assert await manager.close("task-m5") is True


async def test_t2_failed_creator_releases_and_waiting_successor_acquires(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path)
    db_path = tmp_path / "sandbox_bindings.db"
    provider = GatedCreateProvider(tmp_path / "provider")
    provider.fail_first_create = True
    manager_a = _manager_for(tmp_path, provider, db_path)
    manager_b = manager_a
    first = asyncio.create_task(
        manager_a.ensure_binding(
            task_id="task-m5",
            source_repo=str(repo),
            context=exec_context(),
            worker_id="w1",
        )
    )
    await provider.create_entered.wait()
    second = asyncio.create_task(
        manager_b.ensure_binding(
            task_id="task-m5",
            source_repo=str(repo),
            context=exec_context(),
            worker_id="w1",
        )
    )
    await asyncio.sleep(0)
    provider.create_release.set()
    with pytest.raises(RuntimeError, match="injected"):
        await first
    binding = await second
    assert binding.state == "READY"
    assert provider.create_count == 1
    assert provider.create_calls == 2
    store = SQLiteSandboxBindingStore(db_path)
    durable = store.get_for_task("run-m5", "task-m5")
    assert durable is not None
    assert durable.sandbox_id == binding.sandbox_handle.id
    assert await manager_b.close("task-m5") is True


async def test_t3_release_waits_for_inflight_write_and_process_gate(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path)
    provider = GatedWriteProvider(tmp_path / "provider")
    manager = _manager_for(tmp_path, provider)
    binding = await manager.ensure_binding(
        task_id="task-m5",
        source_repo=str(repo),
        context=exec_context(),
        worker_id="w1",
    )
    tools = SandboxWorkspaceTools(manager, "task-m5", provider)
    write = asyncio.create_task(
        tools.file_write(_file_write_input("/workspace/project/t3.txt", "t3"), context())
    )
    await provider.write_entered.wait()
    release = asyncio.create_task(manager.release_ownership("task-m5"))
    await asyncio.sleep(0)
    assert release.done() is False
    assert _attempt_lock_in_process(tmp_path, run_id="run-m5", task_id="task-m5") is False
    provider.write_release.set()
    result = await write
    assert result.is_error is False
    await release
    assert _attempt_lock_in_process(tmp_path, run_id="run-m5", task_id="task-m5") is True
    assert _attempt_lock_in_process(tmp_path, run_id="run-m5", task_id="task-m5") is True
    assert binding.state == "READY"


async def test_t4_two_tools_instances_share_manager_gate(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    provider = GatedWriteProvider(tmp_path / "provider")
    manager = _manager_for(tmp_path, provider)
    await manager.ensure_binding(
        task_id="task-m5",
        source_repo=str(repo),
        context=exec_context(),
        worker_id="w1",
    )
    first_tools = SandboxWorkspaceTools(manager, "task-m5", provider)
    second_tools = SandboxWorkspaceTools(manager, "task-m5", provider)
    first = asyncio.create_task(
        first_tools.file_write(_file_write_input("/workspace/project/t4-a.txt", "a"), context())
    )
    await provider.write_entered.wait()
    second = asyncio.create_task(
        second_tools.file_write(_file_write_input("/workspace/project/t4-b.txt", "b"), context())
    )
    await asyncio.sleep(0)
    assert provider.write_started == 1
    assert second.done() is False
    provider.write_release.set()
    assert (await first).is_error is False
    assert (await second).is_error is False
    assert provider.write_max_active == 1
    assert await manager.close("task-m5") is True


async def test_t5_canceled_provider_operation_fails_closed(tmp_path: Path) -> None:
    store = SQLiteSandboxBindingStore(tmp_path / "sandbox_bindings.db")
    repo = make_repo(tmp_path)
    provider = GatedWriteProvider(tmp_path / "provider")
    manager = _manager_for(tmp_path, provider)
    binding = await manager.ensure_binding(
        task_id="task-m5",
        source_repo=str(repo),
        context=exec_context(),
        worker_id="w1",
    )
    tools = SandboxWorkspaceTools(manager, "task-m5", provider)
    write = asyncio.create_task(
        tools.file_write(_file_write_input("/workspace/project/t5.txt", "x"), context())
    )
    await provider.write_entered.wait()
    write.cancel()
    with pytest.raises(asyncio.CancelledError):
        await write
    with pytest.raises(SandboxOwnershipError, match="uncertain"):
        manager.require_ready("task-m5")
    with pytest.raises(SandboxOwnershipError, match="uncertain"):
        await manager.release_ownership("task-m5")
    await manager.mark_lost("task-m5")
    durable = store.get(binding.binding_id)
    assert durable is not None
    assert durable.state == "LOST"
    assert provider.create_count == 1


async def test_t5_cancel_waits_for_inflight_provider_operation(
    tmp_path: Path,
) -> None:
    store = SQLiteSandboxBindingStore(tmp_path / "sandbox_bindings.db")
    repo = make_repo(tmp_path)
    provider = GatedWriteProvider(tmp_path / "provider")
    manager = _manager_for(tmp_path, provider)
    binding = await manager.ensure_binding(
        task_id="task-m5",
        source_repo=str(repo),
        context=exec_context(),
        worker_id="w1",
    )
    tools = SandboxWorkspaceTools(manager, "task-m5", provider)
    write = asyncio.create_task(
        tools.file_write(_file_write_input("/workspace/project/t5-cancel.txt", "x"), context())
    )
    await provider.write_entered.wait()
    cancel = asyncio.create_task(manager.cancel("task-m5"))
    await asyncio.sleep(0)
    assert cancel.done() is False
    provider.write_release.set()
    assert (await write).is_error is False
    await cancel
    durable = store.get(binding.binding_id)
    assert durable is not None
    assert durable.state == "CLOSED"
    assert provider.create_count == 1


async def test_t5_mark_lost_waits_for_inflight_provider_operation(
    tmp_path: Path,
) -> None:
    store = SQLiteSandboxBindingStore(tmp_path / "sandbox_bindings.db")
    repo = make_repo(tmp_path)
    provider = GatedWriteProvider(tmp_path / "provider")
    manager = _manager_for(tmp_path, provider)
    binding = await manager.ensure_binding(
        task_id="task-m5",
        source_repo=str(repo),
        context=exec_context(),
        worker_id="w1",
    )
    tools = SandboxWorkspaceTools(manager, "task-m5", provider)
    write = asyncio.create_task(
        tools.file_write(_file_write_input("/workspace/project/t5-lost.txt", "x"), context())
    )
    await provider.write_entered.wait()
    lost = asyncio.create_task(manager.mark_lost("task-m5"))
    await asyncio.sleep(0)
    assert lost.done() is False
    provider.write_release.set()
    assert (await write).is_error is False
    await lost
    durable = store.get(binding.binding_id)
    assert durable is not None
    assert durable.state == "LOST"
    assert provider.create_count == 1


async def test_t5_close_waits_for_inflight_provider_operation(tmp_path: Path) -> None:
    store = SQLiteSandboxBindingStore(tmp_path / "sandbox_bindings.db")
    repo = make_repo(tmp_path)
    provider = GatedWriteProvider(tmp_path / "provider")
    manager = _manager_for(tmp_path, provider)
    await manager.ensure_binding(
        task_id="task-m5",
        source_repo=str(repo),
        context=exec_context(),
        worker_id="w1",
    )
    tools = SandboxWorkspaceTools(manager, "task-m5", provider)
    write = asyncio.create_task(
        tools.file_write(_file_write_input("/workspace/project/t5-close.txt", "x"), context())
    )
    await provider.write_entered.wait()
    close = asyncio.create_task(manager.close("task-m5"))
    await asyncio.sleep(0)
    assert close.done() is False
    provider.write_release.set()
    result = await write
    assert result.is_error is False
    assert await close is True
    assert provider.destroy_count == 1
    durable = store.get_for_task("run-m5", "task-m5")
    assert durable is not None
    assert durable.state == "CLOSED"


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
    await manager.release_ownership("task-m5")
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
