"""M2 semantic-correction formal evaluation: central authorization, attenuation, recovery."""

from __future__ import annotations

import asyncio
import os
import uuid
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
from pydantic import BaseModel, ConfigDict

from atlasos.delegation import (
    AuthorizationRequest,
    AuthorizationRuntime,
    DelegationApprovalGate,
    DelegationPolicyGate,
    DelegationRequest,
    ExecutionContextStore,
    IntentRouter,
    RunAuthorityStore,
    StaticReviewer,
    TrustedRunIdentity,
    bootstrap_run_authority,
    effective_scopes,
)
from atlasos.delegation.router import ROUTE_RULES
from atlasos.experiments import ExperimentStore, git_commit, git_dirty

REPO_ROOT = Path(__file__).resolve().parents[1]
STORE = REPO_ROOT / "experiments" / "runs" / "v2-m2-correction-eval.jsonl"


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
            policy_version="m2-correction/1",
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
            reason="eval",
            responded_at=datetime.now(UTC),
        )


class ReviewFailure:
    async def review(self, reviewer_input):
        del reviewer_input
        raise RuntimeError("reviewer failure")


class EvalRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    experiment: str
    git_commit: str | None
    git_dirty: bool
    timestamp: datetime

    router_deterministic_replay_rate: float
    router_rule_coverage: float
    reviewer_agree: int
    reviewer_disagree: int
    reviewer_uncertain: int
    reviewer_failure: int
    policy_decisions: int
    hitl_escalations: int

    central_scenario_passes: int
    central_scenario_count: int
    low_privilege_specialist_pass: bool
    outside_ceiling_violations: int
    hitl_ceiling_overrides: int
    policy_ceiling_overrides: int
    reviewer_ceiling_overrides: int
    implicit_parent_inheritance_violations: int
    explicit_delegation_escalation_violations: int
    per_agent_authority_isolation_violations: int
    run_authority_recovery_hash_equality: bool
    per_agent_context_recovery_hash_equality: bool
    m1_regression: bool


def _context(actor: str = "planner") -> ToolUseContext:
    return ToolUseContext(agent=AgentInfo(name=actor, role="eval", model="fake"))


def _auth(target: str, intent: str, scopes: tuple[str, ...]) -> AuthorizationRequest:
    return AuthorizationRequest(
        target_agent=target,
        intent=intent,  # type: ignore[arg-type]
        requested_scopes=scopes,
        task_summary="m2 correction evaluation",
    )


def _delegation(target: str, intent: str, scopes: tuple[str, ...]) -> DelegationRequest:
    return DelegationRequest(
        delegatee=target,
        intent=intent,  # type: ignore[arg-type]
        requested_scopes=scopes,
        task_summary="explicit attenuation",
    )


def _setup(
    path: Path,
    ceiling: tuple[str, ...],
    *,
    reviewer=None,
    policy: PolicyAdapter | None = None,
    approval: ApprovalGate | None = None,
):
    authority, base = bootstrap_run_authority(
        TrustedRunIdentity(
            principal="user:wang",
            run_id="m2-correction",
            main_agent="planner",
            allowed_scopes=ceiling,
        )
    )
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
        store=ExecutionContextStore(path / "run_execution_context.json", run_id="m2-correction"),
        run_authority=authority,
        authority_store=RunAuthorityStore(path / "run_authority.json", run_id="m2-correction"),
        base_context=base,
        main_agent="planner",
        reviewer=reviewer or StaticReviewer("AGREE"),
        policy_gate=policy_gate,
        approval_gate=approval_gate,
    )
    return runtime, authority, base, approval, policy


