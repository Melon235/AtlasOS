"""M2 required acceptance scenarios A-G and M1 regression boundary."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

from anycode.contracts.models import PolicyDecision
from anycode.hitl.approval import ApprovalManager
from anycode.identity.context import ExecutionContext
from anycode.identity.policy import PolicyEnforcer, PolicyRequest
from anycode.types import (
    AgentConfig,
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
from atlasos.tools import AtlasToolRuntime


def root(scopes: tuple[str, ...], *, expires_at=None) -> ExecutionContext:
    return bootstrap_root_context(
        TrustedRootIdentity(
            principal="user:wang",
            main_agent="planner",
            scopes=scopes,
            expires_at=expires_at,
        )
    )


def tool_context(actor: str = "planner"):
    return ToolUseContext(
        agent=AgentInfo(name=actor, role="test", model="fake"),
        execution_context=None,
    )


def request(
    *,
    delegatee: str,
    intent: str,
    scopes: tuple[str, ...],
    summary: str = "delegate work",
) -> DelegationRequest:
    return DelegationRequest(
        delegatee=delegatee,
        intent=intent,  # type: ignore[arg-type]
        requested_scopes=scopes,
        task_summary=summary,
    )


class PolicyAdapter:
    def __init__(self, outcome: str = "allow") -> None:
        self.outcome = outcome
        self.requests: list[PolicyRequest] = []

    async def decide(self, request: PolicyRequest) -> PolicyDecision:
        self.requests.append(request)
        return PolicyDecision(
            id=str(uuid.uuid4()),
            run_id=request.run_id,
            task_id=request.task_id,
            outcome="allow" if self.outcome == "allow" else "deny",
            policy_version="test/1",
            reason_codes=(f"{self.outcome}_test",),
            correlation_id=request.correlation_id,
            generation=request.generation,
            attempt=request.attempt,
        )


class ApprovalGate:
    def __init__(self, approved: bool = True) -> None:
        self.approved = approved
        self.requests: list[ApprovalRequest] = []

    async def request_approval(self, request: ApprovalRequest) -> ApprovalResponse:
        self.requests.append(request)
        return ApprovalResponse(
            request_id=request.id,
            approved=self.approved,
            reason="test",
            responded_at=datetime.now(UTC),
        )


def make_runtime(
    tmp_path: Path,
    *,
    root_context: ExecutionContext,
    reviewer=None,
    policy: PolicyAdapter | None = None,
    approval: ApprovalGate | None = None,
) -> DelegationRuntime:
    store = ExecutionContextStore(tmp_path / "run_execution_context.json", run_id="m2-run")
    policy_gate = DelegationPolicyGate(PolicyEnforcer(policy, fail_closed=True)) if policy else None
    approval_gate = None
    if approval is not None:
        manager = ApprovalManager(
            ApprovalConfig(
                enabled=True,
                require_approval_tasks=True,
                timeout_seconds=5.0,
                default_on_timeout="reject",
            ),
            approval,
        )
        approval_gate = DelegationApprovalGate(manager)
    return DelegationRuntime(
        run_id="m2-run",
        store=store,
        root_context=root_context,
        reviewer=reviewer or StaticReviewer("AGREE"),
        policy_gate=policy_gate,
        approval_gate=approval_gate,
    )


async def test_scenario_a_auto(tmp_path: Path) -> None:
    root_context = root(("github.read", "email.send"))
    runtime = make_runtime(tmp_path, root_context=root_context)
    outcome = await runtime.propose(
        tool_context(),
        request(delegatee="agent:github_worker", intent="research", scopes=("github.read",)),
    )
    assert outcome.status == "granted"
    assert outcome.required_gates == ()
    assert outcome.child_context is not None
    assert effective_scopes(outcome.child_context) == frozenset({"github.read"})


async def test_scenario_b_hitl(tmp_path: Path) -> None:
    root_context = root(("email.send",))
    approval = ApprovalGate(True)
    runtime = make_runtime(tmp_path, root_context=root_context, approval=approval)
    outcome = await runtime.propose(
        tool_context(),
        request(
            delegatee="agent:email_worker",
            intent="external_communication",
            scopes=("email.send",),
        ),
    )
    assert outcome.status == "granted"
    assert outcome.required_gates == ("HITL",)
    assert len(approval.requests) == 1
    assert approval.requests[0].context is not None
    assert approval.requests[0].context["principal"] == "user:wang"
    assert approval.requests[0].context["from_agent"] == "agent:planner"
    assert approval.requests[0].context["to_agent"] == "agent:email_worker"


async def test_scenario_c_policy_allow_and_deny(tmp_path: Path) -> None:
    root_context = root(("github.write",))
    allow_policy = PolicyAdapter("allow")
    runtime = make_runtime(tmp_path / "allow", root_context=root_context, policy=allow_policy)
    allowed = await runtime.propose(
        tool_context(),
        request(delegatee="agent:github_worker", intent="modify_data", scopes=("github.write",)),
    )
    assert allowed.status == "granted"
    assert allowed.required_gates == ("POLICY",)
    assert len(allow_policy.requests) == 1

    deny_policy = PolicyAdapter("deny")
    deny_runtime = make_runtime(tmp_path / "deny", root_context=root_context, policy=deny_policy)
    denied = await deny_runtime.propose(
        tool_context(),
        request(delegatee="agent:github_worker", intent="modify_data", scopes=("github.write",)),
    )
    assert denied.status == "denied"
    assert denied.child_context is None


async def test_scenario_d_policy_and_hitl_variants(tmp_path: Path) -> None:
    root_context = root(("credentials.read",))
    approval = ApprovalGate(True)
    policy = PolicyAdapter("allow")
    runtime = make_runtime(
        tmp_path / "both", root_context=root_context, policy=policy, approval=approval
    )
    outcome = await runtime.propose(
        tool_context(),
        request(
            delegatee="agent:credential_worker",
            intent="manage_credentials",
            scopes=("credentials.read",),
        ),
    )
    assert outcome.status == "granted"
    assert outcome.required_gates == ("POLICY", "HITL")

    policy_deny_approval = ApprovalGate(True)
    denied = await make_runtime(
        tmp_path / "policy-deny",
        root_context=root_context,
        policy=PolicyAdapter("deny"),
        approval=policy_deny_approval,
    ).propose(
        tool_context(),
        request(
            delegatee="agent:credential_worker",
            intent="manage_credentials",
            scopes=("credentials.read",),
        ),
    )
    assert denied.status == "denied"
    assert policy_deny_approval.requests == []

    denied_hitl = await make_runtime(
        tmp_path / "hitl-deny",
        root_context=root_context,
        policy=PolicyAdapter("allow"),
        approval=ApprovalGate(False),
    ).propose(
        tool_context(),
        request(
            delegatee="agent:credential_worker",
            intent="manage_credentials",
            scopes=("credentials.read",),
        ),
    )
    assert denied_hitl.status == "denied"


async def test_scenario_e_reviewer_disagreement_preserves_policy(tmp_path: Path) -> None:
    root_context = root(("github.write",))
    policy = PolicyAdapter("allow")
    approval = ApprovalGate(True)
    runtime = make_runtime(
        tmp_path,
        root_context=root_context,
        reviewer=StaticReviewer("DISAGREE"),
        policy=policy,
        approval=approval,
    )
    outcome = await runtime.propose(
        tool_context(),
        request(delegatee="agent:github_worker", intent="modify_data", scopes=("github.write",)),
    )
    assert outcome.status == "granted"
    assert outcome.required_gates == ("POLICY", "HITL")
    assert len(policy.requests) == 1
    assert len(approval.requests) == 1


async def test_scenario_f_hard_privilege_escalation(tmp_path: Path) -> None:
    root_context = root(("github.read",))
    reviewer = StaticReviewer("AGREE")
    policy = PolicyAdapter("allow")
    approval = ApprovalGate(True)
    runtime = make_runtime(
        tmp_path,
        root_context=root_context,
        reviewer=reviewer,
        policy=policy,
        approval=approval,
    )
    outcome = await runtime.propose(
        tool_context(),
        request(delegatee="agent:db_worker", intent="modify_data", scopes=("database.write",)),
    )
    assert outcome.status == "denied"
    assert outcome.reason_codes == ("scope_escalation",)
    assert reviewer.calls == 0
    assert policy.requests == []
    assert approval.requests == []


async def test_scenario_g_recovery_and_child_config(tmp_path: Path) -> None:
    root_context = root(("github.read",))
    runtime = make_runtime(tmp_path, root_context=root_context)
    outcome = await runtime.propose(
        tool_context(),
        request(delegatee="agent:github_worker", intent="research", scopes=("github.read",)),
    )
    assert outcome.status == "granted"
    assert outcome.child_context is not None
    child = outcome.child_context
    resumed = make_runtime(tmp_path, root_context=root_context, reviewer=StaticReviewer("DISAGREE"))
    restored = resumed.context_for("agent:github_worker")
    assert restored == child
    assert resumed.snapshot_hash() == runtime.snapshot_hash()

    base = AgentConfig(name="github_worker", model="fake", provider="fake", tools=[])
    child_config = resumed.build_child_agent_config(base, outcome)
    assert child_config.execution_context == child


async def test_m1_regression_tool_runtime_unchanged_and_no_capability_enforcement() -> None:
    calls: list[str] = []
    external = _external_tool(calls)
    tool_runtime = AtlasToolRuntime("m1-regression", external_tools=(external,))
    assert tool_runtime.native_tool_names == tuple(sorted(tool_runtime.native_tool_names))
    assert "atlas_rag.search" not in tool_runtime.native_tool_names
    result = await tool_runtime.search("policy")
    assert result.is_error is False
    dispatch = await tool_runtime.executor.execute(
        "dispatch_action",
        {"tool_id": "atlas_rag.search", "arguments": {"text": "x"}},
        ToolUseContext(agent=AgentInfo(name="worker", role="test", model="fake")),
    )
    assert dispatch.is_error is False
    assert calls == ["atlas_rag.search"]


def _external_tool(calls: list[str]):
    from anycode.types import ToolDefinition, ToolResult
    from pydantic import BaseModel

    class Input(BaseModel):
        text: str = ""

    async def execute(validated: Input, context: ToolUseContext) -> ToolResult:
        del context
        calls.append("atlas_rag.search")
        return ToolResult(data=validated.text, is_error=False)

    return ToolDefinition(
        name="atlas_rag.search",
        description="Search internal policy knowledge.",
        input_model=Input,
        execute=execute,
        side_effecting=False,
    )
