"""Trusted recovery composition for a durable M6 sandbox suspension.

This module is a thin composition helper, not a second runtime. It reconstructs the exact
trusted Worker and AtlasToolRuntime from durable state, keeps the existing M3 action gate and
M4 SQLite execution records, and delegates the M6 continuation to the existing
``resume_execution_suspension`` path.

Stage D scope: an M6 suspension that was created before tool execution and whose M5 sandbox is
still the durable READY S1. Recovery never creates a replacement sandbox.
"""

from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from anycode.runstore.store import FilesystemRunStore
from anycode.sandbox import SandboxProvider
from anycode.tools.idempotency import SQLiteToolIdempotencyStore
from anycode.types import ToolSecurityPolicy, ToolUseContext

from atlasos.actions import ActionCatalog, ActionPolicy
from atlasos.actions.binding import canonical_digest as action_digest
from atlasos.approvals.resume import ResumeOutcome, resume_execution_suspension
from atlasos.approvals.store import SQLiteApprovalStore
from atlasos.config import AtlasConfig
from atlasos.ledger import SQLiteActionLedgerStore
from atlasos.sandbox import (
    OpenSandboxOptions,
    ProjectSandboxManager,
    SandboxProjectConfig,
    SandboxWorkspaceTools,
    SQLiteSandboxBindingStore,
    build_sandbox_provider,
    is_real_sandbox_provider,
    require_capabilities,
)
from atlasos.suspension.models import (
    ExecutionSuspension,
    trusted_identity_digest,
)
from atlasos.suspension.store import SQLiteSuspensionStore
from atlasos.tools import AtlasToolRuntime
from atlasos.tools.catalog import CORE_TOOL_DISPATCH
from atlasos.trajectory.recorder import TrajectoryRecorder
from atlasos.workers import ScopedWorkerFactory, WorkerCatalog, WorkerInstance, WorkerStore


class TrustedRecoveryError(RuntimeError):
    """Trusted reconstruction or identity validation failed closed."""


@dataclass(frozen=True)
class RecoveryCleanupResult:
    """Observable outcome of session resource cleanup.

    ``ownership_unresolved`` means the M5 OS lock is still held by this process because the
    remote effect is uncertain or local detach failed. The caller must not assume a safe handoff.
    """

    ownership_released: bool
    ownership_unresolved: bool
    runtime_closed: bool
    idempotency_closed: bool
    reason: str | None = None
    error: BaseException | None = field(default=None, repr=False, compare=False)

    def describe(self) -> str:
        """Stable one-line projection suitable for an exception note or operator log."""

        details = [
            f"ownership_released={self.ownership_released}",
            f"ownership_unresolved={self.ownership_unresolved}",
            f"runtime_closed={self.runtime_closed}",
            f"idempotency_closed={self.idempotency_closed}",
        ]
        if self.reason:
            details.append(f"reason={self.reason}")
        if self.error is not None:
            details.append(f"error={type(self.error).__name__}: {self.error}")
        return "cleanup: " + "; ".join(details)


class TrustedRecoveryOwnershipUnresolved(TrustedRecoveryError):
    """Recovery produced an outcome while retaining ownership for manual safety review."""

    def __init__(
        self,
        outcome: ResumeOutcome | None,
        cleanup: RecoveryCleanupResult,
    ) -> None:
        self.outcome = outcome
        self.cleanup = cleanup
        reason = cleanup.reason or "recovery ownership remains unresolved"
        super().__init__(reason)


class TrustedRecoveryDisposalError(TrustedRecoveryError):
    """An explicit sandbox disposal request did not complete safely.

    ``cleanup`` is always populated by the public wrapper. ``disposed`` is False because this
    exception is only raised when S1 was not confirmed destroyed.
    """

    def __init__(
        self,
        outcome: ResumeOutcome | None,
        cleanup: RecoveryCleanupResult | None,
        reason: str,
    ) -> None:
        self.outcome = outcome
        self.cleanup = cleanup
        self.disposed = False
        super().__init__(reason)