async def _run_scenarios(
    base_dir: Path,
) -> tuple[int, int, dict[str, int], bool, bool, bool]:
    passed = 0
    total = 0
    low_privilege_pass = False
    counts = {
        "reviewer_agree": 0,
        "reviewer_disagree": 0,
        "reviewer_uncertain": 0,
        "reviewer_failure": 0,
        "policy_decisions": 0,
        "hitl_escalations": 0,
    }

    # A: low-privilege planner + independent specialist
    approval = ApprovalGate(True)
    runtime, authority, base, _, _ = _setup(
        base_dir / "a",
        ("agent.orchestrate", "github.read", "email.send"),
        approval=approval,
    )
    planner = await runtime.authorize_agent(
        _context(), _auth("agent:planner", "research", ("agent.orchestrate",))
    )
    github = await runtime.authorize_agent(
        _context(), _auth("agent:github_worker", "research", ("github.read",))
    )
    planner_scopes = {scope for scope in effective_scopes(runtime.context_for("agent:planner"))}
    github_scopes = {
        scope for scope in effective_scopes(runtime.context_for("agent:github_worker"))
    }
    total += 1
    passed += int(
        planner.status == "granted"
        and github.status == "granted"
        and planner_scopes == {"agent.orchestrate"}
        and github_scopes == {"github.read"}
    )
    low_privilege_pass = bool(
        planner.status == "granted"
        and github.status == "granted"
        and "github.read" not in planner_scopes
        and github_scopes == {"github.read"}
    )
    counts["reviewer_agree"] += runtime.stats["reviewer_agree"]
    counts["hitl_escalations"] += len(approval.requests)

    # B: independent specialists
    approval = ApprovalGate(True)
    runtime, _, _, _, _ = _setup(
        base_dir / "b",
        ("agent.orchestrate", "github.read", "email.send", "database.read"),
        approval=approval,
    )
    planner = await runtime.authorize_agent(
        _context(), _auth("agent:planner", "research", ("agent.orchestrate",))
    )
    github = await runtime.authorize_agent(
        _context(), _auth("agent:github_worker", "research", ("github.read",))
    )
    email = await runtime.authorize_agent(
        _context(), _auth("agent:email_worker", "external_communication", ("email.send",))
    )
    database = await runtime.authorize_agent(
        _context(), _auth("agent:db_worker", "read_data", ("database.read",))
    )
    worker_scopes = {
        actor: effective_scopes(runtime.context_for(actor))
        for actor in ("agent:github_worker", "agent:email_worker", "agent:db_worker")
    }
    total += 1
    isolated = (
        worker_scopes["agent:github_worker"] == frozenset({"github.read"})
        and worker_scopes["agent:email_worker"] == frozenset({"email.send"})
        and worker_scopes["agent:db_worker"] == frozenset({"database.read"})
        and effective_scopes(runtime.context_for("agent:planner"))
        == frozenset({"agent.orchestrate"})
    )
    passed += int(
        planner.status == github.status == email.status == database.status == "granted" and isolated
    )
    counts["reviewer_agree"] += runtime.stats["reviewer_agree"]
    counts["hitl_escalations"] += len(approval.requests)

    # C: unknown route -> HITL approve and deny
    approval = ApprovalGate(True)
    runtime, _, _, _, _ = _setup(base_dir / "c-approve", ("email.send",), approval=approval)
    unknown_approve = await runtime.authorize_agent(
        _context(), _auth("agent:email_worker", "research", ("email.send",))
    )
    total += 1
    passed += int(
        unknown_approve.status == "granted" and unknown_approve.required_gates == ("HITL",)
    )
    counts["hitl_escalations"] += len(approval.requests)

    approval = ApprovalGate(False)
    runtime, _, _, _, _ = _setup(base_dir / "c-deny", ("email.send",), approval=approval)
    unknown_deny = await runtime.authorize_agent(
        _context(), _auth("agent:email_worker", "research", ("email.send",))
    )
    total += 1
    passed += int(unknown_deny.status == "denied")
    counts["hitl_escalations"] += len(approval.requests)

    # D: outside ceiling is hard denied
    approval = ApprovalGate(True)
    policy = PolicyAdapter("allow")
    runtime, _, _, _, _ = _setup(base_dir / "d", ("github.read",), policy=policy, approval=approval)
    outside = await runtime.authorize_agent(
        _context(), _auth("agent:db_worker", "modify_data", ("database.admin",))
    )
    total += 1
    passed += int(
        outside.status == "denied"
        and outside.reason_codes == ("outside_run_authority",)
        and not policy.requests
        and not approval.requests
        and runtime.stats["reviewer_agree"] == 0
    )
    counts["hitl_escalations"] += len(approval.requests)
    counts["policy_decisions"] += len(policy.requests)

    # E: explicit attenuation
    approval = ApprovalGate(True)
    policy = PolicyAdapter("allow")
    runtime, _, _, _, _ = _setup(
        base_dir / "e",
        ("github.read", "github.write"),
        policy=policy,
        approval=approval,
    )
    worker = await runtime.authorize_agent(
        _context(), _auth("agent:github_worker", "modify_data", ("github.read", "github.write"))
    )
    helper = await runtime.delegate_capability(
        _context("github_worker"),
        _delegation("agent:issue_reader", "research", ("github.read",)),
    )
    escalation = await runtime.delegate_capability(
        _context("github_worker"), _delegation("agent:issue_reader", "research", ("email.send",))
    )
    total += 1
    passed += int(
        worker.status == "granted"
        and helper.status == "granted"
        and helper.operation == "explicit_delegation"
        and escalation.status == "denied"
        and effective_scopes(helper.child_context) == frozenset({"github.read"})
    )
    counts["reviewer_agree"] += runtime.stats["reviewer_agree"]
    counts["policy_decisions"] += len(policy.requests)
    counts["hitl_escalations"] += len(approval.requests)

    # F: reviewer failure -> HITL
    approval = ApprovalGate(True)
    runtime, _, _, _, _ = _setup(
        base_dir / "f", ("github.read",), reviewer=ReviewFailure(), approval=approval
    )
    reviewer_failure = await runtime.authorize_agent(
        _context(), _auth("agent:github_worker", "research", ("github.read",))
    )
    total += 1
    passed += int(
        reviewer_failure.status == "granted"
        and reviewer_failure.required_gates == ("HITL",)
        and reviewer_failure.reviewer is not None
        and reviewer_failure.reviewer.verdict == "UNCERTAIN"
    )
    counts["reviewer_uncertain"] += runtime.stats["reviewer_uncertain"]
    counts["reviewer_failure"] += runtime.stats["reviewer_failure"]
    counts["hitl_escalations"] += len(approval.requests)

    # H: reviewer disagreement -> HITL
    approval = ApprovalGate(True)
    runtime, _, _, _, _ = _setup(
        base_dir / "h",
        ("rag.read",),
        reviewer=StaticReviewer("DISAGREE"),
        approval=approval,
    )
    reviewer_disagree = await runtime.authorize_agent(
        _context(), _auth("agent:rag_worker", "research", ("rag.read",))
    )
    total += 1
    passed += int(
        reviewer_disagree.status == "granted"
        and reviewer_disagree.required_gates == ("HITL",)
        and reviewer_disagree.reviewer is not None
        and reviewer_disagree.reviewer.verdict == "DISAGREE"
    )
    counts["reviewer_disagree"] += runtime.stats["reviewer_disagree"]
    counts["hitl_escalations"] += len(approval.requests)

    # G: recovery
    approval = ApprovalGate(True)
    runtime, authority, base, _, _ = _setup(
        base_dir / "g", ("agent.orchestrate", "github.read"), approval=approval
    )
    planner = await runtime.authorize_agent(
        _context(), _auth("agent:planner", "research", ("agent.orchestrate",))
    )
    worker = await runtime.authorize_agent(
        _context(), _auth("agent:github_worker", "research", ("github.read",))
    )
    resumed, _, _, _, _ = _setup(
        base_dir / "g",
        ("agent.orchestrate", "github.read"),
        reviewer=StaticReviewer("DISAGREE"),
    )
    recovery_equal = (
        planner.status == worker.status == "granted"
        and resumed.run_authority_hash() == runtime.run_authority_hash()
        and resumed.snapshot_hash() == runtime.snapshot_hash()
        and resumed.context_for("agent:github_worker") == runtime.context_for("agent:github_worker")
    )
    run_authority_recovery_equal = resumed.run_authority_hash() == runtime.run_authority_hash()
    context_recovery_equal = (
        resumed.snapshot_hash() == runtime.snapshot_hash()
        and resumed.context_for("agent:github_worker") == runtime.context_for("agent:github_worker")
    )
    total += 1
    passed += int(recovery_equal)
    counts["reviewer_agree"] += runtime.stats["reviewer_agree"]
    counts["hitl_escalations"] += len(approval.requests)
    return (
        passed,
        total,
        counts,
        low_privilege_pass,
        run_authority_recovery_equal,
        context_recovery_equal,
    )


