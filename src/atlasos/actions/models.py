"""M3 Cedar-style action request/decision models."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

ActionDecisionValue = Literal["ALLOW", "DENY", "REQUIRE_HUMAN"]
ScopeAlignment = Literal["ALIGNED", "MISMATCH", "UNKNOWN"]
ReviewerVerdict = Literal["CONSISTENT", "SUSPICIOUS", "UNCERTAIN"]


class ActionContext(BaseModel):
    """Request-specific metadata; principal/agent/resource stay top-level."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str | None = None
    task_id: str | None = None
    worker_profile: str | None = None
    operating_scope: str | None = None
    tenant: str | None = None
    classification: str | None = None
    trace_id: str | None = None


class ActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    principal: str
    agent: str
    action: str
    resource: str
    context: ActionContext
    tool_name: str
    call_id: str
    canonical_arguments: dict[str, Any]
    digest: str = ""


class ActionReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    principal: str
    agent: str
    action: str
    resource: str
    operating_scope: str | None = None
    worker_profile: str | None = None
    task_id: str | None = None
    canonical_arguments: dict[str, Any]


class ActionReviewResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    verdict: ReviewerVerdict
    reason_codes: tuple[str, ...] = ()


class PolicyResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    decision: ActionDecisionValue
    matched_rule: str | None = None
    reason_codes: tuple[str, ...] = ()
    policy_name: str


class ActionDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    decision: ActionDecisionValue
    reason_codes: tuple[str, ...] = ()
    matched_rule: str | None = None
    policy_name: str | None = None
    scope_alignment: ScopeAlignment = "UNKNOWN"
    reviewer_verdict: ReviewerVerdict | None = None
    external_policy_outcome: str | None = None


class ActionAudit(BaseModel):
    """In-memory/structured event metadata. M4 owns the durable ledger."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    call_id: str
    principal: str
    agent: str
    tool_name: str
    action: str
    resource: str
    digest: str
    decision: ActionDecisionValue
    matched_rule: str | None = None
    reviewer_verdict: ReviewerVerdict | None = None
    human_outcome: str | None = None
    executed: bool = False
    reason_codes: tuple[str, ...] = Field(default_factory=tuple)
    policy_name: str | None = None
