"""M5 project sandbox binding integration tests."""

from __future__ import annotations

import asyncio
import hashlib
import os
import random
import subprocess
from pathlib import Path

import pytest
from anycode.identity.context import ExecutionContext
from anycode.sandbox import SandboxCapabilities
from anycode.tools.bash import BashInput
from anycode.tools.built_in import BUILT_IN_TOOLS
from anycode.tools.idempotency import InMemoryToolIdempotencyStore
from anycode.types import AgentInfo, ToolSecurityPolicy, ToolUseContext

from atlasos.actions import ActionCatalog, ActionPolicy, ActionRule, StaticActionReviewer
from atlasos.ledger import SQLiteActionLedgerStore
from atlasos.sandbox import (
    ProjectSandboxManager,
    SandboxCapabilityError,
    SandboxLostError,
    SandboxProjectConfig,
    SandboxProviderError,
    SandboxProvisionError,
    SandboxWorkspaceTools,
    export_patch,
    export_workspace,
)
from atlasos.tools import AtlasToolRuntime
from atlasos.workers import ScopedWorkerFactory, WorkerAssignment, WorkerStore


def make_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    repo = root / "host-project"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "app.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (repo / "test_app.py").write_text(
        "from app import add\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=repo, check=True)
    return repo


def repo_digest(repo: Path) -> str:
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    digest = hashlib.sha256()
    for path in sorted(repo.rglob("*")):
        if path.is_file() and ".git" not in path.parts:
            digest.update(str(path.relative_to(repo)).encode())
            digest.update(path.read_bytes())
    return f"{head}:{digest.hexdigest()}"


def policy(*, allow: bool = True) -> ActionPolicy:
    rules = (
        (
            ActionRule(id="allow-read", decision="ALLOW", action="filesystem.read"),
            ActionRule(id="allow-write", decision="ALLOW", action="filesystem.write"),
            ActionRule(id="allow-shell", decision="ALLOW", action="shell.execute"),
        )
        if allow
        else ()
    )
    return ActionPolicy(name="m5", default="DENY", rules=rules)


def exec_context():
    ctx = context().execution_context
    assert ctx is not None
    return ctx


def context(*, workspace: str = "/workspace/project") -> ToolUseContext:
    return ToolUseContext(
        agent=AgentInfo(name="research_worker:w1", role="worker", model="fake"),
        execution_context=ExecutionContext(
            principal="user:test",
            attributes={"run_id": "run-m5", "worker_id": "w1"},
        ),
        security_policy=ToolSecurityPolicy(workspace_root=workspace),
    )


async def setup_stack(tmp_path: Path, *, max_bundle: int = 64 * 1024 * 1024, shell: str = "cmd"):
    from tests.helpers.fake_sandbox_provider import FakeSandboxProvider

    repo = make_repo(tmp_path)
    provider = FakeSandboxProvider(tmp_path / "provider", shell=shell)
    config = SandboxProjectConfig(
        provider_name="fake",
        image="fake@sha256:0123456789abcdef",
        max_bundle_bytes=max_bundle,
    )
    manager = ProjectSandboxManager(provider, config, run_id="run-m5")
    binding = await manager.ensure_binding(
        task_id="task-m5",
        source_repo=str(repo),
        context=exec_context(),
        worker_id="w1",
    )
    tools = SandboxWorkspaceTools(manager, "task-m5", provider)
    runtime = AtlasToolRuntime(
        "run-m5",
        sandbox_tools=tools,
        action_policy=policy(),
        action_catalog=ActionCatalog(),
        action_reviewer=StaticActionReviewer("CONSISTENT"),
        idempotency_store=InMemoryToolIdempotencyStore(),
        action_ledger=SQLiteActionLedgerStore(tmp_path / "ledger.db"),
    )
    return repo, provider, manager, binding, tools, runtime


