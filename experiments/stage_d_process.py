"""Real OS-process worker for the Stage D HITL recovery evaluator.

Modes are intentionally narrow deployment worker roles, not a second runtime:

* ``process-a`` creates the trusted Worker, S1, and the pre-execution M6 suspension.
* ``review-approve`` / ``review-reject`` resolve the real durable ApprovalStore challenge.
* ``recover`` reconstructs the trusted runtime, reattaches only S1, and delegates to M6.
* ``cleanup-remote`` is evaluator cleanup for scenarios where recovery must fail closed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from anycode.identity.context import ExecutionContext
from anycode.runstore.store import FilesystemRunStore
from anycode.sandbox import SandboxHandle
from anycode.tools.idempotency import SQLiteToolIdempotencyStore
from anycode.types import (
    LLMMessage,
    ToolSecurityPolicy,
    ToolUseBlock,
)

from atlasos.actions import ActionCatalog, ActionPolicy
from atlasos.approvals.coordinator import ApprovalCoordinator
from atlasos.approvals.models import ReviewerIdentity
from atlasos.approvals.store import SQLiteApprovalStore
from atlasos.config import AtlasConfig, load_config
from atlasos.ledger import SQLiteActionLedgerStore
from atlasos.sandbox import (
    OpenSandboxOptions,
    ProjectSandboxManager,
    SandboxProjectConfig,
    SandboxWorkspaceTools,
    SQLiteSandboxBindingStore,
    build_sandbox_provider,
)
from atlasos.suspension.batch import BatchCall
from atlasos.suspension.store import SQLiteSuspensionStore
from atlasos.tools import AtlasToolRuntime
from atlasos.trajectory.recorder import TrajectoryRecorder
from atlasos.trusted_recovery import (
    TrustedRecoveryConfig,
    TrustedRecoveryDisposalError,
    TrustedRecoverySession,
)
from atlasos.workers import ScopedWorkerFactory, WorkerAssignment, WorkerCatalog, WorkerStore

TARGET_PATH = "/workspace/project/recovered.txt"
TARGET_CONTENT = "approved recovery\n"
MARKER_PATH = "/workspace/project/stage-d-marker.txt"


def _write_pid(path: str) -> None:
    if path:
        Path(path).write_text(str(os.getpid()), encoding="utf-8")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def _provider(config: AtlasConfig):
    sandbox = config.sandbox
    return build_sandbox_provider(
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


def _project(config: AtlasConfig) -> SandboxProjectConfig:
    sandbox = config.sandbox
    return SandboxProjectConfig(
        provider_name=sandbox.provider,
        image=sandbox.image,
        snapshot=sandbox.snapshot,
        workspace_root=sandbox.workspace_root,
        max_bundle_bytes=sandbox.max_bundle_bytes,
        network=sandbox.network,
    )


def _recorder(config: AtlasConfig, run_dir: Path, run_id: str) -> TrajectoryRecorder:
    return TrajectoryRecorder(
        FilesystemRunStore(run_dir / config.trajectory.store_root),
        run_id,
        agent_name="atlas-stage-d",
        model=config.provider.model,
    )


class _CountingProvider:
    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.counts = {
            "create": 0,
            "inspect_existing": 0,
            "attach_existing": 0,
            "detach_local": 0,
            "write_file": 0,
            "read_file": 0,
            "destroy": 0,
            "cancel": 0,
            "execute": 0,
        }

    def __getattr__(self, name: str):
        return getattr(self.inner, name)

    def capabilities(self):
        return self.inner.capabilities()

    async def create(self, spec):
        self.counts["create"] += 1
        return await self.inner.create(spec)

    async def inspect_existing(self, sandbox_id: str):
        self.counts["inspect_existing"] += 1
        return await self.inner.inspect_existing(sandbox_id)

    async def attach_existing(self, sandbox_id: str) -> None:
        self.counts["attach_existing"] += 1
        await self.inner.attach_existing(sandbox_id)

    async def detach_local(self, sandbox_id: str) -> None:
        self.counts["detach_local"] += 1
        await self.inner.detach_local(sandbox_id)

    async def write_file(self, handle, path: str, data: bytes):
        self.counts["write_file"] += 1
        return await self.inner.write_file(handle, path, data)

    async def read_file(self, handle, path: str):
        self.counts["read_file"] += 1
        return await self.inner.read_file(handle, path)

    async def destroy(self, handle):
        self.counts["destroy"] += 1
        return await self.inner.destroy(handle)

    async def cancel(self, handle):
        self.counts["cancel"] += 1
        return await self.inner.cancel(handle)

    async def execute(self, handle, command):
        self.counts["execute"] += 1
        return await self.inner.execute(handle, command)


class _FaultWriteProvider(_CountingProvider):
    """Real provider that blocks after the remote write effect is observed."""

    def __init__(self, inner: Any, *, signal: Path, target: str) -> None:
        super().__init__(inner)
        self.signal = signal
        self.target = target

    async def write_file(self, handle, path: str, data: bytes):
        result = await super().write_file(handle, path, data)
        if path == self.target:
            self.signal.parent.mkdir(parents=True, exist_ok=True)
            self.signal.write_text("remote write completed; result not settled", encoding="utf-8")
            await asyncio.Event().wait()
        return result


def _context(worker, config: AtlasConfig) -> Any:
    return worker.tool_context(
        security_policy=ToolSecurityPolicy(workspace_root=config.sandbox.workspace_root)
    )


async def _process_a(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    state_root = Path(args.state_root)
    run_dir = state_root / "runs" / args.run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    provider = _provider(config)
    recorder = _recorder(config, run_dir, args.run_id)
    parent = ExecutionContext(
        principal=args.principal,
        tenant_scope=args.tenant,
        classification="internal",
        attributes={"run_id": args.run_id},
    )
    factory = ScopedWorkerFactory(
        model=config.provider.model,
        provider=config.provider.name,
        store=WorkerStore(state_root / "workers.json"),
        catalog=WorkerCatalog(),
        trajectory_recorder=recorder,
    )
    worker = factory.create(
        parent_context=parent,
        parent_agent="stage-d-process-a",
        assignment=WorkerAssignment(profile_id=args.profile_id, task="Stage D HITL fixture"),
        worker_id=args.worker_id,
        task_id=args.task_id,
    )
    project = _project(config)
    manager = ProjectSandboxManager(
        provider,
        project,
        run_id=args.run_id,
        binding_store=SQLiteSandboxBindingStore(state_root / "sandbox_bindings.db"),
        ownership_root=state_root,
        trajectory_recorder=recorder,
    )
    binding = await manager.ensure_binding(
        task_id=args.task_id,
        source_repo=args.source_repo,
        context=worker.execution_context,
        worker_id=args.worker_id,
    )
    marker = await provider.write_file(binding.sandbox_handle, MARKER_PATH, b"initial\n")
    if not marker.ok:
        raise RuntimeError(f"initial marker write failed: {marker.error}")
    missing = await provider.read_file(binding.sandbox_handle, TARGET_PATH)
    if missing.ok:
        raise RuntimeError("pending target already exists before approval")

    suspension_store = SQLiteSuspensionStore(state_root / "suspension.db")
    approval_store = SQLiteApprovalStore(state_root / "approvals.db")
    ledger = SQLiteActionLedgerStore(state_root / "ledger.db")
    idempotency = SQLiteToolIdempotencyStore(state_root / "idempotency.db")
    await idempotency.setup()
    policy = ActionPolicy.from_file(Path(args.policy))
    tools = SandboxWorkspaceTools(manager, args.task_id, provider)
    runtime = AtlasToolRuntime(
        args.run_id,
        state_path=run_dir / "tool_state.json",
        conversation_path=run_dir / "tool_conversations.json",
        action_policy=policy,
        action_catalog=ActionCatalog(),
        action_ledger=ledger,
        idempotency_store=idempotency,
        suspension_store=suspension_store,
        approval_store=approval_store,
        sandbox_tools=tools,
        trajectory_recorder=recorder,
        runtime_instance_id=f"stage-d-a-{os.getpid()}",
    )
    context = _context(worker, config)
    call = BatchCall(
        ordinal=0,
        call_id="approved-write",
        tool_name="file_write",
        raw_input={"path": TARGET_PATH, "content": TARGET_CONTENT},
        execution_key="stage-d:0",
    )
    plan = await runtime.preflight_batch(
        [call],
        context,
        model_response_id="stage-d-response",
        execution_scope="task:0",
    )
    if not plan.requires_human:
        raise RuntimeError("trusted policy did not require human approval")
    suspension = await runtime.suspension_coordinator.suspend(  # type: ignore[union-attr]
        plan,
        context,
        assistant_message=LLMMessage(
            role="assistant",
            content=[
                ToolUseBlock(
                    id=call.call_id,
                    name=call.tool_name,
                    input=call.raw_input,
                )
            ],
        ),
        execution_scope="task:0",
        sandbox_binding_digest=runtime.sandbox_binding_digest(args.task_id),
        sandbox_required=True,
        model_response_id="stage-d-response",
    )
    approvals = approval_store.list_for_suspension(suspension.suspension_id)
    _write_json(
        Path(args.ready),
        {
            "mode": "process-a",
            "pid": os.getpid(),
            "suspension_id": suspension.suspension_id,
            "sandbox_id": binding.sandbox_handle.id,
            "binding_id": binding.binding_id,
            "continuity_digest": manager.continuity_digest(args.task_id),
            "approval_ids": [item.approval_id for item in approvals],
            "approval_states": [item.state for item in approvals],
            "provider_create_count": manager.create_count,
            "target_exists_before_approval": False,
        },
    )
    # Stay alive holding the real ownership lock until the evaluator kills this OS process.
    await asyncio.Event().wait()
    return 0


async def _review(args: argparse.Namespace) -> int:
    suspension_store = SQLiteSuspensionStore(Path(args.state_root) / "suspension.db")
    approval_store = SQLiteApprovalStore(Path(args.state_root) / "approvals.db")
    coordinator = ApprovalCoordinator(approval_store)
    suspension = suspension_store.require(args.suspension_id)
    statuses: list[dict[str, Any]] = []
    for challenge in approval_store.list_for_suspension(suspension.suspension_id):
        if challenge.state != "PENDING":
            statuses.append(
                {
                    "approval_id": challenge.approval_id,
                    "status": "already_resolved",
                    "state": challenge.state,
                }
            )
            continue
        decision = "APPROVED" if args.mode == "review-approve" else "REJECTED"
        resolution = coordinator.resolve(
            challenge.approval_id,
            expected_version=challenge.version,
            decision=decision,
            reviewer=ReviewerIdentity(actor=args.reviewer, source="host"),
            reason=f"stage-d {decision.lower()}",
        )
        statuses.append(
            {
                "approval_id": challenge.approval_id,
                "status": resolution.status,
                "state": resolution.challenge.state if resolution.challenge else None,
            }
        )
    _write_json(
        Path(args.output),
        {
            "mode": args.mode,
            "pid": os.getpid(),
            "suspension_id": suspension.suspension_id,
            "statuses": statuses,
        },
    )
    return 0


def _recovery_config(
    args: argparse.Namespace, config: AtlasConfig, provider
) -> TrustedRecoveryConfig:
    return TrustedRecoveryConfig(
        state_root=Path(args.state_root),
        run_id=args.run_id,
        task_id=args.task_id,
        worker_id=args.worker_id,
        profile_id=args.profile_id,
        suspension_id=args.suspension_id,
        config=config,
        policy_path=Path(args.policy),
        expected_principal=args.principal,
        expected_tenant_scope=args.tenant,
        provider_override=provider,
        runtime_instance_id=f"stage-d-b-{os.getpid()}",
    )


def _sqlite_idempotency_rows(path: Path) -> list[dict[str, Any]]:
    try:
        with sqlite3.connect(path) as connection:
            rows = connection.execute(
                "SELECT tool_name, key, input_fingerprint, result_json IS NOT NULL "
                "FROM tool_idempotency ORDER BY tool_name, key"
            ).fetchall()
    except sqlite3.Error:
        return []
    return [
        {
            "tool_name": row[0],
            "key_hash": row[1],
            "fingerprint": row[2],
            "completed": bool(row[3]),
        }
        for row in rows
    ]


async def _recover(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    provider = _CountingProvider(_provider(config))
    if args.fault_write_block:
        provider = _FaultWriteProvider(  # type: ignore[assignment]
            provider.inner,
            signal=Path(args.fault_signal),
            target=TARGET_PATH,
        )
    session: TrustedRecoverySession | None = None
    output: dict[str, Any] = {
        "mode": "recover",
        "pid": os.getpid(),
        "run_id": args.run_id,
        "suspension_id": args.suspension_id,
        "provider_counts": provider.counts,
    }
    try:
        recovery = _recovery_config(args, config, provider)
        session = await TrustedRecoverySession.prepare(recovery)
        output.update(
            {
                "worker_id": session.worker.worker_id,
                "profile_id": session.worker.profile.profile_id,
                "runtime_instance_id": session.runtime.runtime_instance_id,
                "policy_name": session.policy.name,
            }
        )

        def hook(point: str) -> None:
            if args.crash_after_settlement and point == "resume_after_settlement":
                os._exit(70)

        outcome = await session.resume(
            owner=f"stage-d-b-{os.getpid()}",
            lease_seconds=args.lease_seconds,
            fault_hook=hook,
        )
        output.update(
            {
                "outcome_status": outcome.status,
                "outcome_reason": outcome.reason,
                "suspension_state": (
                    outcome.suspension.state if outcome.suspension is not None else None
                ),
                "settlement_results": (
                    [
                        {
                            "call_id": item.call_id,
                            "tool_name": item.tool_name,
                            "outcome": item.outcome,
                            "is_error": item.is_error,
                        }
                        for item in outcome.settlement.results
                    ]
                    if outcome.settlement is not None
                    else []
                ),
                "settlement_digest": (
                    outcome.settlement.result_batch_digest
                    if outcome.settlement is not None
                    else None
                ),
                "continuation_count": len(outcome.continuation),
                "reattached": session.reattached,
                "network_verified": session.network_verified,
                "sandbox_id": session.sandbox_id,
                "binding_id": session.binding_id,
                "continuity_digest": session.continuity_digest,
                "manager_create_count": session.manager.create_count,
                "manager_destroy_count": session.manager.destroy_count,
                "provider_counts": provider.counts,
                "approval_states": [
                    item.state
                    for item in session.approval_store.list_for_suspension(args.suspension_id)
                ],
                "ledger": [
                    {"tool_name": item.tool_name, "state": item.state}
                    for item in session.ledger.list()
                ],
                "idempotency": _sqlite_idempotency_rows(Path(args.state_root) / "idempotency.db"),
            }
        )
        continuation = (
            session.runtime.conversation_store.load(args.task_id)
            if session.runtime is not None
            else None
        )
        output["continuation_present"] = continuation is not None
        output["continuation"] = continuation
        if args.dispose and not session.reattached and outcome.status == "settled":
            # SETTLED backfill intentionally did not connect to S1. Reattach only for explicit
            # cleanup/read-back after the durable continuation was applied; no tool is executed.
            await session.reattach_for_cleanup()
        if session.reattached:
            handle = session.manager.bindings[args.task_id].sandbox_handle
            read = await provider.read_file(handle, TARGET_PATH)
            output["target_content"] = (
                read.data.decode("utf-8", errors="replace") if read.ok and read.data else None
            )
        else:
            output["target_content"] = None
        if args.dispose and outcome.status in ("settled", "cleared"):
            try:
                output["disposed"] = await session.dispose_sandbox()
            except TrustedRecoveryDisposalError as error:
                # UNCERTAIN (or another unsafe disposal condition) deliberately leaves S1 and
                # ownership unresolved for an operator. Record the refusal and the real cleanup
                # result; never rewrite it as a successful cleanup.
                cleanup = await session.close()
                output["disposed"] = error.disposed
                output["disposal_refused"] = str(error)
                output["disposal_cleanup"] = cleanup.describe()
            output["manager_destroy_count"] = session.manager.destroy_count
            output["provider_counts"] = provider.counts
        elif session.reattached:
            # A failed/pending recovery must not leave ownership held by this process.
            await session.manager.release_ownership(args.task_id)
        output["status"] = "OK"
        _write_json(Path(args.output), output)
        return 0
    except BaseException as error:  # pragma: no cover - child failure surface
        output.update(
            {
                "status": "ERROR",
                "error_type": type(error).__name__,
                "error": str(error),
                "provider_counts": provider.counts,
            }
        )
        _write_json(Path(args.output), output)
        return 1
    finally:
        if session is not None:
            await session.close()


async def _cleanup_remote(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    store = SQLiteSandboxBindingStore(Path(args.state_root) / "sandbox_bindings.db")
    durable = store.get_for_task(args.run_id, args.task_id)
    provider = _provider(config)
    output: dict[str, Any] = {"mode": "cleanup-remote", "pid": os.getpid()}
    if durable is None:
        output["cleanup"] = True
        output["status"] = "NO_BINDING"
        _write_json(Path(args.output), output)
        return 0
    try:
        await provider.attach_existing(durable.sandbox_id)
    except Exception as error:
        output["cleanup"] = True
        output["status"] = "ALREADY_ABSENT"
        output["error"] = str(error)
        _write_json(Path(args.output), output)
        return 0
    context = ExecutionContext(
        principal=args.principal,
        tenant_scope=args.tenant,
        classification="internal",
        attributes={"run_id": args.run_id, "task_id": args.task_id},
    )
    handle = SandboxHandle(
        id=durable.sandbox_id,
        provider=durable.provider_name,
        run_id=durable.run_id,
        task_id=durable.task_id,
        correlation_id=f"{durable.run_id}:{durable.task_id}",
        context=context,
        capabilities=provider.capabilities(),
    )
    result = await provider.destroy(handle)
    await provider.detach_local(durable.sandbox_id)
    output.update({"cleanup": result.ok, "status": "DESTROYED", "result": result.ok})
    _write_json(Path(args.output), output)
    return 0 if result.ok else 1


async def _main(args: argparse.Namespace) -> int:
    _write_pid(args.pid_file)
    if args.mode == "process-a":
        return await _process_a(args)
    if args.mode in ("review-approve", "review-reject"):
        return await _review(args)
    if args.mode == "recover":
        return await _recover(args)
    if args.mode == "cleanup-remote":
        return await _cleanup_remote(args)
    raise ValueError(f"unknown mode {args.mode!r}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        required=True,
        choices=("process-a", "review-approve", "review-reject", "recover", "cleanup-remote"),
    )
    parser.add_argument("--state-root", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--worker-id", required=True)
    parser.add_argument("--profile-id", default="coding_worker")
    parser.add_argument("--principal", default="user:stage-d")
    parser.add_argument("--tenant", default="stage-d")
    parser.add_argument("--config", required=True)
    parser.add_argument("--policy", required=True)
    parser.add_argument("--suspension-id", default="")
    parser.add_argument("--source-repo", default="")
    parser.add_argument("--ready", default="")
    parser.add_argument("--output", default="")
    parser.add_argument("--reviewer", default="stage-d-reviewer")
    parser.add_argument("--lease-seconds", type=float, default=60.0)
    parser.add_argument("--dispose", action="store_true")
    parser.add_argument("--crash-after-settlement", action="store_true")
    parser.add_argument("--fault-write-block", action="store_true")
    parser.add_argument("--fault-signal", default="")
    parser.add_argument("--pid-file", default="")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_main(args)))


if __name__ == "__main__":
    main()
