"""Delegation-facing models. Capabilities live only in AnyCode DelegationGrant.scopes."""

from __future__ import annotations

from typing import Literal

from anycode.identity.context import DelegationGrant, ExecutionContext
from pydantic import BaseModel, ConfigDict, Field

DelegationIntent = Literal[
    "research",
    "read_data",
    "modify_data",
    "external_communication",
    "execute_code",
    "manage_credentials",
    "administrative",
]

Gate = Literal["POLICY", "HITL"]
GATE_ORDER: tuple[Gate, ...] = ("POLICY", "HITL")

ReviewerVerdict = Literal["AGREE", "DISAGREE", "UNCERTAIN"]


class DelegationRequest(BaseModel):
    """Untrusted model proposal. Runtime fills identity and request id."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    delegatee: str = Field(min_length=1, max_length=128)
    intent: DelegationIntent
    requested_scopes: tuple[str, ...] = Field(min_length=1)
    task_summary: str = Field(min_length=1, max_length=2000)
    reason: str | None = Field(default=None, max_length=2000)


class ReviewerInput(BaseModel):
    """Minimal stateless reviewer input; no grant or runtime access."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    principal: str
    current_actor: str
    effective_scopes: tuple[str, ...]
    delegatee: str
    intent: DelegationIntent
    requested_scopes: tuple[str, ...]
    task_summary: str
    reason: str | None = None
    matched_rules: tuple[str, ...]
    router_gates: tuple[Gate, ...]
    router_reason_codes: tuple[str, ...]


class ReviewerResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    verdict: ReviewerVerdict
    reason_codes: tuple[str, ...] = ()


class RouteDecision(BaseModel):
    """Deterministic router output. POLICY and HITL are orthogonal gates."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    matched_rules: tuple[str, ...] = ()
    required_gates: tuple[Gate, ...] = ()
    reason_codes: tuple[str, ...] = ()


class DelegationOutcome(BaseModel):
    """Result of one runtime delegation attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str
    status: Literal["granted", "denied"]
    reason_codes: tuple[str, ...] = ()
    route: RouteDecision | None = None
    reviewer: ReviewerResult | None = None
    required_gates: tuple[Gate, ...] = ()

    policy_outcome: str | None = None
    approval_outcome: str | None = None
    approval_request_id: str | None = None

    grant: DelegationGrant | None = None
    child_context: ExecutionContext | None = None


class DelegationDenied(RuntimeError):
    """Raised by helpers that require a granted outcome."""

    def __init__(self, outcome: DelegationOutcome) -> None:
        self.outcome = outcome
        super().__init__(
            "delegation was not granted: " + ", ".join(outcome.reason_codes or ("unknown",))
        )