async def test_m5_provision_six_tools_and_host_isolation(tmp_path: Path) -> None:
    repo, provider, manager, binding, tools, runtime = await setup_stack(tmp_path)
    before = repo_digest(repo)
    ctx = context()
    assert binding.state == "READY"
    assert provider.specs[0].network == "none"
    assert provider.specs[0].secret_references == {}
    assert binding.sandbox_handle.id in provider.workspaces

    read = await runtime.executor.execute("file_read", {"path": "/workspace/project/app.py"}, ctx)
    assert read.is_error is not True
    assert "return a - b" in read.data

    write = await runtime.executor.execute(
        "file_write",
        {"path": "/workspace/project/fix.py", "content": "fixed\n"},
        ctx,
        idempotency_key="write-1",
    )
    assert write.is_error is not True
    assert (
        provider.workspaces[binding.sandbox_handle.id] / "workspace/project/fix.py"
    ).read_text() == "fixed\n"
    assert (repo / "fix.py").exists() is False

    edit = await runtime.executor.execute(
        "file_edit",
        {
            "path": "/workspace/project/app.py",
            "old_string": "return a - b",
            "new_string": "return a + b",
        },
        ctx,
        idempotency_key="edit-1",
    )
    assert edit.is_error is not True
    assert (
        "return a + b"
        in (provider.workspaces[binding.sandbox_handle.id] / "workspace/project/app.py").read_text()
    )

    grep = await runtime.executor.execute(
        "grep", {"pattern": "return a \\+ b", "path": "/workspace/project"}, ctx
    )
    assert grep.is_error is not True
    assert "app.py" in grep.data
    listed = await runtime.executor.execute("list_files", {"path": "/workspace/project"}, ctx)
    assert listed.is_error is not True
    assert "fix.py" in listed.data
    shell = await runtime.executor.execute(
        "bash", {"command": 'python3 -c "print(1)"'}, ctx, idempotency_key="bash-1"
    )
    assert shell.is_error is not True
    assert repo_digest(repo) == before


async def test_m5_m3_deny_and_hitl_reject_prevent_sandbox_write(tmp_path: Path) -> None:
    repo, provider, manager, binding, tools, _ = await setup_stack(tmp_path)
    deny_runtime = AtlasToolRuntime(
        "deny",
        sandbox_tools=tools,
        action_policy=ActionPolicy(name="deny", default="DENY", rules=()),
        action_catalog=ActionCatalog(),
    )
    write_calls = provider.write_calls
    denied = await deny_runtime.executor.execute(
        "file_write",
        {"path": "/workspace/project/denied.py", "content": "x"},
        context(),
        idempotency_key="denied",
    )
    assert denied.is_error is True
    assert provider.write_calls == write_calls
    assert not (repo / "denied.py").exists()


async def test_m5_lost_binding_does_not_recreate_or_host_fallback(tmp_path: Path) -> None:
    repo, provider, manager, binding, tools, runtime = await setup_stack(tmp_path)
    provider.lose(binding.sandbox_handle)
    result = await runtime.executor.execute(
        "file_read", {"path": "/workspace/project/app.py"}, context()
    )
    assert result.is_error is True
    await manager.mark_lost("task-m5")
    with pytest.raises(SandboxLostError):
        await manager.ensure_binding(
            task_id="task-m5",
            source_repo=str(repo),
            context=exec_context(),
        )
    assert provider.create_count == 1
    assert repo.exists()


async def test_m5_concurrent_binding_single_flight_and_different_tasks(tmp_path: Path) -> None:
    repo, provider, manager, binding, _, _ = await setup_stack(tmp_path)
    # A second task can be created concurrently; task-m5 must not create again.
    results = await asyncio.gather(
        *(
            manager.ensure_binding(
                task_id="task-race",
                source_repo=str(repo),
                context=exec_context(),
            )
            for _ in range(20)
        )
    )
    assert len({item.sandbox_handle.id for item in results}) == 1
    assert provider.create_count == 2  # pre-created task-m5 + one task-race
    other = await manager.ensure_binding(
        task_id="task-other", source_repo=str(repo), context=exec_context()
    )
    assert other.sandbox_handle.id != results[0].sandbox_handle.id