def _annotate_cleanup(error: BaseException, cleanup: RecoveryCleanupResult) -> None:
    """Expose cleanup beside an original failure without replacing that failure."""

    with contextlib.suppress(Exception):
        error.add_note(f"Stage D {cleanup.describe()}")
    with contextlib.suppress(Exception):
        error.atlas_cleanup = cleanup  # type: ignore[attr-defined]


@dataclass(frozen=True)
class TrustedRecoveryConfig:
    """Deployment-controlled inputs for reconstructing one suspended runtime.

    ``provider_override`` exists only for deterministic tests and embedding. Production callers
    leave it as ``None`` so the provider is built from the trusted Atlas configuration.
    """

    state_root: Path
    run_id: str
    task_id: str
    worker_id: str
    profile_id: str
    suspension_id: str
    config: AtlasConfig
    policy_path: Path
    expected_principal: str
    expected_tenant_scope: str
    provider_override: SandboxProvider | None = field(default=None, repr=False, compare=False)
    runtime_instance_id: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "run_id",
            "task_id",
            "worker_id",
            "profile_id",
            "suspension_id",
            "expected_principal",
            "expected_tenant_scope",
        ):
            if not str(getattr(self, name)).strip():
                raise TrustedRecoveryError(f"trusted recovery {name} must not be empty")
        try:
            root = Path(self.state_root).expanduser().resolve(strict=True)
            policy = Path(self.policy_path).expanduser().resolve(strict=True)
        except OSError as error:
            raise TrustedRecoveryError(f"trusted recovery state is missing: {error}") from error
        if not root.is_dir():
            raise TrustedRecoveryError(f"trusted state_root is not a directory: {root}")
        if not policy.is_file():
            raise TrustedRecoveryError(f"trusted policy path is not a file: {policy}")
        object.__setattr__(self, "state_root", root)
        object.__setattr__(self, "policy_path", policy)
        if self.config.tools.mode != "progressive":
            raise TrustedRecoveryError("trusted recovery requires tools.mode='progressive'")
        if not self.config.sandbox.enabled:
            raise TrustedRecoveryError("trusted recovery requires an enabled Atlas sandbox")
        if self.provider_override is None and self.config.sandbox.provider == "fake":
            raise TrustedRecoveryError(
                "a fake sandbox provider cannot be selected by a production recovery configuration"
            )

    @property
    def run_dir(self) -> Path:
        return self.state_root / "runs" / self.run_id

    @property
    def worker_store_path(self) -> Path:
        return self.state_root / "workers.json"

    @property
    def suspension_store_path(self) -> Path:
        return self.state_root / "suspension.db"

    @property
    def approval_store_path(self) -> Path:
        return self.state_root / "approvals.db"

    @property
    def ledger_path(self) -> Path:
        return self.state_root / "ledger.db"

    @property
    def idempotency_path(self) -> Path:
        return self.state_root / "idempotency.db"

    @property
    def sandbox_binding_path(self) -> Path:
        return self.state_root / "sandbox_bindings.db"

    @property
    def effective_runtime_instance_id(self) -> str:
        return self.runtime_instance_id or f"stage-d-recovery-{self.run_id}-{os.getpid()}"


def project_config_for(config: TrustedRecoveryConfig) -> SandboxProjectConfig:
    return SandboxProjectConfig(
        provider_name=config.config.sandbox.provider,
        image=config.config.sandbox.image,
        snapshot=config.config.sandbox.snapshot,
        workspace_root=config.config.sandbox.workspace_root,
        max_bundle_bytes=config.config.sandbox.max_bundle_bytes,
        network=config.config.sandbox.network,
    )


def build_recovery_provider(config: TrustedRecoveryConfig) -> SandboxProvider:
    if config.provider_override is not None:
        provider = config.provider_override
        expected = config.config.sandbox.provider
        actual = provider.capabilities().provider
        if actual != expected:
            raise TrustedRecoveryError(
                f"provider identity mismatch: trusted config requires {expected!r}, "
                f"provider reports {actual!r}"
            )
        return provider
    sandbox = config.config.sandbox
    provider = build_sandbox_provider(
        sandbox.provider,
        opensandbox_options=OpenSandboxOptions(
            endpoint=sandbox.endpoint,
            protocol=sandbox.protocol,
            timeout_minutes=sandbox.timeout_minutes,
            ready_timeout_seconds=sandbox.ready_timeout_seconds,
            cpu=sandbox.cpu,
            memory=sandbox.memory,
            use_server_proxy=sandbox.use_server_proxy,
        ),
    )
    if not is_real_sandbox_provider(provider, expected_name=sandbox.provider):
        raise TrustedRecoveryError("trusted recovery requires a real Atlas sandbox provider")
    return provider


