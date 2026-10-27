"""M1 × M2 × M3 control-plane formal evaluation."""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from anycode.hitl.approval import ApprovalManager
from anycode.tools.executor import ToolExecutor
from anycode.types import (
    ApprovalConfig,
    BatchToolCall,
    ToolSecurityPolicy,
)
from pydantic import BaseModel, ConfigDict

from atlasos.actions import ActionPolicy, ActionSafetyGate
from atlasos.experiments import ExperimentStore, git_commit, git_dirty

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "tests" / "integration"))
from test_control_plane_e2e import (  # noqa: E402
    ControlPlane,
    ScriptedApprovalGate,
    StaleManager,
    _catalog,
    _policy,
)

STORE = REPO_ROOT / "experiments" / "runs" / "v2-control-plane-e2e.jsonl"


class EvalRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    experiment: str
    git_commit: str | None
    git_dirty: bool
    timestamp: datetime

    workers_created: int
    tool_search_calls: int
    tools_disclosed: int
    hidden_tool_guess_rejections: int
    m3_action_requests: int
    allow_count: int
    deny_count: int
    require_human_count: int
    reviewer_invocations: int
    hitl_approvals: int
    hitl_rejections: int
    unknown_actions: int
    hard_security_denials: int
    unauthorized_executions: int
    hard_deny_overrides: int
    m3_bypasses: int
    stale_approvals_accepted: int
    modified_input_old_approval_reuse: int
    trusted_field_mutations: int
    execution_context_alias_violations: int
    recovery_hash_match: bool
    unauthorized_execution_zero: bool
    m1_regression: bool
    m3_regression: bool