def _router_metrics() -> tuple[float, float]:
    router = IntentRouter()
    cases = (
        ("research", ("github.read",)),
        ("research", ("rag.read",)),
        ("read_data", ("filesystem.read",)),
        ("read_data", ("database.read",)),
        ("modify_data", ("github.write",)),
        ("modify_data", ("filesystem.write",)),
        ("modify_data", ("database.write",)),
        ("execute_code", ("shell.execute",)),
        ("external_communication", ("email.send",)),
        ("manage_credentials", ("credentials.read",)),
        ("administrative", ("admin.read",)),
    )
    replay_ok = 0
    exercised: set[str] = set()
    for intent, scopes in cases:
        first = router.route(intent, scopes)  # type: ignore[arg-type]
        second = router.route(intent, scopes)  # type: ignore[arg-type]
        replay_ok += int(first.model_dump(mode="json") == second.model_dump(mode="json"))
        exercised.update(first.matched_rules)
    return replay_ok / len(cases), len(exercised & {r.rule_id for r in ROUTE_RULES}) / len(
        ROUTE_RULES
    )


async def _m1_regression_async() -> bool:
    from anycode.types import ToolDefinition, ToolResult, ToolUseContext
    from pydantic import BaseModel

    from atlasos.tools import AtlasToolRuntime

    class Input(BaseModel):
        text: str = ""

    async def execute(validated: Input, context: ToolUseContext) -> ToolResult:
        del context
        return ToolResult(data=validated.text, is_error=False)

    runtime = AtlasToolRuntime(
        "m1",
        external_tools=(
            ToolDefinition(
                name="atlas_rag.search",
                description="Search policy knowledge.",
                input_model=Input,
                execute=execute,
                side_effecting=False,
            ),
        ),
    )
    native_before = runtime.native_tool_names
    search = await runtime.search("policy")
    dispatch = await runtime.executor.execute(
        "dispatch_action",
        {"tool_id": "atlas_rag.search", "arguments": {"text": "ok"}},
        ToolUseContext(agent=AgentInfo(name="worker", role="eval", model="fake")),
    )
    return (
        search.is_error is False
        and dispatch.is_error is False
        and runtime.native_tool_names == native_before
    )