@dataclass
class TrustedRecoverySession:
    """One validated reconstruction of Worker + M3/M4/M5/M6 runtime state."""

    config: TrustedRecoveryConfig
    suspension: ExecutionSuspension
    worker: WorkerInstance
    provider: SandboxProvider
    manager: ProjectSandboxManager
    sandbox_tools: SandboxWorkspaceTools
    runtime: AtlasToolRuntime
    suspension_store: SQLiteSuspensionStore
    approval_store: SQLiteApprovalStore
    ledger: SQLiteActionLedgerStore
    idempotency: SQLiteToolIdempotencyStore
    context: ToolUseContext
    policy: ActionPolicy
    trajectory_recorder: TrajectoryRecorder | None = None
    reattached: bool = False
    network_verified: bool = False
    sandbox_id: str | None = None
    binding_id: str | None = None
    continuity_digest: str | None = None
    _last_outcome: ResumeOutcome | None = field(default=None, init=False, repr=False)
    _cleanup_result: RecoveryCleanupResult | None = field(default=None, init=False, repr=False)

    @classmethod
    async def prepare(cls, config: TrustedRecoveryConfig) -> TrustedRecoverySession:
        _require_recovery_files(config)
        suspension_store: SQLiteSuspensionStore | None = None
        idempotency: SQLiteToolIdempotencyStore | None = None
        try:
            suspension_store = SQLiteSuspensionStore(config.suspension_store_path)
            approval_store = SQLiteApprovalStore(config.approval_store_path)
            ledger = SQLiteActionLedgerStore(config.ledger_path)
            idempotency = SQLiteToolIdempotencyStore(config.idempotency_path)
            await idempotency.setup()
            suspension = suspension_store.get(config.suspension_id)
            if suspension is None:
                raise TrustedRecoveryError(
                    f"no durable suspension {config.suspension_id!r} in trusted state"
                )
            _validate_suspension_identity(config, suspension)

            worker_store = WorkerStore(config.worker_store_path)
            worker_factory = ScopedWorkerFactory(
                model=config.config.provider.model,
                provider=config.config.provider.name,
                store=worker_store,
                catalog=WorkerCatalog(),
            )
            try:
                worker = worker_factory.resume(config.worker_id)
            except Exception as error:
                raise TrustedRecoveryError(
                    f"trusted worker reconstruction failed for {config.worker_id!r}: {error}"
                ) from error
            _validate_worker_identity(config, suspension, worker)

            try:
                policy = ActionPolicy.from_file(config.policy_path)
            except Exception as error:
                raise TrustedRecoveryError(
                    f"trusted action policy is unavailable: {error}"
                ) from error

            provider = build_recovery_provider(config)
            require_capabilities(provider.capabilities())
            project_config = project_config_for(config)
            project_config.validate_v1()
            recorder: TrajectoryRecorder | None = None
            if config.config.trajectory.enabled:
                recorder = TrajectoryRecorder(
                    FilesystemRunStore(config.run_dir / config.config.trajectory.store_root),
                    config.run_id,
                    agent_name="atlas-stage-d-recovery",
                    model=config.config.provider.model,
                )
            manager = ProjectSandboxManager(
                provider,
                project_config,
                run_id=config.run_id,
                binding_store=SQLiteSandboxBindingStore(config.sandbox_binding_path),
                ownership_root=config.state_root,
                trajectory_recorder=recorder,
            )
            sandbox_tools = SandboxWorkspaceTools(manager, config.task_id, provider)
            runtime = AtlasToolRuntime(
                config.run_id,
                state_path=config.run_dir / "tool_state.json",
                conversation_path=config.run_dir / "tool_conversations.json",
                external_tools=(),
                action_policy=policy,
                action_catalog=ActionCatalog(),
                action_ledger=ledger,
                idempotency_store=idempotency,
                suspension_store=suspension_store,
                approval_store=approval_store,
                sandbox_tools=sandbox_tools,
                trajectory_recorder=recorder,
                runtime_instance_id=config.effective_runtime_instance_id,
            )
            context = worker.tool_context(
                security_policy=ToolSecurityPolicy(
                    workspace_root=project_config.workspace_root,
                )
            )
            session = cls(
                config=config,
                suspension=suspension,
                worker=worker,
                provider=provider,
                manager=manager,
                sandbox_tools=sandbox_tools,
                runtime=runtime,
                suspension_store=suspension_store,
                approval_store=approval_store,
                ledger=ledger,
                idempotency=idempotency,
                context=context,
                policy=policy,
                trajectory_recorder=recorder,
            )
            session._validate_tool_bindings()
            return session
        except BaseException:
            if idempotency is not None:
                with contextlib.suppress(Exception):
                    await idempotency.teardown()
            raise

    def _validate_tool_bindings(self) -> None:
        for invocation in self.suspension.pending_invocations:
            target = invocation.effective_tool_id
            if not self.runtime.registry.has(target):
                raise TrustedRecoveryError(
                    f"pending tool {target!r} is not available in the trusted tool runtime"
                )
            if invocation.logical_tool_id != CORE_TOOL_DISPATCH:
                if target not in self.worker.profile.base_tools:
                    raise TrustedRecoveryError(
                        f"pending tool {target!r} is not exposed by worker profile "
                        f"{self.worker.profile.profile_id!r}"
                    )
                current = self.runtime.tool_binding_digest(target)
            else:
                if CORE_TOOL_DISPATCH not in self.worker.profile.base_tools:
                    raise TrustedRecoveryError("worker profile does not expose dispatch_action")
                if invocation.dispatch_target is None:
                    raise TrustedRecoveryError("dispatch suspension lacks an exact target")
                if not self.runtime.registry.has(invocation.dispatch_target):
                    raise TrustedRecoveryError(
                        f"dispatch target {invocation.dispatch_target!r} is unavailable"
                    )
                current = action_digest(
                    {
                        "outer": self.runtime.tool_binding_digest(CORE_TOOL_DISPATCH),
                        "target": self.runtime.tool_binding_digest(invocation.dispatch_target),
                    }
                )
            if current != invocation.tool_binding_digest:
                raise TrustedRecoveryError(
                    f"tool binding changed for pending call {invocation.call_id!r}"
                )

    async def resume(
        self,
        *,
        owner: str | None = None,
        lease_seconds: float = 60.0,
        commit_continuation: bool = True,
        fault_hook: Any | None = None,
    ) -> ResumeOutcome:
        """Resume through the existing M6 path, reattaching S1 only when needed."""

        outcome = await self._resume_impl(
            owner=owner,
            lease_seconds=lease_seconds,
            commit_continuation=commit_continuation,
            fault_hook=fault_hook,
        )
        self._last_outcome = outcome
        return outcome

    async def _resume_impl(
        self,
        *,
        owner: str | None = None,
        lease_seconds: float = 60.0,
        commit_continuation: bool = True,
        fault_hook: Any | None = None,
    ) -> ResumeOutcome:
        current = self.suspension_store.get(self.config.suspension_id)
        if current is None:
            raise TrustedRecoveryError("suspension disappeared during recovery")
        run_state = self.suspension_store.run_state(current.run_id)
        if run_state in ("CANCELLED", "COMPLETED", "FAILED"):
            return ResumeOutcome(
                status="terminal_run",
                suspension_id=current.suspension_id,
                reason=f"run is terminal: {run_state}",
                suspension=current,
            )
        if current.state == "CLEARED":
            return ResumeOutcome(
                status="cleared",
                suspension_id=current.suspension_id,
                suspension=current,
            )
        if current.state == "STALE":
            return ResumeOutcome(
                status="stale",
                suspension_id=current.suspension_id,
                reason=current.last_error or "suspension is stale",
                suspension=current,
            )
        if current.state == "SETTLED" or (
            current.state == "SETTLING" and current.settlement is not None
        ):
            return await self._invoke_m6(
                current,
                owner=owner,
                lease_seconds=lease_seconds,
                commit_continuation=commit_continuation,
                fault_hook=fault_hook,
            )

        if current.state == "ACTIVE":
            pending = self.approval_store.list_pending(suspension_id=current.suspension_id)
            if pending:
                return ResumeOutcome(
                    status="waiting",
                    suspension_id=current.suspension_id,
                    reason="one or more required human decisions are still pending",
                    suspension=current,
                )

        if current.sandbox_required:
            await self._reattach_for_execution(current)

        return await self._invoke_m6(
            current,
            owner=owner,
            lease_seconds=lease_seconds,
            commit_continuation=commit_continuation,
            fault_hook=fault_hook,
        )

    async def _reattach_for_execution(self, suspension: ExecutionSuspension) -> None:
        if self.reattached:
            return
        binding = await self.manager.reattach_verified(
            task_id=self.config.task_id,
            context=self.worker.execution_context,
        )
        try:
            continuity = self.manager.continuity_digest(self.config.task_id)
            if suspension.sandbox_binding_digest is None:
                raise TrustedRecoveryError("suspension has no M5 continuity digest")
            if continuity != suspension.sandbox_binding_digest:
                raise TrustedRecoveryError(
                    "M5 continuity changed since suspension; refusing to execute pending action"
                )
        except BaseException:
            with contextlib.suppress(Exception):
                await self.manager.release_ownership(self.config.task_id)
            raise
        self.reattached = True
        self.network_verified = True
        self.sandbox_id = binding.sandbox_handle.id
        self.binding_id = binding.binding_id
        self.continuity_digest = continuity

    async def _invoke_m6(
        self,
        suspension: ExecutionSuspension,
        *,
        owner: str | None,
        lease_seconds: float,
        commit_continuation: bool,
        fault_hook: Any | None,
    ) -> ResumeOutcome:
        coordinator = self.runtime.suspension_coordinator
        if coordinator is None:
            raise TrustedRecoveryError("M6 tool runtime coordinator is unavailable")
        return await resume_execution_suspension(
            suspension.suspension_id,
            runtime=self.runtime,
            coordinator=coordinator,
            context=self.context,
            owner=owner or self.config.effective_runtime_instance_id,
            lease_seconds=lease_seconds,
            commit_continuation=commit_continuation,
            fault_hook=fault_hook,
        )

    async def reattach_for_cleanup(self) -> None:
        """Explicit cleanup-only reattachment used after a settlement was already durable."""

        await self._reattach_for_execution(self.suspension)

    async def dispose_sandbox(self) -> bool:
        """Destroy the original S1 after a terminal settlement; never create a replacement.

        Disposal is refused before any provider/reattachment work when the durable settlement or
        this process still contains an UNCERTAIN remote outcome. A False return means the provider
        did not confirm destruction; callers must not report normal cleanup in that case.
        """

        if self.manager.has_provider_uncertainty(self.config.task_id):
            raise TrustedRecoveryDisposalError(
                self._last_outcome,
                None,
                "provider operation outcome is UNCERTAIN; refusing to destroy S1",
            )
        if self._outcome_has_uncertain(self._last_outcome):
            raise TrustedRecoveryDisposalError(
                self._last_outcome,
                None,
                "settlement contains UNCERTAIN results; refusing to destroy S1",
            )
        if self._durable_settlement_has_uncertain():
            raise TrustedRecoveryDisposalError(
                self._last_outcome,
                None,
                "durable settlement contains UNCERTAIN results; refusing to destroy S1",
            )

        store = self.manager.binding_store
        if store is not None:
            durable = store.get_for_task(self.config.run_id, self.config.task_id)
            if durable is None:
                return True
            if durable.state == "CLOSED":
                return True
            if durable.state != "READY":
                return False
        if not self.reattached:
            await self._reattach_for_execution(self.suspension)
        return await self.manager.close(self.config.task_id)

    @staticmethod
    def _settlement_has_uncertain(settlement: Any | None) -> bool:
        if settlement is None:
            return False
        return any(item.outcome == "UNCERTAIN" for item in settlement.results)

    @classmethod
    def _outcome_has_uncertain(cls, outcome: ResumeOutcome | None) -> bool:
        if outcome is None:
            return False
        if cls._settlement_has_uncertain(outcome.settlement):
            return True
        suspension = outcome.suspension
        return cls._settlement_has_uncertain(
            suspension.settlement if suspension is not None else None
        )

    def _durable_settlement_has_uncertain(self) -> bool:
        """Read the durable settlement, not only the in-process provider marker.

        A resume may run in a new OS process: settlement UNCERTAIN is durable while the prior
        process's in-memory provider-uncertainty marker is gone. Failure to read the durable truth
        is treated as uncertain.
        """

        try:
            current = self.suspension_store.get(self.config.suspension_id)
        except Exception:
            return True
        return current is not None and self._settlement_has_uncertain(current.settlement)

    def _has_unresolved_remote_outcome(self) -> bool:
        return (
            self.manager.has_provider_uncertainty(self.config.task_id)
            or self._outcome_has_uncertain(self._last_outcome)
            or self._durable_settlement_has_uncertain()
        )

    async def close(self) -> RecoveryCleanupResult:
        """Release only safe ownership, then close local runtime resources.

        Cleanup is idempotent. It never destroys S1 and never force-unlocks ownership when the
        remote result is uncertain or local detach fails.
        """

        if self._cleanup_result is not None:
            return self._cleanup_result
        ownership = self.manager.has_ownership(self.config.task_id)
        uncertain = self._has_unresolved_remote_outcome()
        released = not ownership
        unresolved = False
        reason: str | None = None
        error: BaseException | None = None
        if ownership and uncertain:
            unresolved = True
            reason = (
                "provider operation outcome is uncertain; M5 ownership retained for manual "
                "safety review"
            )
            error = TrustedRecoveryError(reason)
        elif ownership and self._last_outcome is None:
            unresolved = True
            reason = (
                "recovery exited by exception with unconfirmed remote state; M5 ownership "
                "retained for manual safety review"
            )
            error = TrustedRecoveryError(reason)
        elif ownership:
            try:
                # Stage C gate/drain + local detach + runtime-cache invalidation happen here.
                await self.manager.release_ownership(self.config.task_id)
            except Exception as cleanup_error:
                unresolved = True
                reason = (
                    "local detach or ownership release failed; M5 ownership retained and not "
                    "handed off"
                )
                error = cleanup_error
            else:
                released = True

        runtime_closed = False
        try:
            await self.runtime.close()
            runtime_closed = True
        except Exception as runtime_error:
            if error is None:
                error = runtime_error
                reason = f"runtime close failed: {runtime_error}"

        idempotency_closed = False
        try:
            await self.idempotency.teardown()
            idempotency_closed = True
        except Exception as idempotency_error:
            if error is None:
                error = idempotency_error
                reason = f"idempotency close failed: {idempotency_error}"

        result = RecoveryCleanupResult(
            ownership_released=released,
            ownership_unresolved=unresolved,
            runtime_closed=runtime_closed,
            idempotency_closed=idempotency_closed,
            reason=reason,
            error=error,
        )
        self._cleanup_result = result
        return result