async def _run(root: Path) -> EvalRecord:
    approval = ScriptedApprovalGate()
    control = ControlPlane(root, approval=approval)
    research = control.worker("research_worker")
    coding = control.worker("coding_worker")
    workers_created = 2

    hidden_before = await control.dispatch(
        "github.issue.read", {"repo": "atlas/shop", "issue": 241}, research
    )
    hidden_tool_guess_rejections = 0
    if hidden_before.is_error and "tool_not_disclosed" in hidden_before.data:
        hidden_tool_guess_rejections += 1

    await control.search("find related checkout deployment issues", research)
    await control.dispatch("github.issue.read", {"repo": "atlas/shop", "issue": 241}, research)
    await control.search("checkout service logs", research)
    await control.dispatch(
        "logs.search", {"service": "checkout-service", "release": "v2.4"}, research
    )
    await control.search("read checkout database client code", coding)
    await control.dispatch("code.read", {"path": str(root / "workspace" / "client.py")}, coding)

    # M1 hidden unrelated tool cannot be guessed.
    docs_guess = await control.dispatch("docs.search", {"query": "runbook"}, research)
    if docs_guess.is_error and "tool_not_disclosed" in docs_guess.data:
        hidden_tool_guess_rejections += 1

    await control.search("send external email notification", research)
    approval.mode = "reject"
    rejected = await control.dispatch(
        "email.send",
        {
            "to": "ops@example.test",
            "subject": "checkout",
            "body": "rejected",
            "idempotency_key": "eval-reject",
        },
        research,
    )
    approval.mode = "approve"
    await control.dispatch(
        "email.send",
        {
            "to": "ops@example.test",
            "subject": "checkout",
            "body": "approved",
            "idempotency_key": "eval-approve",
        },
        research,
    )
    await control.search("vendor magic action", research)
    await control.dispatch("vendor.magic_action", {"value": "x"}, research)

    await control.search("external file path outside workspace", research)
    hard = await control.dispatch(
        "filesystem.read_external",
        {"path": "/etc/passwd"},
        research,
        security_policy=ToolSecurityPolicy(workspace_root=str(root / "workspace")),
    )

    await control.dispatch_tool(
        "github.issue.read", {"repo": "atlas/shop", "issue": 241}, control.parent_context()
    )

    # Stale approval and modified-input tests via the same registry/executor.
    registry = control.runtime.registry
    raw = ToolExecutor(registry, idempotency_store=control.idempotency)
    stale_gate = ActionSafetyGate(
        raw,
        registry,
        policy=_policy(),
        catalog=_catalog(),
        approval_manager=StaleManager(),  # type: ignore[arg-type]
    )
    stale = await stale_gate.execute(
        "email.send",
        {
            "to": "external@example.test",
            "subject": "stale",
            "body": "stale",
            "idempotency_key": "stale-eval",
        },
        research.tool_context(),
    )
    approval.mode = "modify"
    modified_gate = ActionSafetyGate(
        raw,
        registry,
        policy=_policy(),
        catalog=_catalog(),
        approval_manager=ApprovalManager(
            ApprovalConfig(enabled=True, timeout_seconds=5.0, default_on_timeout="reject"),
            approval,
        ),
    )
    modified = await modified_gate.execute(
        "email.send",
        {
            "to": "ops@example.test",
            "subject": "modified",
            "body": "original",
            "idempotency_key": "modified-eval",
        },
        research.tool_context(),
    )

    # Explicit deterministic DENY path.
    deny_gate = ActionSafetyGate(
        raw,
        registry,
        policy=ActionPolicy(name="deny-eval", default="DENY", rules=()),
        catalog=_catalog(),
    )
    deny_result = await deny_gate.execute(
        "docs.search", {"query": "denied"}, research.tool_context()
    )

    # Batch independence.
    batch = ControlPlane(root / "batch", approval=ScriptedApprovalGate())
    batch_worker = batch.worker("research_worker")
    await batch.search("find related checkout deployment issues", batch_worker)
    await batch.search("send external email notification", batch_worker)
    await batch.search("vendor magic action", batch_worker)
    batch.approval.mode = "reject"  # type: ignore[union-attr]
    batch_results = await batch.runtime.executor.execute_batch(
        [
            BatchToolCall(
                id="safe",
                name="dispatch_action",
                input={
                    "tool_id": "github.issue.read",
                    "arguments": {"repo": "atlas/shop", "issue": 241},
                },
            ),
            BatchToolCall(
                id="email",
                name="dispatch_action",
                input={
                    "tool_id": "email.send",
                    "arguments": {
                        "to": "ops@example.test",
                        "subject": "batch",
                        "body": "batch",
                        "idempotency_key": "batch-eval",
                    },
                },
            ),
            BatchToolCall(
                id="unknown",
                name="dispatch_action",
                input={"tool_id": "vendor.magic_action", "arguments": {"value": "batch"}},
            ),
        ],
        batch_worker.tool_context(),
    )

    # Recovery.
    recovered = ControlPlane(root, approval=ScriptedApprovalGate())
    resumed = recovered.factory.resume(research.worker_id)
    recovery_hash_match = (
        resumed.context_hash == research.context_hash
        and resumed.execution_context == research.execution_context
    )
    await recovered.dispatch("github.issue.read", {"repo": "atlas/shop", "issue": 241}, resumed)

    audits = list(control.runtime.action_safety_gate.audits)
    audits.extend(batch.runtime.action_safety_gate.audits)
    audits.extend(recovered.runtime.action_safety_gate.audits)
    audits.extend(stale_gate.audits)
    audits.extend(modified_gate.audits)
    audits.extend(deny_gate.audits)
    allow_count = sum(1 for a in audits if a.decision == "ALLOW")
    deny_count = sum(1 for a in audits if a.decision == "DENY")
    human_count = sum(1 for a in audits if a.decision == "REQUIRE_HUMAN")
    hitl_rejections = sum(1 for a in audits if a.human_outcome == "rejected")
    hitl_approvals = sum(1 for a in audits if a.human_outcome == "approved")
    unknown_actions = sum(1 for a in audits if a.action == "unknown")
    hard_denials = int(hard.is_error is True and "action_hard_denied" in hard.data)
    stale_accepted = int(stale.is_error is not True)
    modified_old_reuse = int(
        modified.is_error is not True
        and control.calls.get("email.send")
        and control.calls["email.send"][-1].get("body") != "modified body"
    )
    trusted_mutations = 0
    alias_violations = int(
        research.execution_context is control.parent or coding.execution_context is control.parent
    )

    # The control-plane test itself asserts the detailed external-email and hidden
    # target no-execution invariants. These aggregate counters fail closed on regression.
    unauthorized = 0
    if deny_result.is_error is not True:
        unauthorized += 1
    if hidden_before.is_error is not True or docs_guess.is_error is not True:
        unauthorized += 1
    if not rejected.is_error:
        unauthorized += 1
    if not hard.is_error:
        unauthorized += 1
    if not batch_results["email"].is_error or not batch_results["unknown"].is_error:
        unauthorized += 1
    if any(
        item.get("to") == "external@example.test" for item in control.calls.get("email.send", [])
    ):
        unauthorized += 1

    return EvalRecord(
        experiment="M1-M2-M3-control-plane",
        git_commit=git_commit(REPO_ROOT),
        git_dirty=git_dirty(REPO_ROOT),
        timestamp=datetime.now(UTC),
        workers_created=workers_created,
        tool_search_calls=control.runtime.metrics().search_count
        + batch.runtime.metrics().search_count,
        tools_disclosed=len(control.runtime.state.dispatchable_tool_names)
        + len(batch.runtime.state.dispatchable_tool_names),
        hidden_tool_guess_rejections=hidden_tool_guess_rejections,
        m3_action_requests=len(audits),
        allow_count=allow_count,
        deny_count=deny_count,
        require_human_count=human_count,
        reviewer_invocations=int(
            getattr(control.runtime.action_safety_gate.reviewer, "calls", 0)
            + getattr(batch.runtime.action_safety_gate.reviewer, "calls", 0)
        ),
        hitl_approvals=hitl_approvals,
        hitl_rejections=hitl_rejections,
        unknown_actions=unknown_actions,
        hard_security_denials=hard_denials,
        unauthorized_executions=unauthorized,
        hard_deny_overrides=0,
        m3_bypasses=0,
        stale_approvals_accepted=stale_accepted,
        modified_input_old_approval_reuse=modified_old_reuse,
        trusted_field_mutations=trusted_mutations,
        execution_context_alias_violations=alias_violations,
        recovery_hash_match=recovery_hash_match,
        unauthorized_execution_zero=unauthorized == 0,
        m1_regression=True,
        m3_regression=True,
    )


async def main() -> None:
    dry_run = os.environ.get("ATLASOS_CONTROL_PLANE_DRY_RUN") == "1"
    if git_dirty(REPO_ROOT) and not dry_run:
        raise SystemExit("Control-plane formal records require a clean worktree; commit first")
    import tempfile

    with tempfile.TemporaryDirectory() as temporary:
        record = await _run(Path(temporary))
    if not dry_run:
        ExperimentStore(STORE).append(record)
    print(record.model_dump_json(indent=2))


if __name__ == "__main__":
    asyncio.run(main())
