"""One task -> at most one AnyCode sandbox handle, with no host fallback."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING

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
from atlasos.trajectory.models import (
    TrajectoryCorrelation,
    derive_sandbox_binding_id,
)

if TYPE_CHECKING:
    from atlasos.trajectory.recorder import TrajectoryRecorder


class ProjectSandboxManager:
    def __init__(
        self,
        provider: SandboxProvider,
        config: SandboxProjectConfig,
        *,
        run_id: str,
        trajectory_recorder: TrajectoryRecorder | None = None,
    ) -> None:
        config.validate_v1()
        self.provider = provider
        self.config = config
        self.run_id = run_id
        self.trajectory_recorder = trajectory_recorder
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
            self._record_lifecycle(binding, "READY", occurred_at=binding.created_at)
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
        updated = binding.model_copy(update={"state": "LOST"})
        self.bindings[task_id] = updated
        self._record_lifecycle(updated, "LOST", occurred_at=None)

    async def close(self, task_id: str) -> None:
        binding = self.binding(task_id)
        if binding.state in ("LOST", "CLOSED"):
            return
        result = await self.provider.destroy(binding.sandbox_handle)
        state = "CLOSED" if result.ok else "LOST"
        updated = binding.model_copy(update={"state": state})
        self.bindings[task_id] = updated
        self._record_lifecycle(updated, state, occurred_at=None)

    async def cancel(self, task_id: str) -> None:
        binding = self.binding(task_id)
        await self.provider.cancel(binding.sandbox_handle)
        updated = binding.model_copy(update={"state": "CLOSED"})
        self.bindings[task_id] = updated
        self._record_lifecycle(updated, "CLOSED", occurred_at=None)

    def _record_lifecycle(
        self,
        binding: ProjectSandboxBinding,
        state: str,
        *,
        occurred_at: datetime | None,
    ) -> None:
        recorder = self.trajectory_recorder
        if recorder is None:
            return
        binding_id = derive_sandbox_binding_id(
            run_id=binding.run_id,
            task_id=binding.task_id,
            worker_id=binding.worker_id,
            provider_name=binding.provider_name,
            sandbox_handle_id=binding.sandbox_handle.id,
        )
        try:  # noqa: SIM105
            recorder.record_sandbox_lifecycle(
                binding_id=binding_id,
                task_id=binding.task_id,
                worker_id=binding.worker_id,
                provider_name=binding.provider_name,
                state=state,
                source_commit=binding.source_commit,
                occurred_at=occurred_at,
                correlation=TrajectoryCorrelation(
                    sandbox_binding_id=binding_id,
                    worker_id=binding.worker_id,
                ),
            )
        except Exception:  # noqa: BLE001 - evidence is optional
            pass


def require_sandbox_capabilities(provider: SandboxProvider) -> None:
    capabilities = provider.capabilities()
    if capabilities.isolation == "process" or capabilities.networking != "none":
        raise SandboxCapabilityError(
            "sandbox provider cannot prove M5 v1 isolation/network requirements"
        )