async def resume_durable_sandbox_suspension(
    config: TrustedRecoveryConfig,
    *,
    owner: str | None = None,
    lease_seconds: float = 60.0,
    commit_continuation: bool = True,
    fault_hook: Any | None = None,
    dispose_sandbox_on_settlement: bool = False,
) -> ResumeOutcome:
    """Explicit Stage D recovery entry over the existing M6 settlement path.

    This is a composition wrapper, not a second resume pipeline: it prepares the trusted
    Worker/M3/M4/M5 runtime and delegates to ``resume_execution_suspension``.
    """

    session = await TrustedRecoverySession.prepare(config)
    try:
        outcome = await session.resume(
            owner=owner,
            lease_seconds=lease_seconds,
            commit_continuation=commit_continuation,
            fault_hook=fault_hook,
        )
    except BaseException as recovery_error:
        cleanup = await session.close()
        _annotate_cleanup(recovery_error, cleanup)
        raise

    if dispose_sandbox_on_settlement and outcome.status in ("settled", "cleared"):
        try:
            disposed = await session.dispose_sandbox()
        except BaseException as disposal_error:
            cleanup = await session.close()
            if not isinstance(disposal_error, Exception):
                _annotate_cleanup(disposal_error, cleanup)
                raise
            raise TrustedRecoveryDisposalError(
                outcome,
                cleanup,
                f"sandbox disposal failed: {disposal_error}",
            ) from disposal_error
        if not disposed:
            cleanup = await session.close()
            raise TrustedRecoveryDisposalError(
                outcome,
                cleanup,
                "provider did not confirm sandbox destruction; cleanup was not reported as "
                "successful",
            )

    cleanup = await session.close()
    if cleanup.ownership_unresolved:
        raise TrustedRecoveryOwnershipUnresolved(outcome, cleanup)
    return outcome


