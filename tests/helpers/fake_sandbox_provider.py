"""Deterministic local FakeSandboxProvider for M5 verification."""

from __future__ import annotations

import asyncio
import re
import shlex
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

from anycode.sandbox import (
    SandboxActionResult,
    SandboxCapabilities,
    SandboxCommand,
    SandboxCommandResult,
    SandboxCreateResult,
    SandboxFileResult,
    SandboxHandle,
    SandboxHealth,
    SandboxOutputChunk,
    SandboxSpec,
)

from atlasos.sandbox.models import SandboxLostError
from atlasos.sandbox.reattachment import (
    SandboxNetworkPolicyUnavailable,
    SandboxNetworkPolicyViolation,
    SandboxReattachmentObservation,
    SandboxRemoteNotFound,
    atlas_metadata_digest,
    normalize_atlas_metadata,
)


class FakeSandboxProvider:
    def __init__(
        self,
        root: Path,
        *,
        capabilities: SandboxCapabilities | None = None,
        shell: str = "cmd",
    ) -> None:
        self.root = root
        self.shell = shell
        root.mkdir(parents=True, exist_ok=True)
        self.capabilities_value = capabilities or SandboxCapabilities(
            provider="fake",
            isolation="container",
            networking="none",
            persistent_filesystem=True,
            snapshots=False,
            command_streaming=False,
            cancellation=True,
            file_transfer=True,
            evidence=False,
        )
        self.create_count = 0
        self.destroy_count = 0
        self.execute_calls = 0
        self.read_calls = 0
        self.write_calls = 0
        self.cancel_calls = 0
        self.commands: list[tuple[str, str | None]] = []
        self.active_operations = 0
        self.max_active_operations = 0
        self.lost: set[str] = set()
        self.workspaces: dict[str, Path] = {}
        self.handles: dict[str, SandboxHandle] = {}
        self.specs: list[SandboxSpec] = []
        self.control_plane_digest = "sha256:fake-control-plane"
        self.remote_created_at: dict[str, datetime] = {}
        self.remote_metadata: dict[str, dict[str, str]] = {}
        self.remote_state: dict[str, str] = {}
        self.attached: set[str] = set()
        self.inspect_calls = 0
        self.attach_calls = 0
        self.detach_calls = 0
        self.inspect_error: Exception | None = None
        self.attach_error: Exception | None = None
        self.network_policies: dict[str, dict[str, object]] = {}
        self.network_policy_read_calls = 0
        self.network_policy_error: Exception | None = None
        self.fail_create = False
        self.fail_clone = False
        self.fail_operations = False
        self.fail_destroy = False
        self.fail_cancel = False
        self.raise_cancel = False
        self.raise_destroy = False
        self.detach_error: Exception | None = None

    def capabilities(self) -> SandboxCapabilities:
        return self.capabilities_value

    def _base(self, handle: SandboxHandle) -> Path:
        if handle.id in self.lost:
            raise SandboxLostError(f"sandbox {handle.id!r} is LOST")
        return self.workspaces[handle.id]

    def _map(self, handle: SandboxHandle, logical: str) -> Path:
        base = self._base(handle)
        if logical.startswith("/workspace/project"):
            suffix = logical[len("/workspace/project") :].lstrip("/")
            return base / "workspace" / "project" / suffix
        if logical.startswith("/tmp/"):
            return base / logical.lstrip("/")
        if logical.startswith("/"):
            return base / logical.lstrip("/")
        return base / "workspace" / "project" / logical

    @staticmethod
    def _posix_path(path: Path) -> str:
        text = path.resolve().as_posix()
        if len(text) >= 2 and text[1] == ":":
            return "/" + text[0].lower() + text[2:]
        return text

    def _map_command(self, handle: SandboxHandle, command: str) -> str:
        base = self._base(handle)
        project = base / "workspace" / "project"
        tmp_root = base / "tmp"

        def map_tmp(match: re.Match[str]) -> str:
            target = tmp_root / match.group(1)
            return self._posix_path(target) if self.shell == "git-bash" else str(target)

        if self.shell == "git-bash":
            mapped = command.replace("/workspace/project", self._posix_path(project))
            mapped = re.sub(r"/tmp/([A-Za-z0-9._-]+)", map_tmp, mapped)
            python = f'"{self._posix_path(Path(sys.executable))}"'
            mapped = mapped.replace("python3", "__ATLAS_PY__")
            mapped = re.sub(r"\bpython\b", "__ATLAS_PY__", mapped)
            return mapped.replace("__ATLAS_PY__", python)
        mapped = command.replace("/workspace/project", str(project))
        mapped = re.sub(r"/tmp/([A-Za-z0-9._-]+)", map_tmp, mapped)
        if mapped.startswith("python3 "):
            mapped = f'"{sys.executable}"' + mapped[len("python3") :]
        return mapped

    async def create(self, spec: SandboxSpec) -> SandboxCreateResult:
        self.specs.append(spec)
        if self.fail_create:
            return SandboxCreateResult(ok=False, error=None)
        self.create_count += 1
        handle = SandboxHandle(
            id=f"sandbox-{self.create_count}",
            provider="fake",
            run_id=spec.run_id,
            task_id=spec.task_id,
            correlation_id=spec.correlation_id,
            context=spec.context,
            capabilities=self.capabilities_value,
        )
        workspace = self.root / handle.id
        (workspace / "workspace" / "project").mkdir(parents=True, exist_ok=True)
        (workspace / "tmp").mkdir(parents=True, exist_ok=True)
        self.workspaces[handle.id] = workspace
        self.handles[handle.id] = handle
        metadata = {
            "atlas.run_id": spec.run_id,
            "atlas.task_id": spec.task_id or "none",
            "atlas.provider": "fake",
            "atlas.m5": "1",
            "atlas.config_digest": str(spec.labels.get("atlas_config_digest", "none")),
        }
        worker_id = spec.labels.get("atlas_worker_id")
        if worker_id is not None:
            metadata["atlas.worker_id"] = str(worker_id)
        self.remote_created_at[handle.id] = datetime.now(UTC)
        self.remote_metadata[handle.id] = metadata
        self.remote_state[handle.id] = "RUNNING"
        self.network_policies[handle.id] = {"default_action": "deny", "egress": []}
        return SandboxCreateResult(ok=True, handle=handle)

    def continuity_identity_digest(self) -> str:
        return self.control_plane_digest

    async def inspect_existing(self, sandbox_id: str) -> SandboxReattachmentObservation:
        self.inspect_calls += 1
        if self.inspect_error is not None:
            raise self.inspect_error
        if sandbox_id not in self.remote_state:
            raise SandboxRemoteNotFound(f"fake sandbox {sandbox_id!r} does not exist")
        metadata = self.remote_metadata[sandbox_id]
        return SandboxReattachmentObservation(
            sandbox_id=sandbox_id,
            state=self.remote_state[sandbox_id],
            created_at=self.remote_created_at[sandbox_id],
            metadata=normalize_atlas_metadata(metadata),
            metadata_digest=atlas_metadata_digest(metadata),
        )

    async def attach_existing(self, sandbox_id: str) -> None:
        self.attach_calls += 1
        if self.attach_error is not None:
            raise self.attach_error
        if sandbox_id not in self.remote_state:
            raise SandboxRemoteNotFound(f"fake sandbox {sandbox_id!r} does not exist")
        self.attached.add(sandbox_id)

    async def detach_local(self, sandbox_id: str) -> None:
        self.detach_calls += 1
        if self.detach_error is not None:
            raise self.detach_error
        self.attached.discard(sandbox_id)

    async def verify_network_none(self, sandbox_id: str) -> None:
        self.network_policy_read_calls += 1
        if self.network_policy_error is not None:
            raise self.network_policy_error
        policy = self.network_policies.get(sandbox_id)
        if policy is None:
            raise SandboxNetworkPolicyUnavailable(
                f"fake network policy is unavailable for {sandbox_id!r}"
            )
        default_action = policy.get("default_action") or policy.get("defaultAction")
        if default_action != "deny":
            raise SandboxNetworkPolicyViolation(
                f"fake sandbox {sandbox_id!r} default action is {default_action!r}"
            )
        rules = policy.get("egress") or []
        for rule in rules:  # type: ignore[union-attr]
            action = rule.get("action") if isinstance(rule, dict) else getattr(rule, "action", None)
            if action != "deny":
                raise SandboxNetworkPolicyViolation(
                    f"fake sandbox {sandbox_id!r} has non-deny egress rule {action!r}"
                )

    async def execute(self, handle: SandboxHandle, command: SandboxCommand) -> SandboxCommandResult:
        self.execute_calls += 1
        self.commands.append((command.command, command.cwd))
        self.active_operations += 1
        self.max_active_operations = max(self.max_active_operations, self.active_operations)
        try:
            if self.fail_operations:
                return SandboxCommandResult(ok=False, error=None)
            if handle.id in self.lost:
                raise SandboxLostError(f"sandbox {handle.id!r} is LOST")
            cwd = self._map(handle, command.cwd or "/workspace/project")
            mapped = self._map_command(handle, command.command)
            if self.fail_clone and "git clone" in mapped:
                return SandboxCommandResult(ok=False, exit_code=1, stderr="clone failed")
            git_bash = Path("C:/Program Files/Git/bin/bash.exe")  # noqa: ASYNC240
            if self.shell == "git-bash" and git_bash.is_file():  # noqa: ASYNC240
                process = await asyncio.create_subprocess_exec(
                    str(git_bash),
                    "-lc",
                    mapped,
                    cwd=str(cwd),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            else:
                process = await asyncio.create_subprocess_shell(
                    mapped,
                    cwd=str(cwd),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(), timeout=command.timeout_seconds or 30
                )
            except TimeoutError:
                process.kill()
                await process.wait()
                return SandboxCommandResult(
                    ok=False, exit_code=124, stderr="sandbox command timed out"
                )
            return SandboxCommandResult(
                ok=True,
                exit_code=process.returncode or 0,
                stdout=stdout.decode("utf-8", errors="replace"),
                stderr=stderr.decode("utf-8", errors="replace"),
            )
        finally:
            self.active_operations -= 1

    async def stream(self, handle: SandboxHandle, command: SandboxCommand):
        result = await self.execute(handle, command)
        if result.stdout:
            yield SandboxOutputChunk(stream="stdout", data=result.stdout, sequence=1)
        if result.stderr:
            yield SandboxOutputChunk(stream="stderr", data=result.stderr, sequence=2)

    async def write_file(self, handle: SandboxHandle, path: str, data: bytes) -> SandboxFileResult:
        self.write_calls += 1
        target = self._map(handle, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return SandboxFileResult(ok=True, data=data)

    async def read_file(self, handle: SandboxHandle, path: str) -> SandboxFileResult:
        self.read_calls += 1
        target = self._map(handle, path)
        if not target.is_file():
            return SandboxFileResult(ok=False)
        return SandboxFileResult(ok=True, data=target.read_bytes())

    async def cancel(self, handle: SandboxHandle) -> SandboxActionResult:
        self.cancel_calls += 1
        if self.raise_cancel:
            raise RuntimeError("provider cancel unavailable")
        if self.fail_cancel:
            return SandboxActionResult(ok=False)
        self.lost.add(handle.id)
        self.attached.discard(handle.id)
        self.remote_state.pop(handle.id, None)
        self.remote_created_at.pop(handle.id, None)
        self.remote_metadata.pop(handle.id, None)
        return SandboxActionResult(ok=True)

    async def snapshot(self, handle: SandboxHandle, name: str) -> SandboxActionResult:
        del handle, name
        return SandboxActionResult(ok=False)

    async def destroy(self, handle: SandboxHandle) -> SandboxActionResult:
        self.destroy_count += 1
        if self.raise_destroy:
            raise RuntimeError("provider destroy unavailable")
        if self.fail_destroy:
            return SandboxActionResult(ok=False)
        self.lost.add(handle.id)
        self.attached.discard(handle.id)
        self.remote_state.pop(handle.id, None)
        self.remote_created_at.pop(handle.id, None)
        self.remote_metadata.pop(handle.id, None)
        shutil.rmtree(self.workspaces.get(handle.id, self.root / handle.id), ignore_errors=True)
        return SandboxActionResult(ok=True)

    async def health(self) -> SandboxHealth:
        if self.lost:
            return SandboxHealth(status="degraded", message="some fake handles lost")
        return SandboxHealth(status="healthy")

    def lose(self, handle: SandboxHandle) -> None:
        self.lost.add(handle.id)

    def install_bundle(self, handle: SandboxHandle) -> None:
        source = self._base(handle) / "tmp" / "project.bundle"
        source.parent.mkdir(parents=True, exist_ok=True)

    def command_for_test(self, handle: SandboxHandle, command: str) -> str:
        return self._map_command(handle, command)

    def shell_quote(self, value: str) -> str:
        return shlex.quote(value)
