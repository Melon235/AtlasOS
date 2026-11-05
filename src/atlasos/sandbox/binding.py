"""One task -> at most one durable Atlas sandbox binding, with no host fallback.

The SQLite durable store owns the M5 task/sandbox relationship and lifecycle state. The in-process
``bindings`` dictionary is only a live-handle cache; it can never override durable truth.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast

from anycode.identity.context import ExecutionContext
from anycode.sandbox import SandboxHandle, SandboxProvider

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
from atlasos.sandbox.ownership import (
    SandboxOwnershipError,
    SandboxOwnershipUnavailable,
    WorkloadOwnershipLock,
)
from atlasos.sandbox.provider import (
    capability_digest,
    capability_summary,
    require_capabilities,
)
from atlasos.sandbox.provision import GitProjectProvisioner
from atlasos.sandbox.reattachment import (
    REMOTE_RUNNING,
    REMOTE_TERMINAL,
    SandboxConfigMismatch,
    SandboxContinuityMismatch,
    SandboxCreatedAtMismatch,
    SandboxLegacyContinuityUnavailable,
    SandboxMetadataMismatch,
    SandboxReattachmentProvider,
    SandboxReattachmentUnsupported,
    SandboxRemoteNotAttachable,
    SandboxRemoteNotFound,
    SandboxRemoteTerminal,
    normalize_provider_created_at,
    normalize_remote_state,
)
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
        ownership_root: str | Path | None = None,
        trajectory_recorder: TrajectoryRecorder | None = None,
    ) -> None:
        config.validate_v1()
        self.provider = provider
        self.config = config
        self.run_id = run_id
        self.binding_store = binding_store
        self.trajectory_recorder = trajectory_recorder
        self.ownership_root: Path | None = None
        if binding_store is not None:
            if ownership_root is None:
                store_path = getattr(binding_store, "path", None)
                if store_path is None:
                    raise SandboxOwnershipUnavailable(
                        "durable sandbox store has no ownership root; refusing to run unlocked"
                    )
                ownership_root = Path(store_path).parent
            self.ownership_root = Path(ownership_root)
        self._ownership: dict[str, WorkloadOwnershipLock] = {}
        # Runtime-local live-handle cache, never the durable M5 authority.
        self.bindings: dict[str, ProjectSandboxBinding] = {}
        # Manager-wide per-workload gate. All lifecycle, ownership-release, and provider
        # operations for one workload pass through this gate.
        self._task_locks: dict[str, asyncio.Lock] = {}
        # The create/recover critical section remains separately serialized; it is never
        # entered before the manager-wide gate.
        self._inner_task_locks: dict[str, asyncio.Lock] = {}
        self._uncertain_operations: set[str] = set()
        self.config_digest = compute_sandbox_config_digest(config)
        self.create_count = 0
        self.destroy_count = 0
        self.destroyed_count = 0
        self.provision_commands = 0
        self.provision_uploads = 0

    def _owned_locked(self, task_id: str) -> bool:
        """Return whether this caller newly acquired the OS lock under the gate."""

        if self.ownership_root is None:
            return False
        existing = self._ownership.get(task_id)
        if existing is not None:
            existing.assert_held()
            return False
        lock = WorkloadOwnershipLock(
            self.ownership_root,
            run_id=self.run_id,
            task_id=task_id,
        )
        lock.acquire()
        self._ownership[task_id] = lock
        return True

    def _has_ownership(self, task_id: str) -> bool:
        lock = self._ownership.get(task_id)
        return lock is not None and lock.held

    def require_ownership(self, task_id: str) -> None:
        if self.ownership_root is None:
            return
        lock = self._ownership.get(task_id)
        if lock is None:
            raise SandboxOwnershipError(
                f"local ownership for workload {self.run_id!r}/{task_id!r} is not held"
            )
        lock.assert_held()

    def _release_ownership_locked(self, task_id: str) -> None:
        lock = self._ownership.pop(task_id, None)
        if lock is not None:
            lock.release()

    async def release_ownership(self, task_id: str) -> None:
        """Invalidate local runtime authority, detach locally, then release the OS lock."""

        async with self._operation_gate(task_id):
            if task_id in self._uncertain_operations:
                raise SandboxOwnershipError(
                    f"cannot release ownership for workload {self.run_id!r}/{task_id!r}: "
                    "a provider operation outcome is uncertain; resolve with close or mark_lost"
                )
            runtime = self.bindings.get(task_id)
            if runtime is not None:
                detach = getattr(self.provider, "detach_local", None)
                if not callable(detach):
                    self._uncertain_operations.add(task_id)
                    self.bindings.pop(task_id, None)
                    raise SandboxOwnershipError(
                        "provider has no safe local detach seam; ownership retained and "
                        "runtime authority invalidated"
                    )
                detach_call = cast("Callable[[str], Awaitable[None]]", detach)
                try:
                    await detach_call(runtime.sandbox_handle.id)
                except asyncio.CancelledError:
                    self._uncertain_operations.add(task_id)
                    self.bindings.pop(task_id, None)
                    raise
                except Exception as error:
                    self._uncertain_operations.add(task_id)
                    self.bindings.pop(task_id, None)
                    raise SandboxOwnershipError(
                        f"local detach failed for workload {self.run_id!r}/{task_id!r}; "
                        "ownership retained and runtime authority invalidated"
                    ) from error
                # The durable relationship stays READY, but the old runtime handle is no longer
                # authority. Recovery must reacquire ownership and perform Stage B verification.
                self.bindings.pop(task_id, None)
            self._release_ownership_locked(task_id)

    def _terminal_state_confirmed(self, task_id: str) -> bool:
        """Whether a lifecycle failure left durable/runtime authority definitively terminal."""

        try:
            if self.binding_store is not None:
                durable = self._durable(task_id)
                return durable is not None and durable.state in ("LOST", "CLOSED")
            runtime = self.bindings.get(task_id)
            return runtime is not None and runtime.state in ("LOST", "CLOSED")
        except Exception:  # noqa: BLE001 - an unreadable store is not proof of terminal state
            return False

    def _after_lifecycle_failure(self, task_id: str) -> None:
        """Fail closed on an unconfirmed lifecycle outcome; never hand off a READY owner."""

        if self._terminal_state_confirmed(task_id):
            self._resolve_uncertain(task_id)
            self._release_ownership_locked(task_id)
            return
        self.mark_provider_uncertain(task_id)

    @contextlib.asynccontextmanager
    async def _operation_gate(self, task_id: str):
        gate = self._task_locks.setdefault(task_id, asyncio.Lock())
        async with gate:
            yield

    @contextlib.asynccontextmanager
    async def operation_gate(self, task_id: str):
        """Gate a provider operation and re-confirm ownership inside the gate."""

        async with self._operation_gate(task_id):
            self.require_ownership(task_id)
            if task_id in self._uncertain_operations:
                raise SandboxOwnershipError(
                    f"provider operation outcome for workload {self.run_id!r}/{task_id!r} "
                    "is uncertain; refusing another provider operation"
                )
            yield

    def mark_provider_uncertain(self, task_id: str) -> None:
        """Record that a canceled provider call left the remote side effect unconfirmed."""

        self._uncertain_operations.add(task_id)

    def _resolve_uncertain(self, task_id: str) -> None:
        self._uncertain_operations.discard(task_id)

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

    def _reattachment_provider(self) -> SandboxReattachmentProvider:
        required = (
            "continuity_identity_digest",
            "inspect_existing",
            "attach_existing",
            "detach_local",
            "verify_network_none",
        )
        if not all(callable(getattr(self.provider, name, None)) for name in required):
            raise SandboxReattachmentUnsupported(
                f"provider {self.provider.capabilities().provider!r} does not expose the "
                "verified reattachment seam"
            )
        return cast(SandboxReattachmentProvider, self.provider)

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
        async with self._operation_gate(task_id):
            acquired_here = self._owned_locked(task_id)
            if task_id in self._uncertain_operations:
                if acquired_here:
                    self._release_ownership_locked(task_id)
                raise SandboxOwnershipError(
                    f"provider operation outcome for workload {self.run_id!r}/{task_id!r} "
                    "is uncertain; refusing another create"
                )
            try:
                return await self._ensure_binding_impl(
                    task_id=task_id,
                    source_repo=source_repo,
                    context=context,
                    worker_id=worker_id,
                )
            except asyncio.CancelledError:
                # A canceled provider operation is not proof that the remote side effect
                # stopped. Keep ownership and poison further provider operations.
                if self.ownership_root is not None:
                    self.mark_provider_uncertain(task_id)
                raise
            except BaseException:
                if acquired_here:
                    self._release_ownership_locked(task_id)
                raise

    async def _ensure_binding_impl(
        self,
        *,
        task_id: str,
        source_repo: str,
        context: ExecutionContext,
        worker_id: str | None = None,
    ) -> ProjectSandboxBinding:
        lock = self._inner_task_locks.setdefault(task_id, asyncio.Lock())
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
            reattachment_provider = (
                self._reattachment_provider() if self.binding_store is not None else None
            )
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
            stored = False
            try:
                provider_control_plane_digest: str | None = None
                provider_created_at: datetime | None = None
                provider_metadata_digest: str | None = None
                if reattachment_provider is not None:
                    observation = await reattachment_provider.inspect_existing(handle.id)
                    if observation.sandbox_id != handle.id:
                        raise SandboxContinuityMismatch(
                            "provider inspection returned a different sandbox identity"
                        )
                    if normalize_remote_state(observation.state) != REMOTE_RUNNING:
                        raise SandboxRemoteNotAttachable(
                            f"newly created sandbox {handle.id!r} is provider state "
                            f"{observation.state!r}, not {REMOTE_RUNNING}"
                        )
                    provider_control_plane_digest = (
                        reattachment_provider.continuity_identity_digest()
                    )
                    provider_created_at = normalize_provider_created_at(observation.created_at)
                    provider_metadata_digest = observation.metadata_digest
                    await reattachment_provider.verify_network_none(handle.id)

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
                    provider_control_plane_digest=provider_control_plane_digest,
                    provider_created_at=provider_created_at,
                    provider_metadata_digest=provider_metadata_digest,
                )
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

    async def reattach_verified(
        self,
        *,
        task_id: str,
        context: ExecutionContext,
    ) -> ProjectSandboxBinding:
        """Verify a durable READY binding against the provider, then attach exactly once.

        This is explicit. ``ensure_binding`` never calls it automatically.
        """

        async with self._operation_gate(task_id):
            acquired_here = self._owned_locked(task_id)
            if task_id in self._uncertain_operations:
                if acquired_here:
                    self._release_ownership_locked(task_id)
                raise SandboxOwnershipError(
                    f"provider operation outcome for workload {self.run_id!r}/{task_id!r} "
                    "is uncertain; refusing reattachment"
                )
            try:
                return await self._reattach_verified_impl(
                    task_id=task_id,
                    context=context,
                )
            except asyncio.CancelledError:
                if self.ownership_root is not None:
                    self.mark_provider_uncertain(task_id)
                raise
            except BaseException:
                if acquired_here:
                    self._release_ownership_locked(task_id)
                raise

    async def _reattach_verified_impl(
        self,
        *,
        task_id: str,
        context: ExecutionContext,
    ) -> ProjectSandboxBinding:
        lock = self._inner_task_locks.setdefault(task_id, asyncio.Lock())
        async with lock:
            store = self.binding_store
            if store is None:
                raise SandboxReattachmentUnsupported(
                    "verified reattachment requires a durable sandbox binding store"
                )
            durable = store.get_for_task(self.run_id, task_id)
            if durable is None:
                raise SandboxBindingError(f"no durable sandbox binding for task {task_id!r}")
            if durable.state == "LOST":
                raise SandboxLostError(f"sandbox for task {task_id!r} is LOST")
            if durable.state != "READY":
                raise SandboxBindingError(
                    f"durable sandbox for task {task_id!r} is {durable.state}; failing closed"
                )

            existing = self.bindings.get(task_id)
            if existing is not None:
                self._validate_runtime_identity(existing, durable)
                if existing.state != "READY":
                    raise SandboxBindingError(
                        f"runtime sandbox for task {task_id!r} is {existing.state}"
                    )
                return existing

            control_plane_digest = durable.provider_control_plane_digest
            provider_created_at = durable.provider_created_at
            metadata_digest = durable.provider_metadata_digest
            if (
                control_plane_digest is None
                or provider_created_at is None
                or metadata_digest is None
            ):
                raise SandboxLegacyContinuityUnavailable(
                    f"durable binding for task {task_id!r} lacks provider continuity evidence; "
                    "legacy bindings cannot be reattached"
                )

            provider = self._reattachment_provider()
            if self.config_digest != durable.config_digest:
                raise SandboxConfigMismatch(
                    f"current trusted sandbox config does not match durable binding "
                    f"for task {task_id!r}"
                )
            capabilities = self.provider.capabilities()
            if capabilities.provider != durable.provider_name:
                raise SandboxContinuityMismatch(f"provider identity changed for task {task_id!r}")
            if capability_digest(capabilities) != durable.capabilities_digest:
                raise SandboxContinuityMismatch(
                    f"provider capabilities changed for task {task_id!r}"
                )
            if provider.continuity_identity_digest() != control_plane_digest:
                raise SandboxContinuityMismatch(
                    f"OpenSandbox control-plane identity changed for task {task_id!r}; "
                    "refusing to inspect a same-ID resource on another control plane"
                )

            try:
                observation = await provider.inspect_existing(durable.sandbox_id)
            except SandboxRemoteNotFound as error:
                await self._mark_durable_lost(durable)
                raise SandboxLostError(
                    f"sandbox for task {task_id!r} is missing from the trusted provider; "
                    "durable state is LOST"
                ) from error
            except SandboxRemoteTerminal as error:
                await self._mark_durable_lost(durable)
                raise SandboxLostError(
                    f"sandbox for task {task_id!r} is terminal at the trusted provider; "
                    "durable state is LOST"
                ) from error

            observed_state = normalize_remote_state(observation.state)
            if observed_state in REMOTE_TERMINAL:
                await self._mark_durable_lost(durable)
                raise SandboxLostError(
                    f"sandbox for task {task_id!r} is {observed_state} at the trusted provider; "
                    "durable state is LOST"
                )
            if observed_state != REMOTE_RUNNING:
                raise SandboxRemoteNotAttachable(
                    f"sandbox for task {task_id!r} is provider state {observed_state!r}; "
                    "refusing to attach"
                )
            if observation.sandbox_id != durable.sandbox_id:
                raise SandboxContinuityMismatch(
                    f"provider inspection returned sandbox {observation.sandbox_id!r}, "
                    f"expected {durable.sandbox_id!r}"
                )
            if normalize_provider_created_at(
                observation.created_at
            ) != normalize_provider_created_at(provider_created_at):
                raise SandboxCreatedAtMismatch(
                    f"provider created_at mismatch for sandbox {durable.sandbox_id!r}"
                )
            if observation.metadata_digest != metadata_digest:
                raise SandboxMetadataMismatch(
                    f"provider metadata digest mismatch for sandbox {durable.sandbox_id!r}"
                )
            if observation.metadata.get("run_id") != durable.run_id:
                raise SandboxMetadataMismatch(
                    f"provider metadata run_id mismatch for sandbox {durable.sandbox_id!r}"
                )
            if observation.metadata.get("task_id") != durable.task_id:
                raise SandboxMetadataMismatch(
                    f"provider metadata task_id mismatch for sandbox {durable.sandbox_id!r}"
                )
            if (
                durable.worker_id is not None
                and observation.metadata.get("worker_id") != durable.worker_id
            ):
                raise SandboxMetadataMismatch(
                    f"provider metadata worker_id mismatch for sandbox {durable.sandbox_id!r}"
                )
            if observation.metadata.get("provider") != durable.provider_name:
                raise SandboxMetadataMismatch(
                    f"provider metadata provider mismatch for sandbox {durable.sandbox_id!r}"
                )

            await provider.attach_existing(durable.sandbox_id)
            try:
                await provider.verify_network_none(durable.sandbox_id)
                binding = self._binding_from_durable(durable, context)
                self.bindings[task_id] = binding
            except BaseException:
                with contextlib.suppress(Exception):
                    await provider.detach_local(durable.sandbox_id)
                raise
            return binding

    async def _mark_durable_lost(
        self, durable: DurableSandboxBindingRecord
    ) -> DurableSandboxBindingRecord:
        store = self.binding_store
        if store is None:
            raise SandboxBindingError(f"durable store is unavailable for task {durable.task_id!r}")
        try:
            updated = store.transition(
                durable.binding_id,
                expected_revision=durable.revision,
                new_state="LOST",
            )
        except (SandboxBindingConflict, SandboxBindingStateError):
            current = store.get(durable.binding_id)
            if current is None or current.state != "LOST":
                raise
            updated = current
        runtime = self.bindings.get(durable.task_id)
        if runtime is not None:
            runtime = runtime.model_copy(update={"state": "LOST"})
            self.bindings[durable.task_id] = runtime
            self._record_lifecycle(runtime, "LOST", occurred_at=None)
        return updated

    def _binding_from_durable(
        self,
        durable: DurableSandboxBindingRecord,
        context: ExecutionContext,
    ) -> ProjectSandboxBinding:
        capabilities = self.provider.capabilities()
        handle = SandboxHandle(
            id=durable.sandbox_id,
            provider=durable.provider_name,
            run_id=durable.run_id,
            task_id=durable.task_id,
            correlation_id=f"{durable.run_id}:{durable.task_id}",
            context=context,
            capabilities=capabilities,
            metadata={"runtime": "reattached"},
        )
        return ProjectSandboxBinding(
            binding_id=durable.binding_id,
            run_id=durable.run_id,
            task_id=durable.task_id,
            worker_id=durable.worker_id,
            provider_name=durable.provider_name,
            sandbox_handle=handle,
            workspace_root=durable.workspace_root,
            config_digest=durable.config_digest,
            capabilities_digest=durable.capabilities_digest,
            source_commit=durable.source_commit,
            source_digest=durable.source_digest,
            bundle_digest=durable.bundle_digest,
            state="READY",
            created_at=durable.created_at,
        )

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
                f"(sandbox {durable.sandbox_id}); verified reattachment is required"
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
        # Durable lifecycle truth must be classified before runtime-handle availability.
        # A terminal durable binding is never eligible for reattachment.
        durable = self._durable(task_id)
        runtime = self.bindings.get(task_id)
        if durable is not None:
            if durable.state == "LOST":
                raise SandboxLostError(f"sandbox for task {task_id!r} is LOST")
            if durable.state != "READY":
                raise SandboxBindingError(
                    f"durable sandbox for task {task_id!r} is {durable.state}; failing closed"
                )
            if runtime is None:
                raise SandboxReattachmentRequired(
                    f"durable READY binding exists for task {task_id!r} but no live sandbox "
                    "handle is attached; verified reattachment is required"
                )
            self._validate_runtime_identity(runtime, durable)
            return runtime
        if runtime is None:
            raise SandboxBindingError(f"no sandbox binding for task {task_id!r}")
        return runtime

    def require_ready(self, task_id: str) -> ProjectSandboxBinding:
        binding = self.binding(task_id)
        self.require_ownership(task_id)
        if task_id in self._uncertain_operations:
            raise SandboxOwnershipError(
                f"provider operation outcome for workload {self.run_id!r}/{task_id!r} "
                "is uncertain; failing closed"
            )
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
        async with self._operation_gate(task_id):
            if self.ownership_root is not None and not self._has_ownership(task_id):
                return
            try:
                await self._mark_lost_impl(task_id)
            except asyncio.CancelledError:
                self.mark_provider_uncertain(task_id)
                raise
            except BaseException:
                self._after_lifecycle_failure(task_id)
                raise
            self._resolve_uncertain(task_id)
            self._release_ownership_locked(task_id)

    async def _mark_lost_impl(self, task_id: str) -> None:
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
        async with self._operation_gate(task_id):
            self.require_ownership(task_id)
            try:
                result = await self._close_impl(task_id)
            except asyncio.CancelledError:
                if self.ownership_root is not None:
                    self.mark_provider_uncertain(task_id)
                raise
            except BaseException:
                self._after_lifecycle_failure(task_id)
                raise
            self._resolve_uncertain(task_id)
            self._release_ownership_locked(task_id)
            return result

    async def _close_impl(self, task_id: str) -> bool:
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
        async with self._operation_gate(task_id):
            self.require_ownership(task_id)
            try:
                await self._cancel_impl(task_id)
            except asyncio.CancelledError:
                if self.ownership_root is not None:
                    self.mark_provider_uncertain(task_id)
                raise
            except BaseException:
                self._after_lifecycle_failure(task_id)
                raise
            self._resolve_uncertain(task_id)
            self._release_ownership_locked(task_id)

    async def _cancel_impl(self, task_id: str) -> None:
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
            await self._mark_lost_impl(task_id)
            raise SandboxProviderError(
                f"provider cancel failed; sandbox state is unknown: {error}"
            ) from error
        if not result.ok:
            await self._mark_lost_impl(task_id)
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