async def test_m5_clean_repo_required_and_size_bound(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    (repo / "app.py").write_text("dirty\n", encoding="utf-8")
    from tests.helpers.fake_sandbox_provider import FakeSandboxProvider

    provider = FakeSandboxProvider(tmp_path / "provider")
    manager = ProjectSandboxManager(
        provider,
        SandboxProjectConfig(
            provider_name="fake", image="fake@sha256:fixed", max_bundle_bytes=1024
        ),
        run_id="run",
    )
    with pytest.raises(SandboxProvisionError, match="dirty working trees"):
        await manager.ensure_binding(task_id="dirty", source_repo=str(repo), context=exec_context())
    assert provider.create_count == 0


async def test_m5_capability_mismatch_fails_closed(tmp_path: Path) -> None:
    from tests.helpers.fake_sandbox_provider import FakeSandboxProvider

    repo = make_repo(tmp_path)
    bad = FakeSandboxProvider(
        tmp_path / "bad",
        capabilities=SandboxCapabilities(
            provider="fake",
            isolation="process",
            networking="allowlist",
            persistent_filesystem=True,
            snapshots=False,
            command_streaming=False,
            cancellation=True,
            file_transfer=True,
            evidence=False,
        ),
    )
    manager = ProjectSandboxManager(
        bad,
        SandboxProjectConfig(provider_name="fake", image="fake@sha256:fixed"),
        run_id="run",
    )
    with pytest.raises(SandboxCapabilityError):
        await manager.ensure_binding(task_id="bad", source_repo=str(repo), context=exec_context())
    assert bad.create_count == 0


async def test_m5_patch_export_and_host_unchanged(tmp_path: Path) -> None:
    repo, provider, manager, binding, tools, runtime = await setup_stack(tmp_path)
    before = repo_digest(repo)
    await runtime.executor.execute(
        "file_write",
        {"path": "/workspace/project/new.py", "content": "new\n"},
        context(),
        idempotency_key="new-file",
    )
    await runtime.executor.execute(
        "file_edit",
        {
            "path": "/workspace/project/app.py",
            "old_string": "return a - b",
            "new_string": "return a + b",
        },
        context(),
        idempotency_key="edit",
    )
    patch = await export_patch(provider, binding.sandbox_handle, base_commit=binding.source_commit)
    assert patch.patch_text
    assert patch.patch_digest.startswith("sha256:")
    assert patch.base_commit == binding.source_commit
    assert patch.changed_file_count >= 2
    assert repo_digest(repo) == before
    assert not (repo / "new.py").exists()


async def test_m5_m4_replay_conflict_and_read_not_cached(tmp_path: Path) -> None:
    repo, provider, manager, binding, tools, runtime = await setup_stack(tmp_path)
    before_writes = provider.write_calls
    first = await runtime.executor.execute(
        "file_write",
        {"path": "/workspace/project/replay.txt", "content": "same"},
        context(),
        idempotency_key="write-replay",
    )
    assert first.is_error is not True
    second = await runtime.executor.execute(
        "file_write",
        {"path": "/workspace/project/replay.txt", "content": "same"},
        context(),
        idempotency_key="write-replay",
    )
    assert second.is_error is not True
    assert provider.write_calls == before_writes + 1
    ledger = runtime.action_ledger
    assert ledger is not None
    assert ledger.list()[-1].completion_kind == "REPLAYED"

    conflict = await runtime.executor.execute(
        "file_write",
        {"path": "/workspace/project/replay.txt", "content": "different"},
        context(),
        idempotency_key="write-replay",
    )
    assert conflict.is_error is True
    assert provider.write_calls == before_writes + 1
    assert ledger.list()[-1].completion_kind == "CONFLICT"

    before_reads = len(ledger.list())
    await runtime.executor.execute("file_read", {"path": "/workspace/project/app.py"}, context())
    await runtime.executor.execute(
        "grep", {"pattern": "def", "path": "/workspace/project"}, context()
    )
    await runtime.executor.execute("list_files", {"path": "/workspace/project"}, context())
    assert len(ledger.list()) == before_reads


async def test_m5_m3_hitl_and_deny_prevent_sandbox_execution(tmp_path: Path) -> None:
    repo, provider, manager, binding, tools, _ = await setup_stack(tmp_path)
    writes = provider.write_calls
    human = AtlasToolRuntime(
        "human",
        sandbox_tools=tools,
        action_policy=ActionPolicy(
            name="human",
            default="REQUIRE_HUMAN",
            rules=(
                ActionRule(id="human-write", decision="REQUIRE_HUMAN", action="filesystem.write"),
            ),
        ),
        action_catalog=ActionCatalog(),
    )
    rejected = await human.executor.execute(
        "file_write", {"path": "/workspace/project/hitl.py", "content": "x"}, context()
    )
    assert rejected.is_error is True
    assert provider.write_calls == writes
    executes = provider.execute_calls
    deny = AtlasToolRuntime(
        "deny-bash",
        sandbox_tools=tools,
        action_policy=ActionPolicy(name="deny", default="DENY", rules=()),
        action_catalog=ActionCatalog(),
    )
    denied_bash = await deny.executor.execute(
        "bash", {"command": "echo no"}, context(), idempotency_key="deny-bash"
    )
    assert denied_bash.is_error is True
    assert provider.execute_calls == executes


async def test_m5_path_escape_cwd_escape_and_missing_binding(tmp_path: Path) -> None:
    repo, provider, manager, binding, tools, runtime = await setup_stack(tmp_path)
    reads = provider.read_calls
    escape = await runtime.executor.execute("file_read", {"path": "/etc/passwd"}, context())
    assert escape.is_error is True
    assert provider.read_calls == reads
    traversal = await runtime.executor.execute(
        "file_write",
        {"path": "/workspace/project/../escape.py", "content": "x"},
        context(),
        idempotency_key="traversal",
    )
    assert traversal.is_error is True
    executes = provider.execute_calls
    cwd_escape = await runtime.executor.execute(
        "bash", {"command": "echo no", "cwd": "/"}, context(), idempotency_key="cwd"
    )
    assert cwd_escape.is_error is True
    assert provider.execute_calls == executes

    missing = SandboxWorkspaceTools(manager, "missing-task", provider)
    writes = provider.write_calls
    result = await missing.file_write(
        type("V", (), {"path": "/workspace/project/x.py", "content": "x"})(), context()
    )
    assert result.is_error is True
    assert provider.write_calls == writes


async def test_m5_cancel_lost_and_no_recreate(tmp_path: Path) -> None:
    repo, provider, manager, binding, tools, runtime = await setup_stack(tmp_path)
    provider.lose(binding.sandbox_handle)
    lost = await runtime.executor.execute(
        "file_read", {"path": "/workspace/project/app.py"}, context()
    )
    assert lost.is_error is True
    assert manager.binding("task-m5").state == "LOST"
    creates = provider.create_count
    with pytest.raises(SandboxLostError):
        await manager.ensure_binding(
            task_id="task-m5", source_repo=str(repo), context=exec_context()
        )
    assert provider.create_count == creates
    failed_read = await tools.file_read(
        type("V", (), {"path": "/workspace/project/app.py"})(), context()
    )
    assert failed_read.is_error is True

    other_repo = make_repo(tmp_path / "cancel-repo")
    other = await manager.ensure_binding(
        task_id="cancel-task", source_repo=str(other_repo), context=exec_context()
    )
    assert other.state == "READY"
    await manager.cancel("cancel-task")
    assert manager.binding("cancel-task").state == "CLOSED"
    with pytest.raises(SandboxLostError):
        await manager.ensure_binding(
            task_id="cancel-task", source_repo=str(other_repo), context=exec_context()
        )


async def test_m5_registry_schema_and_no_alternate_tools(tmp_path: Path) -> None:
    repo, provider, manager, binding, tools, runtime = await setup_stack(tmp_path)
    names = {tool.name for tool in runtime.registry.list()}
    assert {"bash", "file_read", "file_write", "file_edit", "grep", "list_files"} <= names
    assert "filesystem.write" not in names
    assert "shell.execute" not in names
    assert not any(name.startswith("sandbox.") for name in names)
    originals = {tool.name: tool for tool in BUILT_IN_TOOLS}
    for name in ("bash", "file_read", "file_write", "file_edit", "grep", "list_files"):
        current = runtime.registry.get(name)
        assert current is not None
        assert current.input_model is originals[name].input_model
        assert current.description == originals[name].description
        assert current.side_effecting == originals[name].side_effecting


async def test_m5_output_cap_and_timeout(tmp_path: Path) -> None:
    repo, provider, manager, binding, tools, runtime = await setup_stack(tmp_path)
    large = await runtime.executor.execute(
        "bash",
        {"command": "python3 -c \"print('x' * 10000)\"", "max_output_bytes": 100},
        context(),
        idempotency_key="large",
    )
    assert large.is_error is not True
    assert "output truncated" in large.data
    slow = await runtime.executor.execute(
        "bash",
        {"command": 'python3 -c "import time; time.sleep(2)"', "timeout": 0.2},
        context(),
        idempotency_key="slow",
    )
    assert slow.is_error is True


async def test_m5_same_binding_operations_are_serialized(tmp_path: Path) -> None:
    repo, provider, manager, binding, tools, runtime = await setup_stack(tmp_path)
    before = provider.max_active_operations
    for index in range(20):
        await asyncio.gather(
            runtime.executor.execute(
                "file_write",
                {"path": f"/workspace/project/race-{index}.py", "content": "x"},
                context(),
                idempotency_key=f"serial-{index}",
            ),
            runtime.executor.execute(
                "bash",
                {"command": "python3 -c \"print('parallel')\""},
                context(),
                idempotency_key=f"serial-bash-{index}",
            ),
        )
    assert provider.max_active_operations <= 1
    assert provider.max_active_operations >= before


async def test_m5_oversize_bundle_rejected_before_create(tmp_path: Path) -> None:
    from tests.helpers.fake_sandbox_provider import FakeSandboxProvider

    repo = make_repo(tmp_path)
    (repo / "huge.bin").write_bytes(random.Random(0).randbytes(100_000))
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)  # noqa: ASYNC221
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "huge"], check=True)  # noqa: ASYNC221
    provider = FakeSandboxProvider(tmp_path / "provider")
    manager = ProjectSandboxManager(
        provider,
        SandboxProjectConfig(
            provider_name="fake", image="fake@sha256:fixed", max_bundle_bytes=1024
        ),
        run_id="run",
    )
    with pytest.raises(SandboxProvisionError, match="max_bundle_bytes"):
        await manager.ensure_binding(task_id="huge", source_repo=str(repo), context=exec_context())
    assert provider.create_count == 0


