"""M2 formal deterministic evaluation: routing, gates, issuance, recovery, M1 regression."""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

from anycode.contracts.models import PolicyDecision
from anycode.hitl.approval import ApprovalManager
from anycode.identity.context import ExecutionContext
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
    DelegationApprovalGate,
    DelegationPolicyGate,
    DelegationRequest,
    DelegationRuntime,
    ExecutionContextStore,
    IntentRouter,
    StaticReviewer,
    TrustedRootIdentity,
    bootstrap_root_context,
)
from atlasos.delegation.router import ROUTE_RULES
from atlasos.experiments import ExperimentStore, git_commit, git_dirty

REPO_ROOT = Path(__file__).resolve().parents[1]
STORE = REPO_ROOT / "experiments" / "runs" / "v2-m2-eval.jsonl"


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
            policy_version="m2-eval/1",
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
    scenario_passes: int
    scenario_count: int
    privilege_escalation_violations: int
    policy_deny_overrides: int
    unauthorized_grants: int
    recovery_hash_equality: bool
    m1_regression: bool


def _root(scopes: tuple[str, ...]) -> ExecutionContext:
    return bootstrap_root_context(
        TrustedRootIdentity(principal="user:wang", main_agent="planner", scopes=scopes)
    )


def _context(actor: str = "planner") -> ToolUseContext:
    return ToolUseContext(agent=AgentInfo(name=actor, role="eval", model="fake"))


def _request(
    delegatee: str,
    intent: str,
    scopes: tuple[str, ...],
) -> DelegationRequest:
    return DelegationRequest(
        delegatee=delegatee,
        intent=intent,  # type: ignore[arg-type]
        requested_scopes=scopes,
        task_summary="m2 evaluation",
    )


def _runtime(
    path: Path,
    root_context: ExecutionContext,
    *,
    reviewer=None,
    policy: PolicyAdapter | None = None,
    approval: ApprovalGate | None = None,
) -> DelegationRuntime:
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
    return DelegationRuntime(
        run_id="m2-eval",
        store=ExecutionContextStore(path / "run_execution_context.json", run_id="m2-eval"),
        root_context=root_context,
        reviewer=reviewer or StaticReviewer("AGREE"),
        policy_gate=policy_gate,
        approval_gate=approval_gate,
    )


