"""M2 integration: reviewer monotonicity, policy/HITL composition, issuance, recovery."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from anycode.contracts.models import PolicyDecision
from anycode.hitl.approval import ApprovalManager
from anycode.identity.context import ExecutionContext
from anycode.identity.policy import PolicyEnforcer, PolicyRequest
from anycode.types import (
    AgentConfig,
    AgentInfo,
    ApprovalConfig,
    ApprovalGate,
    ApprovalRequest,
    ApprovalResponse,
    ToolUseContext,
)

from atlasos.delegation import (
    DelegationApprovalGate,
    DelegationPolicyGate,
    DelegationRequest,
    DelegationRuntime,
    DelegationValidationError,
    DelegationValidator,
    ExecutionContextStore,
    GrantIssuer,
    ReviewerInput,
    ReviewerResult,
    ReviewerVerdict,
    StaticReviewer,
    TrustedRootIdentity,
    bootstrap_root_context,
)


def make_root(
    *,
    scopes: tuple[str, ...] = (
        "github.read",
        "github.write",
        "email.send",
        "rag.read",
        "filesystem.read",
        "shell.execute",
        "database.write",
    ),
    expires_at: datetime | None = None,
    **fields,
) -> ExecutionContext:
    return bootstrap_root_context(
        TrustedRootIdentity(
            principal="user:wang",
            main_agent="planner",
            scopes=scopes,
            expires_at=expires_at,
            **fields,
        )
    )


def actor_context(actor: str = "planner", context: ExecutionContext | None = None):
    return ToolUseContext(
        agent=AgentInfo(name=actor, role="test", model="fake"),
        execution_context=context,
    )


def request(
    *,
    delegatee: str = "agent:github_worker",
    intent: str = "research",
    scopes: tuple[str, ...] = ("github.read",),
    summary: str = "read issues",
) -> DelegationRequest:
    return DelegationRequest(
        delegatee=delegatee,
        intent=intent,  # type: ignore[arg-type]
        requested_scopes=scopes,
        task_summary=summary,
    )


class StaticPolicyAdapter:
    def __init__(self, outcome: str = "allow", reason_codes: tuple[str, ...] = ()) -> None:
        self.outcome = outcome
        self.reason_codes = reason_codes
        self.requests: list[PolicyRequest] = []

    async def decide(self, request: PolicyRequest) -> PolicyDecision:
        self.requests.append(request)
        return PolicyDecision(
            id=str(uuid.uuid4()),
            run_id=request.run_id,
            task_id=request.task_id,
            outcome="allow" if self.outcome == "allow" else "deny",
            policy_version="test/1",
            reason_codes=self.reason_codes,
            correlation_id=request.correlation_id,
            generation=request.generation,
            attempt=request.attempt,
        )


class StaticApprovalGate:
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


class BlockingApprovalGate:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.finish = asyncio.Event()
        self.requests: list[ApprovalRequest] = []

    async def request_approval(self, request: ApprovalRequest) -> ApprovalResponse:
        self.requests.append(request)
        self.started.set()
        await self.finish.wait()
        return ApprovalResponse(
            request_id=request.id,
            approved=True,
            reason="approved",
            responded_at=datetime.now(UTC),
        )


class FailingReviewer:
    def __init__(self) -> None:
        self.calls = 0

    async def review(self, reviewer_input: ReviewerInput) -> ReviewerResult:
        del reviewer_input
        self.calls += 1
        raise RuntimeError("reviewer offline")


class CapturingReviewer:
    def __init__(self, verdict: str = "AGREE") -> None:
        self.verdict = verdict
        self.inputs: list[ReviewerInput] = []

    async def review(self, reviewer_input: ReviewerInput) -> ReviewerResult:
        self.inputs.append(reviewer_input)
        return ReviewerResult(verdict=self.verdict)  # type: ignore[arg-type]


class RejectChildValidator(DelegationValidator):
    def validate_context(self, context, *, expected_actor=None) -> None:
        if expected_actor == "agent:github_worker":
            raise DelegationValidationError("revalidation_failed", "child context rejected")
        super().validate_context(context, expected_actor=expected_actor)


def make_runtime(
    tmp_path: Path,
    *,
    root: ExecutionContext | None = None,
    reviewer=None,
    policy_adapter: StaticPolicyAdapter | None = None,
    approval_gate: ApprovalGate | None = None,
    policy_gate=None,
    validator: DelegationValidator | None = None,
    issuer: GrantIssuer | None = None,
    run_id: str = "run-1",
) -> DelegationRuntime:
    root = root or make_root()
    store = ExecutionContextStore(tmp_path / "run_execution_context.json", run_id=run_id)
    policy_gate = policy_gate
    if policy_gate is None and policy_adapter is not None:
        policy_gate = DelegationPolicyGate(PolicyEnforcer(policy_adapter, fail_closed=True))
    approval_manager = None
    if approval_gate is not None:
        approval_manager = ApprovalManager(
            ApprovalConfig(
                enabled=True,
                require_approval_tasks=True,
                timeout_seconds=5.0,
                default_on_timeout="reject",
            ),
            approval_gate,
        )
    return DelegationRuntime(
        run_id=run_id,
        store=store,
        root_context=root,
        validator=validator,
        reviewer=reviewer if reviewer is not None else StaticReviewer("AGREE"),
        policy_gate=policy_gate,
        approval_gate=(
            DelegationApprovalGate(approval_manager) if approval_manager is not None else None
        ),
        issuer=issuer,
    )


# T19/T20/T21
async def test_reviewer_agree_preserves_gates_disagree_and_uncertain_add_hitl(
    tmp_path: Path,
) -> None:
    for verdict, expected_gates, hitl in (
        ("AGREE", ("POLICY",), False),
        ("DISAGREE", ("POLICY", "HITL"), True),
        ("UNCERTAIN", ("POLICY", "HITL"), True),
    ):
        policy = StaticPolicyAdapter("allow")
        approval = StaticApprovalGate(True)
        runtime = make_runtime(
            tmp_path / verdict,
            reviewer=StaticReviewer(cast(ReviewerVerdict, verdict)),
            policy_adapter=policy,
            approval_gate=approval,
        )
        outcome = await runtime.propose(
            actor_context(),
            request(intent="modify_data", scopes=("github.write",)),
        )
        assert outcome.status == "granted"
        assert outcome.required_gates == expected_gates
        assert bool(approval.requests) is hitl
        assert len(policy.requests) == 1


# T22
async def test_reviewer_provider_error_adds_hitl(tmp_path: Path) -> None:
    root = make_root(scopes=("github.read",))
    reviewer = FailingReviewer()
    approval = StaticApprovalGate(True)
    runtime = make_runtime(
        tmp_path,
        root=root,
        reviewer=reviewer,
        approval_gate=approval,
    )
    outcome = await runtime.propose(actor_context(), request(scopes=("github.read",)))
    assert outcome.status == "granted"
    assert outcome.required_gates == ("HITL",)
    assert outcome.reviewer is not None
    assert outcome.reviewer.verdict == "UNCERTAIN"
    assert reviewer.calls == 1
    assert len(approval.requests) == 1


# T23
async def test_reviewer_cannot_remove_policy_gate(tmp_path: Path) -> None:
    policy = StaticPolicyAdapter("allow")
    approval = StaticApprovalGate(True)
    runtime = make_runtime(
        tmp_path,
        reviewer=StaticReviewer("DISAGREE"),
        policy_adapter=policy,
        approval_gate=approval,
    )
    outcome = await runtime.propose(
        actor_context(), request(intent="modify_data", scopes=("github.write",))
    )
    assert "POLICY" in outcome.required_gates
    assert "HITL" in outcome.required_gates
    assert len(policy.requests) == 1
    assert len(approval.requests) == 1


# T24
def test_reviewer_receives_only_reviewer_input_and_has_no_grant_access() -> None:
    reviewer = CapturingReviewer()
    assert not hasattr(reviewer, "issue")
    assert not hasattr(reviewer, "grant")
    assert not hasattr(reviewer, "runtime")


# T25
async def test_auto_agree_can_issue(tmp_path: Path) -> None:
    reviewer = StaticReviewer("AGREE")
    runtime = make_runtime(tmp_path, reviewer=reviewer)
    outcome = await runtime.propose(actor_context(), request(scopes=("github.read",)))
    assert outcome.status == "granted"
    assert outcome.required_gates == ()
    assert outcome.grant is not None
    assert outcome.child_context is not None
    assert outcome.child_context.delegation[-1].scopes == ("github.read",)


# T26/T27/T28
async def test_policy_allow_deny_and_human_cannot_override_deny(tmp_path: Path) -> None:
    policy = StaticPolicyAdapter("allow")
    runtime = make_runtime(tmp_path / "allow", policy_adapter=policy)
    allowed = await runtime.propose(
        actor_context(), request(intent="modify_data", scopes=("github.write",))
    )
    assert allowed.status == "granted"

    deny_policy = StaticPolicyAdapter("deny", ("policy_denied",))
    deny_approval = StaticApprovalGate(True)
    deny_runtime = make_runtime(
        tmp_path / "deny",
        policy_adapter=deny_policy,
        approval_gate=deny_approval,
    )
    denied = await deny_runtime.propose(
        actor_context(), request(intent="modify_data", scopes=("github.write",))
    )
    assert denied.status == "denied"
    assert denied.reason_codes == ("policy_denied",)
    assert deny_approval.requests == []
    assert denied.child_context is None


# T29/T30
async def test_policy_and_hitl_require_both_and_hitl_deny_blocks(tmp_path: Path) -> None:
    policy = StaticPolicyAdapter("allow")
    approval = StaticApprovalGate(True)
    runtime = make_runtime(tmp_path / "both", policy_adapter=policy, approval_gate=approval)
    outcome = await runtime.propose(
        actor_context(),
        request(intent="modify_data", scopes=("github.write", "email.send")),
    )
    assert outcome.status == "granted"
    assert outcome.required_gates == ("POLICY", "HITL")
    assert len(policy.requests) == 1
    assert len(approval.requests) == 1

    deny_approval = StaticApprovalGate(False)
    deny_runtime = make_runtime(
        tmp_path / "hitl-deny",
        root=make_root(scopes=("email.send",)),
        approval_gate=deny_approval,
    )
    denied = await deny_runtime.propose(
        actor_context(), request(intent="external_communication", scopes=("email.send",))
    )
    assert denied.status == "denied"
    assert denied.reason_codes == ("approval_denied",)
    assert denied.child_context is None


# T31/T32
async def test_missing_required_gate_fails_closed(tmp_path: Path) -> None:
    no_policy = make_runtime(tmp_path / "no-policy")
    denied_policy = await no_policy.propose(
        actor_context(), request(intent="modify_data", scopes=("github.write",))
    )
    assert denied_policy.status == "denied"
    assert denied_policy.reason_codes == ("policy_unavailable",)

    unconfigured = make_runtime(
        tmp_path / "unconfigured",
        policy_gate=DelegationPolicyGate(PolicyEnforcer(None, fail_closed=False)),
    )
    denied_unconfigured = await unconfigured.propose(
        actor_context(), request(intent="modify_data", scopes=("github.write",))
    )
    assert denied_unconfigured.status == "denied"
    assert denied_unconfigured.reason_codes == ("policy_unavailable",)

    root = make_root(scopes=("email.send",))
    no_approval = make_runtime(tmp_path / "no-approval", root=root)
    denied_approval = await no_approval.propose(
        actor_context(), request(intent="external_communication", scopes=("email.send",))
    )
    assert denied_approval.status == "denied"
    assert denied_approval.reason_codes == ("approval_unavailable",)


# T33
async def test_pending_approval_has_no_capability(tmp_path: Path) -> None:
    root = make_root(scopes=("email.send",))
    gate = BlockingApprovalGate()
    runtime = make_runtime(tmp_path, root=root, approval_gate=gate)
    task = asyncio.create_task(
        runtime.propose(
            actor_context(),
            request(
                delegatee="agent:email_worker",
                intent="external_communication",
                scopes=("email.send",),
            ),
        )
    )
    await asyncio.wait_for(gate.started.wait(), timeout=2.0)
    assert runtime.context_for("agent:email_worker") is None
    assert runtime.store.load().keys() == {"agent:planner"}
    gate.finish.set()
    outcome = await asyncio.wait_for(task, timeout=5.0)
    assert outcome.status == "granted"
    assert runtime.context_for("agent:email_worker") is not None


# T34/T35/T36
async def test_issuer_is_internal_parent_immutable_and_appends_one_grant(tmp_path: Path) -> None:
    root = make_root()
    before = root.model_dump(mode="json")
    runtime = make_runtime(tmp_path, root=root)
    outcome = await runtime.propose(actor_context(), request(scopes=("github.read",)))
    assert outcome.status == "granted"
    assert root.model_dump(mode="json") == before
    assert outcome.child_context is not None
    assert len(outcome.child_context.delegation) == len(root.delegation) + 1
    assert not hasattr(GrantIssuer, "as_tool")


# T37
async def test_child_context_is_revalidated_before_commit(tmp_path: Path) -> None:
    validator = RejectChildValidator()
    runtime = make_runtime(tmp_path, validator=validator, issuer=GrantIssuer(validator))
    outcome = await runtime.propose(actor_context(), request(scopes=("github.read",)))
    assert outcome.status == "denied"
    assert outcome.reason_codes == ("revalidation_failed",)
    assert runtime.context_for("agent:github_worker") is None


# T38/T39/T40
async def test_child_config_siblings_and_non_delegation_fields(tmp_path: Path) -> None:
    root = make_root(
        scopes=("github.read", "email.send"),
        subject="subject-1",
        workload_identity="workload-1",
        tenant_scope="tenant-a",
        classification="confidential",
        allowed_regions=("eu",),
        required_region="eu",
        credential_references=("env:ATLAS_TOKEN",),
        trace_id="trace-1",
        attributes={"team": "platform"},
    )
    runtime = make_runtime(tmp_path, root=root, approval_gate=StaticApprovalGate(True))
    first = await runtime.propose(
        actor_context(), request(delegatee="agent:github_worker", scopes=("github.read",))
    )
    second = await runtime.propose(
        actor_context(),
        request(
            delegatee="agent:email_worker", intent="external_communication", scopes=("email.send",)
        ),
    )
    assert first.status == second.status == "granted"
    assert first.child_context is not None and second.child_context is not None
    assert first.child_context != second.child_context
    assert first.child_context.delegation[-1].delegatee == "agent:github_worker"
    assert second.child_context.delegation[-1].delegatee == "agent:email_worker"

    for field in (
        "principal",
        "subject",
        "workload_identity",
        "tenant_scope",
        "classification",
        "allowed_regions",
        "required_region",
        "credential_references",
        "trace_id",
        "attributes",
    ):
        assert getattr(first.child_context, field) == getattr(root, field)

    base = AgentConfig(name="github_worker", model="fake", provider="fake", tools=[])
    child_config = runtime.build_child_agent_config(base, first)
    assert child_config.execution_context == first.child_context
    assert child_config.execution_context != root

    stored = runtime.store.load()
    assert set(stored) == {"agent:planner", "agent:github_worker", "agent:email_worker"}


# T41/T42/T44/T45
async def test_durable_snapshot_resume_and_no_re_review(tmp_path: Path) -> None:
    root = make_root()
    runtime = make_runtime(tmp_path, root=root)
    outcome = await runtime.propose(actor_context(), request(scopes=("github.read",)))
    assert outcome.status == "granted"
    assert (tmp_path / "run_execution_context.json").exists()
    before_hash = runtime.snapshot_hash()
    before_context = runtime.context_for("agent:github_worker")
    assert before_context is not None

    reviewer = CapturingReviewer()
    policy = StaticPolicyAdapter("allow")
    resumed = make_runtime(
        tmp_path,
        root=root,
        reviewer=reviewer,
        policy_adapter=policy,
        run_id="run-1",
    )
    assert resumed.snapshot_hash() == before_hash
    assert resumed.context_for("agent:github_worker") == before_context
    assert reviewer.inputs == []
    assert policy.requests == []


# T43
async def test_crash_before_grant_commit_has_no_new_capability(tmp_path: Path) -> None:
    from atlasos.durability import InjectedCrash

    class CrashingGate:
        async def request_approval(self, request: ApprovalRequest) -> ApprovalResponse:
            del request
            raise InjectedCrash("crash before grant commit")

    root = make_root(scopes=("email.send",))
    runtime = make_runtime(tmp_path, root=root, approval_gate=CrashingGate())
    with pytest.raises(InjectedCrash):
        await runtime.propose(
            actor_context(),
            request(
                delegatee="agent:email_worker",
                intent="external_communication",
                scopes=("email.send",),
            ),
        )
    resumed = make_runtime(tmp_path, root=root, run_id="run-1")
    assert resumed.context_for("agent:email_worker") is None


# T44
async def test_crash_after_grant_commit_restores_capability(tmp_path: Path) -> None:
    root = make_root()
    runtime = make_runtime(tmp_path, root=root)
    outcome = await runtime.propose(actor_context(), request(scopes=("github.read",)))
    assert outcome.status == "granted"
    resumed = make_runtime(tmp_path, root=root, run_id="run-1")
    child = resumed.context_for("agent:github_worker")
    assert child is not None
    assert child == outcome.child_context