async def test_m5_model_cannot_control_trusted_sandbox_config() -> None:
    with pytest.raises(ValueError):
        SandboxProjectConfig(provider_name="fake", image="latest").validate_v1()
    with pytest.raises(ValueError):
        SandboxProjectConfig(
            provider_name="fake",
            image="fake@sha256:fixed",
            workspace_root="/",
        ).validate_v1()
    config = SandboxProjectConfig(provider_name="fake", image="fake@sha256:fixed")
    assert config.network == "none"
    assert config.secret_references == {}
    assert config.workspace_root == "/workspace/project"


async def test_m5_worker_metadata_uses_sandbox_tools(tmp_path: Path) -> None:
    repo, provider, manager, binding, tools, runtime = await setup_stack(tmp_path)
    factory = ScopedWorkerFactory(
        model="fake",
        provider="fake",
        store=WorkerStore(tmp_path / "workers.json"),
        available_tools=set(runtime.agent_tools),
    )
    parent = exec_context()
    assert parent is not None
    worker = factory.create(
        parent_context=parent,
        parent_agent="planner",
        assignment=WorkerAssignment(profile_id="research_worker", task="read project"),
        worker_id="w-sandbox",
        task_id="task-worker",
    )
    result = await runtime.executor.execute(
        "file_read",
        {"path": "/workspace/project/app.py"},
        worker.tool_context(),
    )
    assert result.is_error is not True
    assert "return a - b" in result.data