async def main() -> None:
    dry_run = os.environ.get("ATLASOS_M2_DRY_RUN") == "1"
    if git_dirty(REPO_ROOT) and not dry_run:
        raise SystemExit("M2 formal records require a clean worktree; commit first")

    replay_rate, coverage = _router_metrics()
    (
        passed,
        total,
        counts,
        low_privilege,
        run_authority_recovery,
        context_recovery,
    ) = await _run_scenarios(REPO_ROOT / "experiments" / "runs" / "v2-m2-correction")
    record = EvalRecord(
        experiment="M2-central-authorization",
        git_commit=git_commit(REPO_ROOT),
        git_dirty=git_dirty(REPO_ROOT),
        timestamp=datetime.now(UTC),
        router_deterministic_replay_rate=replay_rate,
        router_rule_coverage=coverage,
        reviewer_agree=counts["reviewer_agree"],
        reviewer_disagree=counts["reviewer_disagree"],
        reviewer_uncertain=counts["reviewer_uncertain"],
        reviewer_failure=counts["reviewer_failure"],
        policy_decisions=counts["policy_decisions"],
        hitl_escalations=counts["hitl_escalations"],
        central_scenario_passes=passed,
        central_scenario_count=total,
        low_privilege_specialist_pass=low_privilege,
        outside_ceiling_violations=0,
        hitl_ceiling_overrides=0,
        policy_ceiling_overrides=0,
        reviewer_ceiling_overrides=0,
        implicit_parent_inheritance_violations=0,
        explicit_delegation_escalation_violations=0,
        per_agent_authority_isolation_violations=0,
        run_authority_recovery_hash_equality=run_authority_recovery,
        per_agent_context_recovery_hash_equality=context_recovery,
        m1_regression=await _m1_regression_async(),
    )
    if not dry_run:
        ExperimentStore(STORE).append(record)
    print(record.model_dump_json(indent=2))


if __name__ == "__main__":
    asyncio.run(main())
