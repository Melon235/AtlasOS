"""M2 semantic correction: central authorization and explicit delegation."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
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
from pydantic import ValidationError

from atlasos.delegation import (
    AuthorizationRequest,
    AuthorizationRuntime,
    DelegationApprovalGate,
    DelegationPolicyGate,
    DelegationRequest,
    ExecutionContextStore,
    RunAuthority,
    RunAuthorityStore,
    StaticReviewer,
    TrustedRunIdentity,
    bootstrap_run_authority,
    effective_scopes,
    run_authority_hash,
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


class CapturingReviewer:
    def __init__(self, verdict: str = "AGREE") -> None:
        self.verdict = verdict
        self.inputs = []

    async def review(self, reviewer_input):
        self.inputs.append(reviewer_input)
        from atlasos.delegation import ReviewerResult

        return ReviewerResult(verdict=self.verdict)  # type: ignore[arg-type]


def tool_context(actor: str = "planner"):
    return ToolUseContext(agent=AgentInfo(name=actor, role="test", model="fake"))


def auth_request(
    target: str,
    intent: str,
    scopes: tuple[str, ...],
    *,
    summary: str = "authorize worker",
) -> AuthorizationRequest:
    return AuthorizationRequest(
        target_agent=target,
        intent=intent,  # type: ignore[arg-type]
        requested_scopes=scopes,
        task_summary=summary,
    )


def delegation_request(
    delegatee: str,
    intent: str,
    scopes: tuple[str, ...],
) -> DelegationRequest:
    return DelegationRequest(
        delegatee=delegatee,
        intent=intent,  # type: ignore[arg-type]
        requested_scopes=scopes,
        task_summary="explicit attenuation",
    )


def make_central(
    tmp_path: Path,
    ceiling: tuple[str, ...],
    *,
    expires_at: datetime | None = None,
    reviewer=None,
    policy: PolicyAdapter | None = None,
    approval: ApprovalGate | None = None,
    run_id: str = "run-1",
):
    authority, base = bootstrap_run_authority(
        TrustedRunIdentity(
            principal="user:wang",
            run_id=run_id,
            main_agent="planner",
            allowed_scopes=ceiling,
            expires_at=expires_at,
        )
    )
    store = ExecutionContextStore(tmp_path / "run_execution_context.json", run_id=run_id)
    authority_store = RunAuthorityStore(tmp_path / "run_authority.json", run_id=run_id)
    policy_gate = DelegationPolicyGate(PolicyEnforcer(policy, fail_closed=True)) if policy else None
    approval_gate = None
    if approval is not None:
        approval_gate = DelegationApprovalGate(
            ApprovalManager(
                ApprovalConfig(
                    enabled=True,
                    require_approval_tasks=True,
                    timeout_seconds=5.0,
                    default_on_timeout="reject",
                ),
                approval,
            )
        )
    runtime = AuthorizationRuntime(
        store=store,
        run_authority=authority,
        authority_store=authority_store,
        base_context=base,
        main_agent="planner",
        reviewer=reviewer or StaticReviewer("AGREE"),
        policy_gate=policy_gate,
        approval_gate=approval_gate,
    )
    return runtime, authority, base, approval


def scopes_of(runtime, actor: str) -> frozenset[str]:
    context = runtime.context_for(actor)
    assert context is not None
    return effective_scopes(context)


async def authorize(
    runtime: AuthorizationRuntime,
    target: str,
    intent: str,
    scopes: tuple[str, ...],
    *,
    actor: str = "planner",
):
    return await runtime.authorize_agent(tool_context(actor), auth_request(target, intent, scopes))


# A01/A02
def test_run_authority_is_trusted_immutable_and_canonical(tmp_path: Path) -> None:
    authority = RunAuthority(
        principal="user:wang",
        run_id="run-1",
        allowed_scopes=("rag.read", "github.read", "github.read"),
    )
    assert authority.allowed_scopes == ("github.read", "rag.read")
    with pytest.raises(ValidationError):
        RunAuthority.model_validate(
            {
                "principal": "user:wang",
                "run_id": "run-1",
                "allowed_scopes": ("github.read",),
                "roles": ("admin",),
            }
        )
    with pytest.raises(ValidationError):
        authority.allowed_scopes = ("email.send",)  # type: ignore[misc]

    store = RunAuthorityStore(tmp_path / "run_authority.json", run_id="run-1")
    store.save(authority)
    assert store.load() == authority
    assert run_authority_hash(authority) == run_authority_hash(
        RunAuthority(
            principal="user:wang",
            run_id="run-1",
            allowed_scopes=("github.read", "rag.read"),
        )
    )


# A03/A04/A05/A06
async def test_main_and_child_use_same_central_runtime_without_parent_scope(
    tmp_path: Path,
) -> None:
    approval = ApprovalGate(True)
    runtime, authority, base, _ = make_central(
        tmp_path, ("agent.orchestrate", "github.read", "email.send"), approval=approval
    )
    assert base.delegation == ()

    main = await authorize(runtime, "agent:planner", "research", ("agent.orchestrate",))
    assert main.status == "granted"
    assert main.operation == "central_authorization"
    assert main.required_gates == ("HITL",)

    child = await authorize(runtime, "agent:github_worker", "research", ("github.read",))
    assert child.status == "granted"
    assert child.operation == "central_authorization"
    assert child.required_gates == ()
    assert scopes_of(runtime, "agent:planner") == frozenset({"agent.orchestrate"})
    assert scopes_of(runtime, "agent:github_worker") == frozenset({"github.read"})
    # Same validator/router/reviewer/gates/issuer pipeline for main and child.
    assert runtime.stats["central_authorized"] == 2
    assert runtime.stats["reviewer_agree"] == 2


# A07/A08/A09/A10/A11/A12/A13/A14/A15
async def test_outside_ceiling_is_hard_denied_before_all_decision_components(
    tmp_path: Path,
) -> None:
    reviewer = CapturingReviewer()
    policy = PolicyAdapter("allow")
    approval = ApprovalGate(True)
    runtime, authority, base, _ = make_central(
        tmp_path,
        ("github.read", "email.send"),
        reviewer=reviewer,
        policy=policy,
        approval=approval,
    )
    outcome = await authorize(runtime, "agent:db_worker", "modify_data", ("database.admin",))
    assert outcome.status == "denied"
    assert outcome.reason_codes == ("outside_run_authority",)
    assert outcome.route is None
    assert outcome.child_context is None
    assert reviewer.inputs == []
    assert policy.requests == []
    assert approval.requests == []
    assert runtime.stats["reviewer_agree"] == 0
    assert runtime.stats["policy_deny"] == 0
    assert runtime.stats["hitl_approve"] == 0

    # Human approval cannot override the ceiling.
    assert (
        await runtime.authorize_agent(
            tool_context(), auth_request("agent:db_worker", "administrative", ("database.admin",))
        )
    ).status == "denied"
    assert approval.requests == []

    # POLICY cannot expand the ceiling either: it is never invoked.
    assert policy.requests == []


# A16/A17/A18
async def test_central_grant_uses_principal_and_target_not_spawn_parent(
    tmp_path: Path,
) -> None:
    runtime, authority, _, _ = make_central(tmp_path, ("github.read",))
    outcome = await authorize(runtime, "agent:github_worker", "research", ("github.read",))
    assert outcome.status == "granted"
    assert outcome.grant is not None
    assert outcome.grant.delegator == "user:wang"
    assert outcome.grant.delegatee == "agent:github_worker"
    assert outcome.child_context is not None
    assert outcome.child_context.delegation == (outcome.grant,)
    assert outcome.grant.scopes == ("github.read",)
    assert all(grant.delegatee != "agent:planner" for grant in outcome.child_context.delegation)
    assert outcome.operation == "central_authorization"


# A19/A20/A21
async def test_sibling_authorities_are_independent_and_planner_does_not_gain_them(
    tmp_path: Path,
) -> None:
    approval = ApprovalGate(True)
    runtime, _, _, _ = make_central(
        tmp_path,
        ("agent.orchestrate", "github.read", "email.send"),
        approval=approval,
    )
    main = await authorize(runtime, "agent:planner", "research", ("agent.orchestrate",))
    github = await authorize(runtime, "agent:github_worker", "research", ("github.read",))
    email = await authorize(
        runtime, "agent:email_worker", "external_communication", ("email.send",)
    )
    assert main.status == github.status == email.status == "granted"
    assert scopes_of(runtime, "agent:github_worker") == frozenset({"github.read"})
    assert scopes_of(runtime, "agent:email_worker") == frozenset({"email.send"})
    assert scopes_of(runtime, "agent:planner") == frozenset({"agent.orchestrate"})
    assert "github.read" not in scopes_of(runtime, "agent:planner")
    assert "agent.orchestrate" not in scopes_of(runtime, "agent:github_worker")


# A22/A23/A24/A25
async def test_unmatched_route_and_reviewer_escalate_but_policy_deny_is_final(
    tmp_path: Path,
) -> None:
    approval = ApprovalGate(True)
    runtime, _, _, _ = make_central(tmp_path / "unmatched", ("email.send",), approval=approval)
    unmatched = await authorize(runtime, "agent:email_worker", "research", ("email.send",))
    assert unmatched.status == "granted"
    assert unmatched.required_gates == ("HITL",)

    denied_approval = ApprovalGate(False)
    denied_runtime, _, _, _ = make_central(
        tmp_path / "unmatched-deny", ("email.send",), approval=denied_approval
    )
    denied = await authorize(denied_runtime, "agent:email_worker", "research", ("email.send",))
    assert denied.status == "denied"
    assert denied.reason_codes == ("approval_denied",)
    assert denied.child_context is None

    reviewer = CapturingReviewer("DISAGREE")
    approval = ApprovalGate(True)
    runtime, _, _, _ = make_central(
        tmp_path / "review", ("rag.read",), reviewer=reviewer, approval=approval
    )
    reviewed = await authorize(runtime, "agent:rag_worker", "research", ("rag.read",))
    assert reviewed.status == "granted"
    assert reviewed.required_gates == ("HITL",)

    policy = PolicyAdapter("deny")
    approval = ApprovalGate(True)
    runtime, _, _, _ = make_central(
        tmp_path / "policy", ("github.write",), policy=policy, approval=approval
    )
    denied = await authorize(runtime, "agent:github_worker", "modify_data", ("github.write",))
    assert denied.status == "denied"
    assert denied.reason_codes == ("deny_test",)
    assert approval.requests == []

    policy = PolicyAdapter("allow")
    approval = ApprovalGate(True)
    runtime, _, _, _ = make_central(
        tmp_path / "both", ("credentials.read",), policy=policy, approval=approval
    )
    both = await authorize(
        runtime, "agent:credential_worker", "manage_credentials", ("credentials.read",)
    )
    assert both.status == "granted"
    assert both.required_gates == ("POLICY", "HITL")


# D01/D02/D07
async def test_explicit_delegation_requires_parent_capability_and_cannot_use_ceiling(
    tmp_path: Path,
) -> None:
    approval = ApprovalGate(True)
    runtime, _, _, _ = make_central(
        tmp_path, ("agent.orchestrate", "github.read", "email.send"), approval=approval
    )
    no_capability = await runtime.delegate_capability(
        tool_context(), delegation_request("agent:helper", "research", ("github.read",))
    )
    assert no_capability.status == "denied"
    assert no_capability.reason_codes == ("delegation_chain_missing",)

    worker = await authorize(runtime, "agent:github_worker", "research", ("github.read",))
    assert worker.status == "granted"
    escalation = await runtime.delegate_capability(
        tool_context("github_worker"),
        delegation_request("agent:issue_reader", "research", ("email.send",)),
    )
    assert escalation.status == "denied"
    assert escalation.reason_codes == ("scope_escalation",)

    # The same capability is reachable through central authorization, not delegation.
    central = await authorize(
        runtime, "agent:issue_reader", "external_communication", ("email.send",)
    )
    assert central.status == "granted"


# D03/D04/D05/D06/D08
async def test_explicit_attenuation_chain_cycles_expiry_and_distinct_operation(
    tmp_path: Path,
) -> None:
    expires = datetime.now(UTC) + timedelta(hours=1)
    approval = ApprovalGate(True)
    policy = PolicyAdapter("allow")
    runtime, authority, _, _ = make_central(
        tmp_path,
        ("github.read", "github.write"),
        expires_at=expires,
        policy=policy,
        approval=approval,
    )
    worker = await authorize(
        runtime, "agent:github_worker", "modify_data", ("github.read", "github.write")
    )
    assert worker.status == "granted"
    helper = await runtime.delegate_capability(
        tool_context("github_worker"),
        delegation_request("agent:issue_reader", "research", ("github.read",)),
    )
    assert helper.status == "granted"
    assert helper.operation == "explicit_delegation"
    assert helper.child_context is not None
    assert [grant.delegatee for grant in helper.child_context.delegation] == [
        "agent:github_worker",
        "agent:issue_reader",
    ]
    assert helper.grant is not None
    assert helper.grant.expires_at == expires
    assert effective_scopes(helper.child_context) == frozenset({"github.read"})

    # Returning to an ancestor is a cycle.
    cycle = await runtime.delegate_capability(
        tool_context("issue_reader"),
        delegation_request("agent:github_worker", "research", ("github.read",)),
    )
    assert cycle.status == "denied"
    assert cycle.reason_codes == ("delegation_cycle",)

    central = await authorize(runtime, "agent:github_worker", "research", ("github.read",))
    assert central.operation == "central_authorization"
    assert central.child_context is not None
    assert central.child_context.delegation[-1].delegator == "user:wang"


# R01-R06
async def test_run_authority_and_per_agent_contexts_survive_resume_without_re_review(
    tmp_path: Path,
) -> None:
    approval = ApprovalGate(True)
    runtime, authority, _, _ = make_central(
        tmp_path, ("agent.orchestrate", "github.read", "email.send"), approval=approval
    )
    assert (tmp_path / "run_authority.json").exists()
    main = await authorize(runtime, "agent:planner", "research", ("agent.orchestrate",))
    worker = await authorize(runtime, "agent:github_worker", "research", ("github.read",))
    helper = await runtime.delegate_capability(
        tool_context("github_worker"),
        delegation_request("agent:issue_reader", "research", ("github.read",)),
    )
    assert main.status == worker.status == helper.status == "granted"
    authority_hash = runtime.run_authority_hash()
    snapshot_hash = runtime.snapshot_hash()
    contexts = {
        actor: runtime.context_for(actor)
        for actor in ("agent:planner", "agent:github_worker", "agent:issue_reader")
    }

    reviewer = CapturingReviewer()
    policy = PolicyAdapter("allow")
    resumed, _, _, _ = make_central(
        tmp_path,
        ("agent.orchestrate", "github.read", "email.send"),
        reviewer=reviewer,
        policy=policy,
    )
    assert resumed.run_authority_hash() == authority_hash
    assert resumed.snapshot_hash() == snapshot_hash
    assert reviewer.inputs == []
    assert policy.requests == []
    for actor, context in contexts.items():
        assert resumed.context_for(actor) == context


# R07
async def test_crash_before_central_commit_creates_no_capability(tmp_path: Path) -> None:
    from atlasos.durability import InjectedCrash

    class CrashingGate:
        async def request_approval(self, request: ApprovalRequest) -> ApprovalResponse:
            del request
            raise InjectedCrash("crash before central commit")

    runtime, _, _, _ = make_central(
        tmp_path,
        ("email.send",),
        approval=CrashingGate(),  # type: ignore[arg-type]
    )
    with pytest.raises(InjectedCrash):
        await authorize(runtime, "agent:email_worker", "external_communication", ("email.send",))
    resumed, _, _, _ = make_central(tmp_path, ("email.send",))
    assert resumed.context_for("agent:email_worker") is None


# R08/R09
async def test_crash_after_central_commit_restores_exact_context(tmp_path: Path) -> None:
    approval = ApprovalGate(True)
    runtime, authority, _, _ = make_central(tmp_path, ("github.read",), approval=approval)
    outcome = await authorize(runtime, "agent:github_worker", "research", ("github.read",))
    assert outcome.status == "granted"
    resumed, _, _, _ = make_central(tmp_path, ("github.read",))
    restored = resumed.context_for("agent:github_worker")
    assert restored == outcome.child_context
    assert restored is not None
    assert restored.delegation[0].delegator == "user:wang"
    assert [grant.delegatee for grant in restored.delegation] == ["agent:github_worker"]


# E2E A/B/C/D/E
async def test_low_privilege_planner_and_independent_specialists(tmp_path: Path) -> None:
    approval = ApprovalGate(True)
    runtime, _, _, _ = make_central(
        tmp_path,
        ("agent.orchestrate", "github.read", "email.send", "database.read"),
        approval=approval,
    )
    planner = await authorize(runtime, "agent:planner", "research", ("agent.orchestrate",))
    assert planner.status == "granted"
    github = await authorize(runtime, "agent:github_worker", "research", ("github.read",))
    email = await authorize(
        runtime, "agent:email_worker", "external_communication", ("email.send",)
    )
    database = await authorize(runtime, "agent:db_worker", "read_data", ("database.read",))
    assert all(outcome.status == "granted" for outcome in (planner, github, email, database))
    assert scopes_of(runtime, "agent:planner") == frozenset({"agent.orchestrate"})
    assert scopes_of(runtime, "agent:github_worker") == frozenset({"github.read"})
    assert scopes_of(runtime, "agent:email_worker") == frozenset({"email.send"})


async def test_unknown_route_and_explicit_attenuation_e2e(tmp_path: Path) -> None:
    approval = ApprovalGate(True)
    runtime, _, _, _ = make_central(
        tmp_path,
        ("github.read", "github.write", "email.send"),
        policy=PolicyAdapter("allow"),
        approval=approval,
    )
    unknown = await authorize(runtime, "agent:email_worker", "research", ("email.send",))
    assert unknown.status == "granted"
    assert unknown.required_gates == ("HITL",)
    worker = await authorize(
        runtime, "agent:github_worker", "modify_data", ("github.read", "github.write")
    )
    assert worker.status == "granted"
    helper = await runtime.delegate_capability(
        tool_context("github_worker"),
        delegation_request("agent:issue_reader", "research", ("github.read",)),
    )
    assert helper.status == "granted"
    assert helper.child_context is not None
    assert helper.child_context.delegation[0].delegatee == "agent:github_worker"
    assert helper.child_context.delegation[-1].delegatee == "agent:issue_reader"
    assert effective_scopes(helper.child_context) == frozenset({"github.read"})
    assert "github.write" not in effective_scopes(helper.child_context)