def _require_recovery_files(config: TrustedRecoveryConfig) -> None:
    required = (
        (config.worker_store_path, "worker store"),
        (config.suspension_store_path, "suspension store"),
        (config.approval_store_path, "approval store"),
        (config.ledger_path, "M4 ledger"),
        (config.idempotency_path, "SQLite idempotency store"),
        (config.sandbox_binding_path, "M5 binding store"),
        (config.policy_path, "trusted policy"),
    )
    for path, label in required:
        if not path.is_file():
            raise TrustedRecoveryError(f"trusted recovery requires existing {label}: {path}")
    if not config.run_dir.is_dir():
        raise TrustedRecoveryError(f"trusted recovery run directory is missing: {config.run_dir}")


def _validate_suspension_identity(
    config: TrustedRecoveryConfig, suspension: ExecutionSuspension
) -> None:
    checks = (
        ("run_id", suspension.run_id, config.run_id),
        ("task_id", suspension.task_id, config.task_id),
        ("worker_id", suspension.worker_id, config.worker_id),
        ("profile_id", suspension.profile_id, config.profile_id),
    )
    mismatched = [name for name, stored, expected in checks if stored != expected]
    if mismatched:
        raise TrustedRecoveryError(
            "suspension/trusted recovery identity mismatch: " + ", ".join(mismatched)
        )
    if suspension.sandbox_required and suspension.sandbox_binding_digest is None:
        raise TrustedRecoveryError("sandbox-required suspension lacks an M5 continuity digest")


