"""OpenSandbox adapter mapping tests against a fake SDK object."""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Literal

import pytest
from anycode.identity.context import ExecutionContext
from anycode.sandbox import SandboxCommand, SandboxProvider, SandboxSpec

from atlasos.sandbox import OpenSandboxOptions, OpenSandboxProvider
from atlasos.sandbox.models import SandboxLostError


class _Output:
    def __init__(self, text: str) -> None:
        self.text = text


class _Logs:
    def __init__(self, stdout: list[str], stderr: list[str]) -> None:
        self.stdout = [_Output(item) for item in stdout]
        self.stderr = [_Output(item) for item in stderr]


class _Execution:
    def __init__(self, *, exit_code: int = 0, stdout=(), stderr=()) -> None:
        self.logs = _Logs(list(stdout), list(stderr))
        self.exit_code = exit_code
        self.error = None


class _Commands:
    def __init__(self, sandbox: FakeSandbox) -> None:
        self.sandbox = sandbox

    async def run(self, command: str, *, opts=None):
        self.sandbox.runs.append((command, opts))
        if self.sandbox.fail_run:
            raise RuntimeError("remote command failed")
        return _Execution(
            exit_code=self.sandbox.exit_code,
            stdout=["hello\n"],
            stderr=["warning\n"] if self.sandbox.stderr else [],
        )


class _Files:
    def __init__(self, sandbox: FakeSandbox) -> None:
        self.sandbox = sandbox

    async def write_file(self, path: str, data: bytes, *, mode: int = 0o755) -> None:
        self.sandbox.writes.append((path, data, mode))

    async def read_bytes(self, path: str) -> bytes:
        self.sandbox.reads.append(path)
        return self.sandbox.file_data


class FakeSandbox:
    def __init__(self) -> None:
        self.id = "opensandbox-1"
        self.runs: list[tuple[str, Any]] = []
        self.writes: list[tuple[str, bytes, int]] = []
        self.reads: list[str] = []
        self.file_data = b"file-body"
        self.exit_code = 0
        self.fail_run = False
        self.stderr = True
        self.destroyed = False
        self.killed = False
        self.closed = False
        self.commands = _Commands(self)
        self.files = _Files(self)

    async def destroy(self) -> None:
        self.destroyed = True

    async def kill(self) -> None:
        self.killed = True

    async def close(self) -> None:
        self.closed = True


def _spec(
    *,
    network: Literal["none", "allowlist", "unrestricted"] = "none",
    allowed_domains: tuple[str, ...] = (),
) -> SandboxSpec:
    return SandboxSpec(
        run_id="run-os",
        task_id="task-os",
        correlation_id="run-os:task-os",
        context=ExecutionContext(principal="user:test", tenant_scope="test"),
        image="python:3.12",
        network=network,
        allowed_domains=allowed_domains,
        persistent=True,
        labels={"atlas_m5": "1"},
    )


async def _provider(fake: FakeSandbox, *, health: bool = True) -> OpenSandboxProvider:
    async def factory(spec: SandboxSpec) -> FakeSandbox:
        del spec
        return fake

    async def probe() -> tuple[bool, str]:
        return health, "healthy" if health else "unavailable"

    return OpenSandboxProvider(
        OpenSandboxOptions(endpoint="localhost:8080", timeout_minutes=20),
        sandbox_factory=factory,
        health_probe=probe,
    )


async def test_adapter_implements_provider_contract_with_truthful_capabilities() -> None:
    fake = FakeSandbox()
    provider = await _provider(fake)
    assert isinstance(provider, SandboxProvider)
    capabilities = provider.capabilities()
    assert capabilities.provider == "opensandbox"
    assert capabilities.isolation == "container"
    assert capabilities.networking == "allowlist"
    assert capabilities.file_transfer is True
    assert capabilities.snapshots is False
    assert capabilities.command_streaming is False
    assert (await provider.health()).status == "healthy"


async def test_create_maps_handle_and_caches_runtime_local_object() -> None:
    fake = FakeSandbox()
    provider = await _provider(fake)
    created = await provider.create(_spec())
    assert created.ok is True
    assert created.handle is not None
    handle = created.handle
    assert handle.id == "opensandbox-1"
    assert handle.provider == "opensandbox"
    assert handle.run_id == "run-os"
    assert handle.task_id == "task-os"
    assert handle.context.principal == "user:test"
    assert handle.metadata["runtime"] == "docker"