async def test_m5_different_bindings_can_run_concurrently(tmp_path: Path) -> None:
    from tests.helpers.fake_sandbox_provider import FakeSandboxProvider

    repo = make_repo(tmp_path)
    provider = FakeSandboxProvider(tmp_path / "provider")
    config = SandboxProjectConfig(provider_name="fake", image="fake@sha256:fixed")
    manager = ProjectSandboxManager(provider, config, run_id="run-multi")
    tools_a = SandboxWorkspaceTools(manager, "task-a", provider)
    tools_b = SandboxWorkspaceTools(manager, "task-b", provider)
    await manager.ensure_binding(task_id="task-a", source_repo=str(repo), context=exec_context())
    await manager.ensure_binding(task_id="task-b", source_repo=str(repo), context=exec_context())
    command = BashInput(command='python3 -c "import time; time.sleep(0.1)"')
    await asyncio.gather(
        tools_a.bash(command, context()),
        tools_b.bash(command, context()),
    )
    assert provider.max_active_operations >= 2


async def test_m5_allowlist_capability_can_enforce_network_none(tmp_path: Path) -> None:
    from tests.helpers.fake_sandbox_provider import FakeSandboxProvider

    repo = make_repo(tmp_path)
    provider = FakeSandboxProvider(
        tmp_path / "provider",
        capabilities=SandboxCapabilities(
            provider="fake",
            isolation="remote",
            networking="allowlist",
            persistent_filesystem=True,
            snapshots=False,
            command_streaming=True,
            cancellation=True,
            file_transfer=True,
            evidence=True,
        ),
    )
    manager = ProjectSandboxManager(
        provider,
        SandboxProjectConfig(provider_name="fake", image="fake@sha256:fixed"),
        run_id="run-allowlist",
    )
    binding = await manager.ensure_binding(
        task_id="task-allowlist", source_repo=str(repo), context=exec_context()
    )
    assert binding.state == "READY"
    assert provider.specs[0].network == "none"
    assert provider.create_count == 1


