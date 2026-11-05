"""Deterministic Stage D World: trusted worker + S1 + pre-execution M6 suspension."""

from __future__ import annotations

import json
from pathlib import Path

from anycode.identity.context import ExecutionContext
from anycode.types import LLMMessage, ToolSecurityPolicy, ToolUseBlock
from tests.helpers.fake_sandbox_provider import FakeSandboxProvider
from tests.integration.test_project_sandbox import make_repo

from atlasos.actions import ActionCatalog, ActionPolicy
from atlasos.approvals.models import ReviewerIdentity
from atlasos.approvals.store import SQLiteApprovalStore
from atlasos.config import AtlasConfig, ProviderConfig, SandboxConfig, ToolsConfig, TrajectoryConfig
from atlasos.ledger import SQLiteActionLedgerStore
from atlasos.sandbox import (
    ProjectSandboxManager,
    SandboxProjectConfig,
    SandboxWorkspaceTools,
    SQLiteSandboxBindingStore,
)
from atlasos.suspension.batch import BatchCall
from atlasos.suspension.models import ExecutionSuspension
from atlasos.suspension.store import SQLiteSuspensionStore
from atlasos.tools import AtlasToolRuntime
from atlasos.trusted_recovery import TrustedRecoveryConfig, TrustedRecoverySession
from atlasos.workers import ScopedWorkerFactory, WorkerAssignment, WorkerCatalog, WorkerStore

RUN_ID = "run-stage-d"
TASK_ID = "task-stage-d"
WORKER_ID = "worker-stage-d"
PROFILE_ID = "coding_worker"
PRINCIPAL = "user:stage-d"
TENANT = "stage-d"


def policy_payload(*, write_decision: str = "REQUIRE_HUMAN") -> dict:
    return {
        "name": "stage-d",
        "default": "DENY",
        "rules": [
            {"id": "allow-search", "decision": "ALLOW", "action": "control.search"},
            {"id": "allow-dispatch", "decision": "ALLOW", "action": "control.dispatch"},
            {"id": "allow-read", "decision": "ALLOW", "action": "filesystem.read"},
            {"id": "allow-shell", "decision": "ALLOW", "action": "shell.execute"},
            {"id": "write", "decision": write_decision, "action": "filesystem.write"},
        ],
    }


def write_policy(path: Path, *, write_decision: str = "REQUIRE_HUMAN") -> None:
    path.write_text(json.dumps(policy_payload(write_decision=write_decision)), encoding="utf-8")


def config(tmp_path: Path) -> AtlasConfig:
    return AtlasConfig(
        provider=ProviderConfig(model="fake-model", name="deepseek"),
        tools=ToolsConfig(mode="progressive"),
        trajectory=TrajectoryConfig(enabled=True, store_root="trajectory"),
        sandbox=SandboxConfig(
            enabled=True,
            provider="fake",
            image="fake@sha256:0123456789abcdef",
            workspace_root="/workspace/project",
        ),
    )