async def test_execute_maps_cwd_timeout_environment_and_exit_code() -> None:
    fake = FakeSandbox()
    provider = await _provider(fake)
    handle = (await provider.create(_spec())).handle
    assert handle is not None
    result = await provider.execute(
        handle,
        SandboxCommand(
            command="python3 -m pytest -q",
            cwd="/workspace/project",
            environment={"MODE": "uat"},
            timeout_seconds=42,
        ),
    )
    assert result.ok is True
    assert result.exit_code == 0
    assert result.stdout == "hello\n"
    assert result.stderr == "warning\n"
    command, opts = fake.runs[0]
    assert command == "python3 -m pytest -q"
    assert opts.working_directory == "/workspace/project"
    assert opts.timeout == timedelta(seconds=42)
    assert opts.envs == {"MODE": "uat"}

    fake.exit_code = 1
    failed = await provider.execute(
        handle, SandboxCommand(command="false", cwd="/workspace/project")
    )
    assert failed.ok is True
    assert failed.exit_code == 1


async def test_execute_transport_failure_is_a_contract_error() -> None:
    fake = FakeSandbox()
    fake.fail_run = True
    provider = await _provider(fake)
    handle = (await provider.create(_spec())).handle
    assert handle is not None
    result = await provider.execute(handle, SandboxCommand(command="boom"))
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "sandbox_command_failed"


async def test_file_read_write_round_trip_and_digest() -> None:
    fake = FakeSandbox()
    provider = await _provider(fake)
    handle = (await provider.create(_spec())).handle
    assert handle is not None
    written = await provider.write_file(handle, "/workspace/project/a.txt", b"payload")
    assert written.ok is True
    assert written.evidence is not None
    assert fake.writes == [("/workspace/project/a.txt", b"payload", 0o644)]
    read = await provider.read_file(handle, "/workspace/project/a.txt")
    assert read.ok is True
    assert read.data == b"file-body"
    assert fake.reads == ["/workspace/project/a.txt"]


async def test_destroy_and_cancel_release_runtime_local_cache() -> None:
    fake = FakeSandbox()
    provider = await _provider(fake)
    handle = (await provider.create(_spec())).handle
    assert handle is not None
    destroyed = await provider.destroy(handle)
    assert destroyed.ok is True and fake.destroyed is True
    with pytest.raises(SandboxLostError):
        await provider.execute(handle, SandboxCommand(command="echo no"))

    second = FakeSandbox()
    provider = await _provider(second)
    handle2 = (await provider.create(_spec())).handle
    assert handle2 is not None
    cancelled = await provider.cancel(handle2)
    assert cancelled.ok is True
    assert second.killed is True and second.closed is True


async def test_network_policy_mapping() -> None:
    pytest.importorskip("opensandbox")
    fake = FakeSandbox()
    provider = await _provider(fake)
    deny = provider._network_policy(_spec(network="none"))  # noqa: SLF001
    assert deny is not None
    assert deny.default_action == "deny"
    allow = provider._network_policy(  # noqa: SLF001
        _spec(network="allowlist", allowed_domains=("pypi.org",))
    )
    assert allow is not None
    assert allow.default_action == "deny"
    assert allow.egress is not None
    assert allow.egress[0].target == "pypi.org"
    with pytest.raises(ValueError, match="allowlist"):
        provider._network_policy(_spec(network="allowlist"))  # noqa: SLF001


async def test_create_rejects_unsupported_snapshot_and_secrets() -> None:
    fake = FakeSandbox()
    provider = await _provider(fake)
    snapshot = await provider.create(
        _spec().model_copy(update={"image": None, "snapshot": "snap-1"})
    )
    assert snapshot.ok is False
    assert snapshot.error is not None
    assert snapshot.error.code == "sandbox_snapshot_unsupported"
    secrets = await provider.create(
        _spec().model_copy(update={"secret_references": {"token": "opensandbox:name"}})
    )
    assert secrets.ok is False
    assert secrets.error is not None
    assert secrets.error.code == "sandbox_secrets_unsupported"


async def test_build_provider_selects_opensandbox_without_cloud_credentials() -> None:
    from atlasos.sandbox import build_sandbox_provider, is_real_sandbox_provider

    provider = build_sandbox_provider("opensandbox", environ={})
    assert isinstance(provider, OpenSandboxProvider)
    assert is_real_sandbox_provider(provider, expected_name="opensandbox") is True
