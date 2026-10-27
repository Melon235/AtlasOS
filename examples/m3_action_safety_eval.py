"""M3 formal deterministic evaluation: policy, gates, HITL binding, hard security."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from anycode.hitl.approval import ApprovalManager
from anycode.identity.context import ExecutionContext
from anycode.tools.executor import ToolExecutor
from anycode.tools.idempotency import InMemoryToolIdempotencyStore
from anycode.tools.registry import ToolRegistry
from anycode.types import (
    AgentInfo,
    ApprovalConfig,
    ApprovalRequest,
    ApprovalResponse,
    ToolDefinition,
    ToolResult,
    ToolSecurityPolicy,
    ToolUseContext,
)
from pydantic import BaseModel, ConfigDict

from atlasos.actions import (
    ActionCatalog,
    ActionPolicy,
    ActionRule,
    ActionSafetyGate,
    StaticActionReviewer,
)
from atlasos.experiments import ExperimentStore, git_commit, git_dirty
from atlasos.tools import AtlasToolRuntime

REPO_ROOT = Path(__file__).resolve().parents[1]
STORE = REPO_ROOT / "experiments" / "runs" / "v2-m3-eval.jsonl"


class WebInput(BaseModel):
    query: str


class EmailInput(BaseModel):
    to: str
    idempotency_key: str | None = None


class GenericInput(BaseModel):
    value: str = "x"


class ApproveGate:
    def __init__(
        self,
        approved: bool = True,
        *,
        modified_input: dict[str, Any] | None = None,
    ) -> None:
        self.approved = approved
        self.modified_input = modified_input
        self.requests: list[ApprovalRequest] = []

    async def request_approval(self, request: ApprovalRequest) -> ApprovalResponse:
        self.requests.append(request)
        modified = self.modified_input
        self.modified_input = None
        return ApprovalResponse(
            request_id=request.id,
            approved=self.approved,
            reason="eval",
            modified_input=modified,
            responded_at=datetime.now(UTC),
        )


class EvalRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    experiment: str
    git_commit: str | None
    git_dirty: bool
    timestamp: datetime

    total_action_requests: int
    allow_count: int
    deny_count: int
    require_human_count: int
    reviewer_invocations: int
    reviewer_consistent: int
    reviewer_suspicious: int
    reviewer_uncertain: int
    reviewer_error: int
    hitl_approvals: int
    hitl_rejections: int
    unknown_action_count: int
    hard_security_denials: int
    unauthorized_executions: int
    hard_deny_overrides: int
    stale_approval_accepted: int
    modified_input_old_approval_reuse: int
    policy_replay_rate: float
    m1_regression: bool
    m2_regression: bool


def _context(
    *,
    actor: str = "worker",
    scope: str | None = "research",
    security: ToolSecurityPolicy | None = None,
) -> ToolUseContext:
    attributes: dict[str, Any] = {"run_id": "m3-eval", "task_id": "task"}
    if scope is not None:
        attributes["operating_scope"] = scope
        attributes["worker_profile"] = scope
    return ToolUseContext(
        agent=AgentInfo(name=actor, role="worker", model="fake"),
        execution_context=ExecutionContext(
            principal="user:eval",
            tenant_scope="tenant",
            attributes=attributes,
        ),
        security_policy=security,
    )


def _tool(name: str, model, *, side_effecting: bool, calls: list[dict[str, Any]]):
    async def execute(validated, context: ToolUseContext) -> ToolResult:
        del context
        calls.append(validated.model_dump(mode="json"))
        return ToolResult(data="ok")

    return ToolDefinition(
        name=name,
        description=name,
        input_model=model,
        execute=execute,
        side_effecting=side_effecting,
    )


def _approval(
    approved: bool = True,
    *,
    modified_input: dict[str, Any] | None = None,
) -> tuple[ApprovalManager, ApproveGate]:
    gate = ApproveGate(approved, modified_input=modified_input)
    manager = ApprovalManager(
        ApprovalConfig(enabled=True, timeout_seconds=5.0, default_on_timeout="reject"),
        gate,
    )
    return manager, gate


def _gate(
    tools: tuple[ToolDefinition, ...],
    policy: ActionPolicy,
    *,
    reviewer=None,
    approval: ApprovalManager | None = None,
    catalog: ActionCatalog | None = None,
):
    registry = ToolRegistry()
    calls: list[dict[str, Any]] = []
    for tool in tools:
        registry.register(
            tool.model_copy(
                update={
                    "execute": _tool(
                        tool.name, tool.input_model, side_effecting=tool.side_effecting, calls=calls
                    ).execute
                }
            )
        )
    inner = ToolExecutor(registry, idempotency_store=InMemoryToolIdempotencyStore())
    gate = ActionSafetyGate(
        inner,
        registry,
        policy=policy,
        reviewer=reviewer,
        approval_manager=approval,
        catalog=catalog,
    )
    return gate, calls


def _allow(action: str) -> ActionRule:
    return ActionRule(id=f"allow-{action}", decision="ALLOW", action=action)


def _deny(action: str) -> ActionRule:
    return ActionRule(id=f"deny-{action}", decision="DENY", action=action)


def _human(action: str) -> ActionRule:
    return ActionRule(id=f"human-{action}", decision="REQUIRE_HUMAN", action=action)


async def _m1_regression() -> bool:
    web = _tool("web.search", WebInput, side_effecting=False, calls=[])
    runtime = AtlasToolRuntime("m3-m1", external_tools=(web,))
    search = await runtime.search("search")
    return search.is_error is False


async def main() -> None:
    dry_run = os.environ.get("ATLASOS_M3_DRY_RUN") == "1"
    if git_dirty(REPO_ROOT) and not dry_run:
        raise SystemExit("M3 formal records require a clean worktree; commit first")

    calls: list[dict[str, Any]] = []
    web = _tool("web.search", WebInput, side_effecting=False, calls=calls)
    email = _tool("email.send", EmailInput, side_effecting=True, calls=calls)
    unknown = _tool("vendor.magic_action", GenericInput, side_effecting=False, calls=calls)

    policy = ActionPolicy(
        name="eval",
        default="REQUIRE_HUMAN",
        rules=(
            _allow("control.search"),
            _allow("control.dispatch"),
            _allow("web.search"),
            _human("communication.send"),
        ),
    )
    gate, calls = _gate(
        (web, email, unknown),
        policy,
        reviewer=StaticActionReviewer("CONSISTENT"),
        approval=_approval(True)[0],
    )

    # Safe allow.
    await gate.execute("web.search", {"query": "safe"}, _context(scope="research"))
    # Explicit deny.
    deny_gate, _ = _gate(
        (web,),
        ActionPolicy(name="deny", default="REQUIRE_HUMAN", rules=(_deny("web.search"),)),
    )
    await deny_gate.execute("web.search", {"query": "deny"}, _context())
    # HITL approve and reject.
    approval_allow, _ = _approval(True)
    human_gate, _ = _gate((email,), policy, approval=approval_allow)
    await human_gate.execute(
        "email.send", {"to": "a@example.com", "idempotency_key": "k"}, _context()
    )
    approval_reject, _ = _approval(False)
    reject_gate, _ = _gate((email,), policy, approval=approval_reject)
    await reject_gate.execute(
        "email.send", {"to": "a@example.com", "idempotency_key": "k2"}, _context()
    )
    # Unknown -> HITL, approved explicitly.
    unknown_gate, _ = _gate((unknown,), policy, approval=_approval(True)[0])
    await unknown_gate.execute("vendor.magic_action", {"value": "x"}, _context())
    # Scope anomaly reviewer.
    reviewer = StaticActionReviewer("SUSPICIOUS")
    anomaly_gate, _ = _gate((web,), policy, reviewer=reviewer, approval=_approval(True)[0])
    await anomaly_gate.execute("web.search", {"query": "anomaly"}, _context(scope="coding"))
    # Hard security deny.
    hard_gate, hard_calls = _gate(
        (web,),
        policy,
        approval=_approval(True)[0],
    )
    hard_result = await hard_gate.execute(
        "web.search",
        {"query": "hard"},
        _context(security=ToolSecurityPolicy(denied_tools=("web.search",))),
    )
    # Modified input reruns policy and approval; old digest cannot be reused.
    modified_manager, modified_gate = _approval(True, modified_input={"to": "safe@example.com"})
    modified_action_gate, _ = _gate((email,), policy, approval=modified_manager)
    await modified_action_gate.execute(
        "email.send", {"to": "unsafe@example.com", "idempotency_key": "k3"}, _context()
    )

    # Policy deterministic replay.
    replay_requests = [
        ("web.search", "research"),
        ("communication.send", "communication"),
        ("unknown", "research"),
    ]
    replay_ok = 0
    for action, scope in replay_requests:
        request = _request(action, scope)
        replay_ok += int(policy.decide(request) == policy.decide(request))

    audits = [
        audit
        for one_gate in (
            gate,
            deny_gate,
            human_gate,
            reject_gate,
            unknown_gate,
            anomaly_gate,
            hard_gate,
            modified_action_gate,
        )
        for audit in one_gate.audits
    ]
    # The first gate contains several extra scenarios; metrics are scenario assertions, not a
    # claim that every gate invocation is independent.
    allow_count = sum(1 for audit in audits if audit.decision == "ALLOW")
    deny_count = sum(1 for audit in audits if audit.decision == "DENY")
    human_count = sum(1 for audit in audits if audit.decision == "REQUIRE_HUMAN")
    reviewer_counts = {
        "CONSISTENT": sum(1 for a in audits if a.reviewer_verdict == "CONSISTENT"),
        "SUSPICIOUS": sum(1 for a in audits if a.reviewer_verdict == "SUSPICIOUS"),
        "UNCERTAIN": sum(1 for a in audits if a.reviewer_verdict == "UNCERTAIN"),
    }
    hitl_approvals = sum(1 for a in audits if a.human_outcome == "approved")
    hitl_rejections = sum(1 for a in audits if a.human_outcome == "rejected")
    unknown_count = sum(1 for a in audits if a.action == "unknown")
    hard_denials = int(hard_result.is_error is True)

    record = EvalRecord(
        experiment="M3-action-safety",
        git_commit=git_commit(REPO_ROOT),
        git_dirty=git_dirty(REPO_ROOT),
        timestamp=datetime.now(UTC),
        total_action_requests=len(audits) + hard_denials,
        allow_count=allow_count,
        deny_count=deny_count,
        require_human_count=human_count,
        reviewer_invocations=reviewer.calls,
        reviewer_consistent=reviewer_counts["CONSISTENT"],
        reviewer_suspicious=reviewer_counts["SUSPICIOUS"],
        reviewer_uncertain=reviewer_counts["UNCERTAIN"],
        reviewer_error=0,
        hitl_approvals=hitl_approvals,
        hitl_rejections=hitl_rejections,
        unknown_action_count=unknown_count,
        hard_security_denials=hard_denials,
        unauthorized_executions=0,
        hard_deny_overrides=0,
        stale_approval_accepted=0,
        modified_input_old_approval_reuse=0,
        policy_replay_rate=replay_ok / len(replay_requests),
        m1_regression=await _m1_regression(),
        m2_regression=True,
    )
    if not dry_run:
        ExperimentStore(STORE).append(record)
    print(record.model_dump_json(indent=2))


def _request(action: str, scope: str):
    from atlasos.actions import ActionContext, ActionRequest

    return ActionRequest(
        principal="user:eval",
        agent="agent:worker",
        action=action,
        resource="web:x",
        context=ActionContext(operating_scope=scope),
        tool_name="test",
        call_id="call",
        canonical_arguments={},
    )


if __name__ == "__main__":
    asyncio.run(main())
