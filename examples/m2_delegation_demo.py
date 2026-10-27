"""Deterministic M2 central authorization demo: low-privilege planner and specialists."""

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
    AuthorizationRequest,
    AuthorizationRuntime,
    DelegationApprovalGate,
    DelegationPolicyGate,
    DelegationRequest,
    ExecutionContextStore,
    RunAuthorityStore,
    StaticReviewer,
    TrustedRunIdentity,
    bootstrap_run_authority,
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


def auth_request(target: str, intent: str, scopes: tuple[str, ...]) -> AuthorizationRequest:
    return AuthorizationRequest(
        target_agent=target,
        intent=intent,  # type: ignore[arg-type]
        requested_scopes=scopes,
        task_summary="demonstrate central authority",
    )


async def main() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        authority, base = bootstrap_run_authority(
            TrustedRunIdentity(
                principal="user:wang",
                run_id="m2-demo",
                main_agent="planner",
                allowed_scopes=("agent.orchestrate", "github.read", "email.send"),
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
        runtime = AuthorizationRuntime(
            store=ExecutionContextStore(
                Path(temporary) / "run_execution_context.json", run_id="m2-demo"
            ),
            run_authority=authority,
            authority_store=RunAuthorityStore(
                Path(temporary) / "run_authority.json", run_id="m2-demo"
            ),
            base_context=base,
            main_agent="planner",
            reviewer=StaticReviewer("AGREE"),
            policy_gate=DelegationPolicyGate(PolicyEnforcer(AllowPolicy(), fail_closed=True)),
            approval_gate=DelegationApprovalGate(approval),
        )

        planner = await runtime.authorize_agent(
            tool_context(),
            auth_request("agent:planner", "research", ("agent.orchestrate",)),
        )
        print("PLANNER:", planner.status, planner.required_gates)

        github = await runtime.authorize_agent(
            tool_context(),
            auth_request("agent:github_worker", "research", ("github.read",)),
        )
        print("GITHUB:", github.status, github.required_gates)

        helper = await runtime.delegate_capability(
            tool_context("github_worker"),
            DelegationRequest(
                delegatee="agent:issue_reader",
                intent="research",
                requested_scopes=("github.read",),
                task_summary="explicit attenuation",
            ),
        )
        print("DELEGATION:", helper.status, helper.operation)

        outside = await runtime.authorize_agent(
            tool_context(),
            auth_request("agent:db_worker", "modify_data", ("database.admin",)),
        )
        print("OUTSIDE:", outside.status, outside.reason_codes)

        planner_context = runtime.context_for("agent:planner")
        print("PLANNER SCOPES:", sorted(effective_scopes(planner_context)))
        print(
            "WORKER SCOPES:",
            sorted(effective_scopes(runtime.context_for("github_worker"))),
        )
        print("HELPER CHAIN:", helper.child_context.delegation if helper.child_context else None)

        resumed = AuthorizationRuntime(
            store=ExecutionContextStore(
                Path(temporary) / "run_execution_context.json", run_id="m2-demo"
            ),
            run_authority=authority,
            authority_store=RunAuthorityStore(
                Path(temporary) / "run_authority.json", run_id="m2-demo"
            ),
            base_context=base,
            main_agent="planner",
            reviewer=StaticReviewer("DISAGREE"),
        )
        print("RECOVERY equality:", resumed.snapshot_hash() == runtime.snapshot_hash())


if __name__ == "__main__":
    asyncio.run(main())