def _validate_worker_identity(
    config: TrustedRecoveryConfig,
    suspension: ExecutionSuspension,
    worker: WorkerInstance,
) -> None:
    if not worker.resumed:
        raise TrustedRecoveryError("worker was not reconstructed through trusted resume")
    if worker.worker_id != config.worker_id:
        raise TrustedRecoveryError("reconstructed worker_id does not match trusted input")
    if worker.profile.profile_id != config.profile_id:
        raise TrustedRecoveryError("reconstructed worker profile does not match trusted input")
    if worker.record.task_id != config.task_id:
        raise TrustedRecoveryError("reconstructed worker task does not match trusted input")
    if worker.execution_context.principal != config.expected_principal:
        raise TrustedRecoveryError("reconstructed worker principal does not match trusted input")
    if worker.execution_context.tenant_scope != config.expected_tenant_scope:
        raise TrustedRecoveryError("reconstructed worker tenant does not match trusted input")
    if worker.execution_context.attributes.get("run_id") != config.run_id:
        raise TrustedRecoveryError("reconstructed worker execution_context run_id changed")
    if suspension.agent_name is not None and suspension.agent_name != worker.worker_name:
        raise TrustedRecoveryError("suspension agent identity does not match reconstructed worker")
    if trusted_identity_digest(worker.execution_context) != suspension.trusted_identity_digest:
        raise TrustedRecoveryError("M2 trusted worker identity changed since suspension")