async def test_m5_provider_identity_mismatch_fails_closed(tmp_path: Path) -> None:
    from tests.helpers.fake_sandbox_provider import FakeSandboxProvider

    repo = make_repo(tmp_path)
    provider = FakeSandboxProvider(tmp_path / "provider")
    manager = ProjectSandboxManager(
        provider,
        SandboxProjectConfig(provider_name="daytona", image="python:3.12"),
        run_id="run-mismatch",
    )
    with pytest.raises(SandboxProviderError, match="provider identity mismatch"):
        await manager.ensure_binding(
            task_id="task-mismatch", source_repo=str(repo), context=exec_context()
        )
    assert provider.create_count == 0


async def test_m5_workspace_export_is_explicit_and_host_unchanged(tmp_path: Path) -> None:
    repo, provider, manager, binding, tools, runtime = await setup_stack(tmp_path)
    before = repo_digest(repo)
    await runtime.executor.execute(
        "file_write",
        {"path": "/workspace/project/new.py", "content": "new\n"},
        context(),
        idempotency_key="export-new",
    )
    await runtime.executor.execute(
        "file_edit",
        {
            "path": "/workspace/project/app.py",
            "old_string": "return a - b",
            "new_string": "return a + b",
        },
        context(),
        idempotency_key="export-edit",
    )
    exported = await export_workspace(
        provider,
        binding.sandbox_handle,
        destination=tmp_path / "exported",
    )
    assert exported.file_count == 3
    assert (tmp_path / "exported" / "new.py").read_text() == "new\n"
    assert "return a + b" in (tmp_path / "exported" / "app.py").read_text()
    assert not (tmp_path / "exported" / ".git").exists()
    assert repo_digest(repo) == before
    assert not (repo / "new.py").exists()


def _capture_first_binding(tools: SandboxWorkspaceTools) -> asyncio.Event:
    captured = asyncio.Event()
    original = tools._binding

    def observe():
        binding = original()
        captured.set()
        return binding

    tools._binding = observe  # type: ignore[method-assign]
    return captured


async def test_m5_file_write_quotes_model_path_against_shell_injection(
    tmp_path: Path,
) -> None:
    git_bash = Path("C:/Program Files/Git/bin/bash.exe")
    if os.name == "nt" and not git_bash.is_file():  # noqa: ASYNC240 - test preflight
        pytest.skip("git-bash is required for the POSIX shell injection regression")
    shell = "git-bash" if os.name == "nt" else "cmd"
    repo, provider, manager, binding, tools, runtime = await setup_stack(tmp_path, shell=shell)
    workspace = provider.workspaces[binding.sandbox_handle.id] / "workspace" / "project"
    before_repo = repo_digest(repo)
    cases = (
        (
            '/workspace/project/a"; touch PWN_SEMICOLON; echo "/file.txt',
            "PWN_SEMICOLON",
        ),
        (
            "/workspace/project/a $(touch PWN_SUBST) `touch PWN_BACKTICK`/file.txt",
            "PWN_SUBST",
        ),
        (
            "/workspace/project/a' ; touch PWN_QUOTE; echo '/file.txt",
            "PWN_QUOTE",
        ),
    )
    for index, (path, sentinel) in enumerate(cases):
        result = await runtime.executor.execute(
            "file_write",
            {"path": path, "content": "safe\n"},
            context(),
            idempotency_key=f"injection-{index}",
        )
        assert not (workspace / sentinel).exists(), f"shell injection created {sentinel}"
        if result.is_error is not True:
            assert (workspace / sentinel).exists() is False
    assert repo_digest(repo) == before_repo
    assert provider.commands
    mkdir_commands = [
        command for command, _ in provider.commands if command.startswith("mkdir -p --")
    ]
    assert mkdir_commands
    assert all(command.startswith("mkdir -p -- '") for command in mkdir_commands)
    assert any("touch PWN_SEMICOLON" in command for command in mkdir_commands)