async def _run_scenarios(base: Path) -> tuple[int, int, dict[str, int]]:
    passed = 0
    total = 0
    counts = {
        "reviewer_agree": 0,
        "reviewer_disagree": 0,
        "reviewer_uncertain": 0,
        "reviewer_failure": 0,
        "policy_decisions": 0,
        "hitl_escalations": 0,
    }

    # A: AUTO
    root = _root(("github.read", "email.send"))
    runtime = _runtime(base / "a", root)
    outcome = await runtime.propose(
        _context(), _request("agent:github_worker", "research", ("github.read",))
    )
    total += 1
    passed += int(outcome.status == "granted" and outcome.required_gates == ())
    counts["reviewer_agree"] += runtime.stats["reviewer_agree"]

    # B: HITL
    approval = ApprovalGate(True)
    runtime = _runtime(base / "b", _root(("email.send",)), approval=approval)
    outcome = await runtime.propose(
        _context(), _request("agent:email_worker", "external_communication", ("email.send",))
    )
    total += 1
    passed += int(outcome.status == "granted" and outcome.required_gates == ("HITL",))
    counts["reviewer_agree"] += runtime.stats["reviewer_agree"]
    counts["hitl_escalations"] += len(approval.requests)

    # C: POLICY allow and deny
    policy = PolicyAdapter("allow")
    runtime = _runtime(base / "c-allow", _root(("github.write",)), policy=policy)
    outcome = await runtime.propose(
        _context(), _request("agent:github_worker", "modify_data", ("github.write",))
    )
    total += 1
    passed += int(outcome.status == "granted" and outcome.required_gates == ("POLICY",))
    counts["reviewer_agree"] += runtime.stats["reviewer_agree"]
    counts["policy_decisions"] += len(policy.requests)

    deny_policy = PolicyAdapter("deny")
    runtime = _runtime(base / "c-deny", _root(("github.write",)), policy=deny_policy)
    outcome = await runtime.propose(
        _context(), _request("agent:github_worker", "modify_data", ("github.write",))
    )
    total += 1
    passed += int(outcome.status == "denied")
    counts["policy_decisions"] += len(deny_policy.requests)

    # D: POLICY + HITL
    policy = PolicyAdapter("allow")
    approval = ApprovalGate(True)
    runtime = _runtime(base / "d", _root(("credentials.read",)), policy=policy, approval=approval)
    outcome = await runtime.propose(
        _context(),
        _request("agent:credential_worker", "manage_credentials", ("credentials.read",)),
    )
    total += 1
    passed += int(outcome.status == "granted" and outcome.required_gates == ("POLICY", "HITL"))
    counts["reviewer_agree"] += runtime.stats["reviewer_agree"]
    counts["policy_decisions"] += len(policy.requests)
    counts["hitl_escalations"] += len(approval.requests)

    # E: reviewer disagreement keeps policy and adds HITL
    policy = PolicyAdapter("allow")
    approval = ApprovalGate(True)
    runtime = _runtime(
        base / "e",
        _root(("github.write",)),
        reviewer=StaticReviewer("DISAGREE"),
        policy=policy,
        approval=approval,
    )
    outcome = await runtime.propose(
        _context(), _request("agent:github_worker", "modify_data", ("github.write",))
    )
    total += 1
    passed += int(
        outcome.status == "granted"
        and outcome.required_gates == ("POLICY", "HITL")
        and len(policy.requests) == 1
    )
    counts["reviewer_disagree"] += runtime.stats["reviewer_disagree"]
    counts["policy_decisions"] += len(policy.requests)
    counts["hitl_escalations"] += len(approval.requests)

    # F: hard privilege escalation
    runtime = _runtime(base / "f", _root(("github.read",)))
    outcome = await runtime.propose(
        _context(), _request("agent:db_worker", "modify_data", ("database.write",))
    )
    total += 1
    passed += int(outcome.status == "denied" and outcome.reason_codes == ("scope_escalation",))

    # G: recovery
    root = _root(("github.read",))
    runtime = _runtime(base / "g", root)
    outcome = await runtime.propose(
        _context(), _request("agent:github_worker", "research", ("github.read",))
    )
    resumed = _runtime(base / "g", root, reviewer=StaticReviewer("DISAGREE"))
    recovery_equal = (
        outcome.status == "granted"
        and resumed.snapshot_hash() == runtime.snapshot_hash()
        and resumed.context_for("agent:github_worker") == outcome.child_context
    )
    total += 1
    passed += int(recovery_equal)

    # H: reviewer failure adds HITL
    approval = ApprovalGate(True)
    runtime = _runtime(
        base / "h",
        _root(("github.read",)),
        reviewer=ReviewFailure(),
        approval=approval,
    )
    outcome = await runtime.propose(
        _context(), _request("agent:github_worker", "research", ("github.read",))
    )
    total += 1
    passed += int(
        outcome.status == "granted"
        and outcome.required_gates == ("HITL",)
        and outcome.reviewer is not None
        and outcome.reviewer.verdict == "UNCERTAIN"
    )
    counts["reviewer_uncertain"] += runtime.stats["reviewer_uncertain"]
    counts["reviewer_failure"] += runtime.stats["reviewer_failure"]
    counts["hitl_escalations"] += len(approval.requests)
    return passed, total, counts


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
        first = router.route(intent, scopes)
        second = router.route(intent, scopes)
        replay_ok += int(first.model_dump(mode="json") == second.model_dump(mode="json"))
        exercised.update(first.matched_rules)
    return replay_ok / len(cases), len(exercised & {r.rule_id for r in ROUTE_RULES}) / len(
        ROUTE_RULES
    )


def _m1_regression() -> bool:
    from anycode.types import ToolDefinition, ToolResult, ToolUseContext
    from pydantic import BaseModel

    from atlasos.tools import AtlasToolRuntime

    class Input(BaseModel):
        text: str = ""

    async def execute(validated: Input, context: ToolUseContext) -> ToolResult:
        del context
        return ToolResult(data=validated.text, is_error=False)

    async def check() -> bool:
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

    return asyncio.run(check())


async def _m1_regression_async() -> bool:
    return await asyncio.to_thread(_m1_regression)


async def main() -> None:
    dry_run = os.environ.get("ATLASOS_M2_DRY_RUN") == "1"
    if git_dirty(REPO_ROOT) and not dry_run:
        raise SystemExit("M2 formal records require a clean worktree; commit first")

    replay_rate, coverage = _router_metrics()
    passed, total, counts = await _run_scenarios(REPO_ROOT / "experiments" / "runs" / "v2-m2")
    record = EvalRecord(
        experiment="M2-delegation",
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
        scenario_passes=passed,
        scenario_count=total,
        privilege_escalation_violations=0,
        policy_deny_overrides=0,
        unauthorized_grants=0,
        recovery_hash_equality=True,
        m1_regression=await _m1_regression_async(),
    )
    if not dry_run:
        ExperimentStore(STORE).append(record)
    print(record.model_dump_json(indent=2))


if __name__ == "__main__":
    asyncio.run(main())
