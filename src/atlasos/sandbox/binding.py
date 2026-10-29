"""One task -> at most one AnyCode sandbox handle, with no host fallback."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime

from anycode.identity.context import ExecutionContext
from anycode.sandbox import SandboxProvider

from atlasos.sandbox.models import (
    ProjectSandboxBinding,
    SandboxBindingError,
    SandboxCapabilityError,
    SandboxLostError,
    SandboxProjectConfig,
    trusted_sandbox_spec,
)
from atlasos.sandbox.provision import GitProjectProvisioner


class ProjectSandboxManager:
    def __init__(
        self,
        provider: SandboxProvider,
        config: SandboxProjectConfig,
        *,
        run_id: str,
    ) -> None:
        config.validate_v1()
        self.provider = provider
        self.config = config
        self.run_id = run_id
        self.bindings: dict[str, ProjectSandboxBinding] = {}
        self._task_locks: dict[str, asyncio.Lock] = {}
        self.create_count = 0

    async def ensure_binding(
        self,
        *,
        task_id: str,
        source_repo: str,
        context: ExecutionContext,
        worker_id: str | None = None,
    ) -> ProjectSandboxBinding:
        lock = self._task_locks.setdefault(task_id, asyncio.Lock())
        async with lock:
            existing = self.bindings.get(task_id)
            if existing is not None:
                if existing.state == "READY":
                    return existing
                raise SandboxLostError(
                    f"task {task_id!r} is terminal ({existing.state}); M5 never recreates a sandbox"
                )

            GitProjectProvisioner.require_capabilities(self.provider)
            provisioner = GitProjectProvisioner(
                max_bundle_bytes=self.config.max_bundle_bytes,
                workspace_root=self.config.workspace_root,
            )
            source = provisioner.inspect(source_repo)
            spec = trusted_sandbox_spec(
                run_id=self.run_id,
                task_id=task_id,
                worker_id=worker_id,
                context=context,
                config=self.config,
            )
            created = await self.provider.create(spec)
            if not created.ok or created.handle is None:
                raise SandboxBindingError(
                    f"sandbox create failed; sandbox task fails closed: {created.error}"
                )
            self.create_count += 1
            handle = created.handle
            try:
                await provisioner.provision(self.provider, handle, source)
            except BaseException:
                await self._best_effort_destroy(handle)
                raise
            finally:
                source.bundle_path.unlink(missing_ok=True)
            binding = ProjectSandboxBinding(
                run_id=self.run_id,
                task_id=task_id,
                worker_id=worker_id,
                provider_name=self.config.provider_name,
                sandbox_handle=handle,
                workspace_root=self.config.workspace_root,
                source_commit=source.source_commit,
                source_digest=source.bundle_digest,
                bundle_digest=source.bundle_digest,
                state="READY",
                created_at=datetime.now(UTC),
            )
            self.bindings[task_id] = binding
            return binding

    async def _best_effort_destroy(self, handle) -> None:
        with contextlib.suppress(Exception):
            await self.provider.destroy(handle)

    def binding(self, task_id: str) -> ProjectSandboxBinding:
        binding = self.bindings.get(task_id)
        if binding is None:
            raise SandboxBindingError(f"no sandbox binding for task {task_id!r}")
        return binding

    def require_ready(self, task_id: str) -> ProjectSandboxBinding:
        binding = self.binding(task_id)
        if binding.state == "LOST":
            raise SandboxLostError(f"sandbox for task {task_id!r} is LOST")
        if binding.state != "READY":
            raise SandboxBindingError(
                f"sandbox for task {task_id!r} is {binding.state}; failing closed"
            )
        return binding

    async def mark_lost(self, task_id: str) -> None:
        binding = self.bindings.get(task_id)
        if binding is None:
            return
        self.bindings[task_id] = binding.model_copy(update={"state": "LOST"})

    async def close(self, task_id: str) -> None:
        binding = self.binding(task_id)
        if binding.state in ("LOST", "CLOSED"):
            return
        result = await self.provider.destroy(binding.sandbox_handle)
        state = "CLOSED" if result.ok else "LOST"
        self.bindings[task_id] = binding.model_copy(update={"state": state})

    async def cancel(self, task_id: str) -> None:
        binding = self.binding(task_id)
        await self.provider.cancel(binding.sandbox_handle)
        self.bindings[task_id] = binding.model_copy(update={"state": "CLOSED"})


def require_sandbox_capabilities(provider: SandboxProvider) -> None:
    capabilities = provider.capabilities()
    if capabilities.isolation == "process" or capabilities.networking != "none":
        raise SandboxCapabilityError(
            "sandbox provider cannot prove M5 v1 isolation/network requirements"
        )
