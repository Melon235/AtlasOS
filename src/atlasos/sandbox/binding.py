"""One task -> at most one durable Atlas sandbox binding, with no host fallback.

The SQLite durable store owns the M5 task/sandbox relationship and lifecycle state. The in-process
``bindings`` dictionary is only a live-handle cache; it can never override durable truth.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from anycode.identity.context import ExecutionContext
from anycode.sandbox import SandboxProvider

from atlasos.sandbox.models import (
    DurableSandboxBindingRecord,
    ProjectSandboxBinding,
    SandboxBindingError,
    SandboxLostError,
    SandboxProjectConfig,
    SandboxProviderError,
    SandboxReattachmentRequired,
    compute_sandbox_config_digest,
    compute_sandbox_continuity_digest,
    trusted_sandbox_spec,
)
from atlasos.sandbox.provider import (
    capability_digest,
    capability_summary,
    require_capabilities,
)
from atlasos.sandbox.provision import GitProjectProvisioner
from atlasos.sandbox.store import (
    SandboxBindingConflict,
    SandboxBindingStateError,
    SandboxBindingStore,
)
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
        binding_store: SandboxBindingStore | None = None,
        trajectory_recorder: TrajectoryRecorder | None = None,
    ) -> None:
        config.validate_v1()
        self.provider = provider
        self.config = config
        self.run_id = run_id
        self.binding_store = binding_store
        self.trajectory_recorder = trajectory_recorder
        # Runtime-local live-handle cache, never the durable M5 authority.
        self.bindings: dict[str, ProjectSandboxBinding] = {}
        self._task_locks: dict[str, asyncio.Lock] = {}
        self.config_digest = compute_sandbox_config_digest(config)
        self.create_count = 0
        self.destroy_count = 0
        self.destroyed_count = 0
        self.provision_commands = 0
        self.provision_uploads = 0

    def _durable(self, task_id: str) -> DurableSandboxBindingRecord | None:
        if self.binding_store is None:
            return None
        return self.binding_store.get_for_task(self.run_id, task_id)

    def _require_durable(self, task_id: str) -> DurableSandboxBindingRecord:
        durable = self._durable(task_id)
        if durable is None:
            raise SandboxBindingError(
                f"durable sandbox binding is missing for task {task_id!r}; failing closed"
            )
        return durable

    @staticmethod
    def _validate_runtime_identity(
        binding: ProjectSandboxBinding, durable: DurableSandboxBindingRecord
    ) -> None:
        mismatches = []
        checks = (
            ("binding_id", binding.binding_id, durable.binding_id),
            ("run_id", binding.run_id, durable.run_id),
            ("task_id", binding.task_id, durable.task_id),
            ("worker_id", binding.worker_id, durable.worker_id),
            ("provider_name", binding.provider_name, durable.provider_name),
            ("sandbox_id", binding.sandbox_handle.id, durable.sandbox_id),
            ("workspace_root", binding.workspace_root, durable.workspace_root),
            ("config_digest", binding.config_digest, durable.config_digest),
            ("capabilities_digest", binding.capabilities_digest, durable.capabilities_digest),
            ("source_commit", binding.source_commit, durable.source_commit),
            ("source_digest", binding.source_digest, durable.source_digest),
        )
        for field, runtime_value, durable_value in checks:
            if runtime_value != durable_value:
                mismatches.append(field)
        if mismatches:
            raise SandboxBindingError(
                "runtime/durable sandbox binding mismatch: " + ", ".join(mismatches)
            )

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
            durable = self._durable(task_id)
            if durable is not None:
                return self._resolve_durable(task_id, durable)

            if self.binding_store is not None and task_id in self.bindings:
                raise SandboxBindingError(
                    f"runtime binding exists without durable authority for task {task_id!r}; "
                    "failing closed"
                )

            if self.binding_store is None:
                existing = self.bindings.get(task_id)
                if existing is not None:
                    if existing.state == "READY":
                        return existing
                    raise SandboxLostError(
                        f"task {task_id!r} is terminal ({existing.state}); "
                        "M5 never recreates a sandbox"
                    )

            capabilities = self.provider.capabilities()
            if capabilities.provider != self.config.provider_name:
                raise SandboxProviderError(
                    "provider identity mismatch: trusted config selects "
                    f"{self.config.provider_name!r}, provider reports {capabilities.provider!r}"
                )
            require_capabilities(capabilities)
            capabilities_digest = capability_digest(capabilities)
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
                raise SandboxProviderError(
                    f"sandbox create failed; sandbox task fails closed: {created.error}"
                )
            self.create_count += 1
            handle = created.handle
            binding_id = derive_sandbox_binding_id(
                run_id=self.run_id,
                task_id=task_id,
                worker_id=worker_id,
                provider_name=self.config.provider_name,
                sandbox_handle_id=handle.id,
            )
            now = datetime.now(UTC)
            binding = ProjectSandboxBinding(
                binding_id=binding_id,
                run_id=self.run_id,
                task_id=task_id,
                worker_id=worker_id,
                provider_name=self.config.provider_name,
                sandbox_handle=handle,
                workspace_root=self.config.workspace_root,
                config_digest=self.config_digest,
                capabilities_digest=capabilities_digest,
                source_commit=source.source_commit,
                source_digest=source.bundle_digest,
                bundle_digest=source.bundle_digest,
                state="READY",
                created_at=now,
            )
            durable_record = DurableSandboxBindingRecord(
                binding_id=binding_id,
                run_id=self.run_id,
                task_id=task_id,
                worker_id=worker_id,
                provider_name=self.config.provider_name,
                sandbox_id=handle.id,
                workspace_root=self.config.workspace_root,
                config_digest=self.config_digest,
                capabilities_digest=capabilities_digest,
                source_commit=source.source_commit,
                source_digest=source.bundle_digest,
                bundle_digest=source.bundle_digest,
                state="READY",
                created_at=now,
                updated_at=now,
                revision=1,
            )
            stored = False
            try:
                report = await provisioner.provision(self.provider, handle, source)
                self.provision_commands += report.commands
                self.provision_uploads += report.uploads
                store = self.binding_store
                if store is not None:
                    store.create(durable_record)
                    stored = True
                self.bindings[task_id] = binding
            except BaseException:
                store = self.binding_store
                if stored and store is not None:
                    with contextlib.suppress(Exception):
                        store.transition(
                            binding_id,
                            expected_revision=durable_record.revision,
                            new_state="LOST",
                        )
                await self._best_effort_destroy(handle)
                raise
            finally:
                source.bundle_path.unlink(missing_ok=True)
            self._record_lifecycle(binding, "READY", occurred_at=binding.created_at)
            return binding

    def _resolve_durable(
        self, task_id: str, durable: DurableSandboxBindingRecord
    ) -> ProjectSandboxBinding:
        if durable.state == "LOST":
            raise SandboxLostError(f"sandbox for task {task_id!r} is LOST")
        if durable.state != "READY":
            raise SandboxBindingError(
                f"durable sandbox for task {task_id!r} is {durable.state}; failing closed"
            )
        runtime = self.bindings.get(task_id)
        if runtime is None:
            raise SandboxReattachmentRequired(
                f"durable READY binding exists for task {task_id!r} "
                f"(sandbox {durable.sandbox_id}); verified reattachment is not implemented"
            )
        self._validate_runtime_identity(runtime, durable)
        if runtime.state != "READY":
            raise SandboxBindingError(
                f"runtime sandbox binding for task {task_id!r} is {runtime.state}; failing closed"
            )
        return runtime

    async def _best_effort_destroy(self, handle) -> None:
        self.destroy_count += 1
        with contextlib.suppress(Exception):
            result = await self.provider.destroy(handle)
            if result.ok:
                self.destroyed_count += 1

    def has_binding(self, task_id: str) -> bool:
        if self.binding_store is not None and self._durable(task_id) is not None:
            return True
        return task_id in self.bindings

    def binding(self, task_id: str) -> ProjectSandboxBinding:
        runtime = self.bindings.get(task_id)
        durable = self._durable(task_id)
        if runtime is None:
            if durable is not None:
                raise SandboxReattachmentRequired(
                    f"durable binding exists for task {task_id!r} but no live sandbox handle "
                    "is attached; verified reattachment is not implemented"
                )
            raise SandboxBindingError(f"no sandbox binding for task {task_id!r}")
        if durable is not None:
            self._validate_runtime_identity(runtime, durable)
        return runtime

    def require_ready(self, task_id: str) -> ProjectSandboxBinding:
        binding = self.binding(task_id)
        durable = self._require_durable(task_id) if self.binding_store is not None else None
        if durable is not None:
            if durable.state == "LOST":
                raise SandboxLostError(f"sandbox for task {task_id!r} is LOST")
            if durable.state != "READY":
                raise SandboxBindingError(
                    f"durable sandbox for task {task_id!r} is {durable.state}; failing closed"
                )
            self._validate_runtime_identity(binding, durable)
        if binding.state == "LOST":
            raise SandboxLostError(f"sandbox for task {task_id!r} is LOST")
        if binding.state != "READY":
            raise SandboxBindingError(
                f"sandbox for task {task_id!r} is {binding.state}; failing closed"
            )
        return binding

    def continuity_digest(self, task_id: str) -> str:
        binding = self.require_ready(task_id)
        if self.binding_store is not None:
            return compute_sandbox_continuity_digest(self._require_durable(task_id))
        return compute_sandbox_continuity_digest(binding)

    async def mark_lost(self, task_id: str) -> None:
        runtime = self.bindings.get(task_id)
        store = self.binding_store
        durable = self._durable(task_id)
        if store is not None:
            if durable is None:
                raise SandboxBindingError(
                    f"durable sandbox binding is missing for task {task_id!r}; failing closed"
                )
            if durable.state == "READY":
                try:
                    durable = store.transition(
                        durable.binding_id,
                        expected_revision=durable.revision,
                        new_state="LOST",
                    )
                except (SandboxBindingConflict, SandboxBindingStateError):
                    current = store.get(durable.binding_id)
                    if current is None or current.state != "LOST":
                        raise
                    durable = current
            elif durable.state == "CLOSED":
                return
        if runtime is None:
            return
        updated = runtime.model_copy(update={"state": "LOST"})
        self.bindings[task_id] = updated
        self._record_lifecycle(updated, "LOST", occurred_at=None)

    async def close(self, task_id: str) -> bool:
        binding = self.binding(task_id)
        store = self.binding_store
        durable = self._durable(task_id)
        if store is not None and durable is None:
            raise SandboxBindingError(
                f"durable sandbox binding is missing for task {task_id!r}; failing closed"
            )
        if durable is not None and durable.state != "READY":
            if binding.state != durable.state:
                self.bindings[task_id] = binding.model_copy(update={"state": durable.state})
            return durable.state == "CLOSED"
        if binding.state in ("LOST", "CLOSED"):
            return binding.state == "CLOSED"

        self.destroy_count += 1
        result = await self.provider.destroy(binding.sandbox_handle)
        if result.ok:
            self.destroyed_count += 1
        new_state = "CLOSED" if result.ok else "LOST"
        if durable is not None:
            if store is None:
                raise SandboxBindingError(
                    f"durable sandbox binding for task {task_id!r} has no store; failing closed"
                )
            try:
                durable = store.transition(
                    durable.binding_id,
                    expected_revision=durable.revision,
                    new_state=new_state,
                )
            except (SandboxBindingConflict, SandboxBindingStateError):
                current = store.get(durable.binding_id)
                if current is None or current.state != new_state:
                    raise
                durable = current
        updated = binding.model_copy(update={"state": durable.state if durable else new_state})
        self.bindings[task_id] = updated
        self._record_lifecycle(updated, updated.state, occurred_at=None)
        return result.ok

    async def cancel(self, task_id: str) -> None:
        binding = self.binding(task_id)
        store = self.binding_store
        durable = self._durable(task_id)
        if store is not None and durable is None:
            raise SandboxBindingError(
                f"durable sandbox binding is missing for task {task_id!r}; failing closed"
            )
        if durable is not None and durable.state != "READY":
            return
        try:
            result = await self.provider.cancel(binding.sandbox_handle)
        except Exception as error:
            await self.mark_lost(task_id)
            raise SandboxProviderError(
                f"provider cancel failed; sandbox state is unknown: {error}"
            ) from error
        if not result.ok:
            await self.mark_lost(task_id)
            raise SandboxProviderError(
                f"provider cancel failed; sandbox state is unknown: {result.error}"
            )
        if durable is not None:
            if store is None:
                raise SandboxBindingError(
                    f"durable sandbox binding for task {task_id!r} has no store; failing closed"
                )
            try:
                durable = store.transition(
                    durable.binding_id,
                    expected_revision=durable.revision,
                    new_state="CLOSED",
                )
            except (SandboxBindingConflict, SandboxBindingStateError):
                current = store.get(durable.binding_id)
                if current is None or current.state != "CLOSED":
                    raise
                durable = current
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
        try:  # noqa: SIM105
            capabilities = self.provider.capabilities()
            recorder.record_sandbox_lifecycle(
                binding_id=binding.binding_id,
                task_id=binding.task_id,
                worker_id=binding.worker_id,
                provider_name=binding.provider_name,
                state=state,
                source_commit=binding.source_commit,
                sandbox_id=binding.sandbox_handle.id,
                capabilities_digest=capability_digest(capabilities),
                capabilities=capability_summary(capabilities),
                network=self.config.network,
                occurred_at=occurred_at,
                correlation=TrajectoryCorrelation(
                    sandbox_binding_id=binding.binding_id,
                    worker_id=binding.worker_id,
                ),
            )
        except Exception:  # noqa: BLE001 - evidence is optional
            pass


def require_sandbox_capabilities(provider: SandboxProvider) -> None:
    require_capabilities(provider.capabilities())
