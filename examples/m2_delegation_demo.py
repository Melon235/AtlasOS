"""Deterministic M2 delegation demo: AUTO, HITL, POLICY, escalation, and recovery."""

from __future__ import annotations

import asyncio
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from anycode.contracts.models import PolicyDecision
from anycode.hitl.approval import ApprovalManager
from anycode.identity.policy import PolicyEnforcer, PolicyRequest
from anycode.types import (
    AgentInfo,
    ApprovalConfig,
    ApprovalRequest,
    ApprovalResponse,
    ToolUseContext,
)

from atlasos.delegation import (
    DelegationApprovalGate,
    DelegationPolicyGate,
    DelegationRequest,
    DelegationRuntime,
    ExecutionContextStore,
    StaticReviewer,
    TrustedRootIdentity,
    bootstrap_root_context,
    effective_scopes,
)


class AllowPolicy:
    async def decide(self, request: PolicyRequest) -> PolicyDecision:
        return PolicyDecision(
            id="policy-allow",
            run_id=request.run_id,
            task_id=request.task_id,
            outcome="allow",
            policy_version="demo/1",
            reason_codes=("demo_allow",),
            correlation_id=request.correlation_id,
            generation=request.generation,
            attempt=request.attempt,
        )


class Approve:
    async def request_approval(self, request: ApprovalRequest) -> ApprovalResponse:
        return ApprovalResponse(
            request_id=request.id,
            approved=True,
            reason="demo approval",
            responded_at=datetime.now(UTC),
        )


def tool_context(actor: str = "planner") -> ToolUseContext:
    return ToolUseContext(agent=AgentInfo(name=actor, role="demo", model="fake"))


def request(delegatee: str, intent: str, scopes: tuple[str, ...]) -> DelegationRequest:
    return DelegationRequest(
        delegatee=delegatee,
        intent=intent,  # type: ignore[arg-type]
        requested_scopes=scopes,
        task_summary="demonstrate delegated authority",
    )


async def main() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = bootstrap_root_context(
            TrustedRootIdentity(
                principal="user:wang",
                main_agent="planner",
                scopes=("github.read", "github.write", "email.send"),
            )
        )
        approval = ApprovalManager(
            ApprovalConfig(
                enabled=True,
                require_approval_tasks=True,
                timeout_seconds=5.0,
                default_on_timeout="reject",
            ),
            Approve(),
        )
        runtime = DelegationRuntime(
            run_id="m2-demo",
            store=ExecutionContextStore(
                Path(temporary) / "run_execution_context.json", run_id="m2-demo"
            ),
            root_context=root,
            reviewer=StaticReviewer("AGREE"),
            policy_gate=DelegationPolicyGate(PolicyEnforcer(AllowPolicy(), fail_closed=True)),
            approval_gate=DelegationApprovalGate(approval),
        )

        auto = await runtime.propose(
            tool_context(),
            request("agent:github_worker", "research", ("github.read",)),
        )
        print(
            "AUTO:", auto.status, auto.required_gates, sorted(effective_scopes(auto.child_context))
        )

        hitl = await runtime.propose(
            tool_context(),
            request("agent:email_worker", "external_communication", ("email.send",)),
        )
        print("HITL:", hitl.status, hitl.required_gates, hitl.approval_outcome)

        escalation = await runtime.propose(
            tool_context(),
            request("agent:db_worker", "modify_data", ("database.write",)),
        )
        print("ESCALATION:", escalation.status, escalation.reason_codes)

        resumed = DelegationRuntime(
            run_id="m2-demo",
            store=ExecutionContextStore(
                Path(temporary) / "run_execution_context.json", run_id="m2-demo"
            ),
            root_context=root,
            reviewer=StaticReviewer("DISAGREE"),
        )
        print("RECOVERY equality:", resumed.snapshot_hash() == runtime.snapshot_hash())
        print("RECOVERED chain:", resumed.context_for("agent:github_worker").delegation)


if __name__ == "__main__":
    asyncio.run(main())