async def test_m5_lost_while_waiting_lock_blocks_stale_read(tmp_path: Path) -> None:
    repo, provider, manager, binding, tools, runtime = await setup_stack(tmp_path)
    del repo
    before_reads = provider.read_calls
    captured = _capture_first_binding(tools)
    await tools.lock.acquire()
    task = asyncio.create_task(
        runtime.executor.execute("file_read", {"path": "/workspace/project/app.py"}, context())
    )
    try:
        await asyncio.wait_for(captured.wait(), timeout=2)
        await manager.mark_lost("task-m5")
    finally:
        tools.lock.release()
    result = await task
    assert result.is_error is True
    assert provider.read_calls == before_reads
    assert manager.binding("task-m5").state == "LOST"
    assert provider.create_count == 1


async def test_m5_lost_while_waiting_lock_blocks_stale_write(tmp_path: Path) -> None:
    repo, provider, manager, binding, tools, runtime = await setup_stack(tmp_path)
    del repo
    before_writes = provider.write_calls
    captured = _capture_first_binding(tools)
    await tools.lock.acquire()
    task = asyncio.create_task(
        runtime.executor.execute(
            "file_write",
            {"path": "/workspace/project/race.txt", "content": "stale\n"},
            context(),
            idempotency_key="lost-race-write",
        )
    )
    try:
        await asyncio.wait_for(captured.wait(), timeout=2)
        await manager.mark_lost("task-m5")
    finally:
        tools.lock.release()
    result = await task
    assert result.is_error is True
    assert provider.write_calls == before_writes
    assert manager.binding("task-m5").state == "LOST"
    assert provider.create_count == 1


async def test_m5_lost_while_waiting_lock_blocks_stale_command(tmp_path: Path) -> None:
    repo, provider, manager, binding, tools, runtime = await setup_stack(tmp_path)
    del repo
    before_executes = provider.execute_calls
    captured = _capture_first_binding(tools)
    await tools.lock.acquire()
    task = asyncio.create_task(
        tools.bash(
            BashInput(command="echo should-not-run", cwd="/workspace/project"),
            context(),
        )
    )
    try:
        await asyncio.wait_for(captured.wait(), timeout=2)
        await manager.mark_lost("task-m5")
    finally:
        tools.lock.release()
    result = await task
    assert result.is_error is True
    assert provider.execute_calls == before_executes
    assert manager.binding("task-m5").state == "LOST"
    assert provider.create_count == 1


async def test_m5_cancel_provider_failure_is_not_closed(tmp_path: Path) -> None:
    repo, provider, manager, binding, tools, runtime = await setup_stack(tmp_path)
    del repo, binding, tools, runtime
    provider.fail_cancel = True
    with pytest.raises(SandboxProviderError, match="cancel failed"):
        await manager.cancel("task-m5")
    assert manager.binding("task-m5").state == "LOST"
    assert provider.cancel_calls == 1
    assert provider.create_count == 1


async def test_m5_cancel_provider_exception_is_not_closed(tmp_path: Path) -> None:
    repo, provider, manager, binding, tools, runtime = await setup_stack(tmp_path)
    del repo, binding, tools, runtime
    provider.raise_cancel = True
    with pytest.raises(SandboxProviderError, match="cancel failed"):
        await manager.cancel("task-m5")
    assert manager.binding("task-m5").state == "LOST"
    assert provider.cancel_calls == 1
    assert provider.create_count == 1