class World:
    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path
        self.state_root = tmp_path / "state"
        self.state_root.mkdir()
        self.run_dir = self.state_root / "runs" / RUN_ID
        self.run_dir.mkdir(parents=True)
        self.policy_path = self.state_root / "policy.json"
        write_policy(self.policy_path)
        self.config = config(tmp_path)
        self.provider = FakeSandboxProvider(tmp_path / "provider")
        self.suspension_store = SQLiteSuspensionStore(self.state_root / "suspension.db")
        self.approval_store = SQLiteApprovalStore(self.state_root / "approvals.db")
        self.ledger = SQLiteActionLedgerStore(self.state_root / "ledger.db")
        from anycode.tools.idempotency import SQLiteToolIdempotencyStore

        self.idempotency = SQLiteToolIdempotencyStore(self.state_root / "idempotency.db")
        self.manager: ProjectSandboxManager | None = None
        self.runtime: AtlasToolRuntime | None = None
        self.worker = None
        self.suspension: ExecutionSuspension | None = None
        self.sandbox_id: str | None = None
        self.binding_id: str | None = None
        self.context = None

    async def create_suspension(self) -> ExecutionSuspension:
        await self.idempotency.setup()
        parent = ExecutionContext(
            principal=PRINCIPAL,
            tenant_scope=TENANT,
            classification="internal",
            attributes={"run_id": RUN_ID},
        )
        worker_factory = ScopedWorkerFactory(
            model="fake-model",
            provider="deepseek",
            store=WorkerStore(self.state_root / "workers.json"),
            catalog=WorkerCatalog(),
        )
        worker = worker_factory.create(
            parent_context=parent,
            parent_agent="stage-d-test",
            assignment=WorkerAssignment(profile_id=PROFILE_ID, task="stage d fixture"),
            worker_id=WORKER_ID,
            task_id=TASK_ID,
        )
        self.worker = worker
        project = SandboxProjectConfig(
            provider_name="fake",
            image="fake@sha256:0123456789abcdef",
            workspace_root="/workspace/project",
        )
        manager = ProjectSandboxManager(
            self.provider,
            project,
            run_id=RUN_ID,
            binding_store=SQLiteSandboxBindingStore(self.state_root / "sandbox_bindings.db"),
            ownership_root=self.state_root,
        )
        self.manager = manager
        binding = await manager.ensure_binding(
            task_id=TASK_ID,
            source_repo=str(make_repo(self.root)),
            context=worker.execution_context,
            worker_id=WORKER_ID,
        )
        self.sandbox_id = binding.sandbox_handle.id
        self.binding_id = binding.binding_id
        tools = SandboxWorkspaceTools(manager, TASK_ID, self.provider)
        context = worker.tool_context(
            security_policy=ToolSecurityPolicy(workspace_root="/workspace/project")
        )
        self.context = context
        runtime = AtlasToolRuntime(
            RUN_ID,
            state_path=self.run_dir / "tool_state.json",
            conversation_path=self.run_dir / "tool_conversations.json",
            action_policy=ActionPolicy.from_file(self.policy_path),
            action_catalog=ActionCatalog(),
            action_ledger=self.ledger,
            idempotency_store=self.idempotency,
            suspension_store=self.suspension_store,
            approval_store=self.approval_store,
            sandbox_tools=tools,
            runtime_instance_id="stage-d-process-a",
        )
        self.runtime = runtime
        call = BatchCall(
            ordinal=0,
            call_id="write-call",
            tool_name="file_write",
            raw_input={
                "path": "/workspace/project/recovered.txt",
                "content": "approved recovery\n",
            },
            execution_key="stage-d:0",
        )
        plan = await runtime.preflight_batch(
            [call],
            context,
            model_response_id="stage-d-response",
            execution_scope="task:0",
        )
        assert plan.requires_human is True
        suspension = await runtime.suspension_coordinator.suspend(  # type: ignore[union-attr]
            plan,
            context,
            assistant_message=LLMMessage(
                role="assistant",
                content=[
                    ToolUseBlock(
                        id="write-call",
                        name="file_write",
                        input=call.raw_input,
                    )
                ],
            ),
            execution_scope="task:0",
            sandbox_binding_digest=runtime.sandbox_binding_digest(TASK_ID),
            sandbox_required=True,
            model_response_id="stage-d-response",
        )
        self.suspension = suspension
        assert self.sandbox_id is not None
        assert (
            self.provider.workspaces[self.sandbox_id] / "workspace/project/recovered.txt"
        ).exists() is False
        return suspension

    async def release_a(self) -> None:
        assert self.manager is not None
        await self.manager.release_ownership(TASK_ID)

    def recovery_config(
        self,
        *,
        worker_id: str = WORKER_ID,
        profile_id: str = PROFILE_ID,
        principal: str = PRINCIPAL,
        tenant: str = TENANT,
    ) -> TrustedRecoveryConfig:
        assert self.suspension is not None
        return TrustedRecoveryConfig(
            state_root=self.state_root,
            run_id=RUN_ID,
            task_id=TASK_ID,
            worker_id=worker_id,
            profile_id=profile_id,
            suspension_id=self.suspension.suspension_id,
            config=self.config,
            policy_path=self.policy_path,
            expected_principal=principal,
            expected_tenant_scope=tenant,
            provider_override=self.provider,
            runtime_instance_id="stage-d-process-b",
        )

    async def approve(self) -> None:
        await self._resolve("APPROVED")

    async def reject(self) -> None:
        await self._resolve("REJECTED")

    async def _resolve(self, decision: str) -> None:
        assert self.suspension is not None
        assert self.runtime is not None
        pending = self.approval_store.list_pending(suspension_id=self.suspension.suspension_id)
        for challenge in pending:
            result = self.runtime.approval_coordinator.resolve(  # type: ignore[union-attr]
                challenge.approval_id,
                expected_version=challenge.version,
                decision=decision,  # type: ignore[arg-type]
                reviewer=ReviewerIdentity(actor="reviewer", source="host"),
                reason=decision.lower(),
            )
            assert result.status == "resolved"


async def prepared_world(tmp_path: Path) -> tuple[World, TrustedRecoverySession]:
    world = World(tmp_path)
    await world.create_suspension()
    await world.release_a()
    session = await TrustedRecoverySession.prepare(world.recovery_config())
    return world, session
