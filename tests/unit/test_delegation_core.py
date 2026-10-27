"""M2 core: root bootstrap, hard validation, capabilities, expiry, deterministic router."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from anycode.identity.context import DelegationGrant, ExecutionContext
from anycode.types import AgentInfo, ToolUseContext
from pydantic import ValidationError

from atlasos.delegation import (
    DelegationRequest,
    DelegationRuntime,
    DelegationValidationError,
    DelegationValidator,
    ExecutionContextStore,
    IntentRouter,
    InvalidScope,
    StaticReviewer,
    TrustedRootIdentity,
    bootstrap_root_context,
    canonical_actor,
    canonicalize_scopes,
    derive_child_expiry,
    effective_scopes,
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
    principal: str = "user:wang",
    main_agent: str = "planner",
    **identity_fields,
) -> ExecutionContext:
    return bootstrap_root_context(
        TrustedRootIdentity(
            principal=principal,
            main_agent=main_agent,
            scopes=scopes,
            expires_at=expires_at,
            **identity_fields,
        )
    )


def tool_context(actor: str = "planner", context: ExecutionContext | None = None):
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


def make_runtime(
    tmp_path: Path,
    *,
    root: ExecutionContext | None = None,
    reviewer=None,
    policy_gate=None,
    approval_gate=None,
    review_mode: str = "always",
    validator: DelegationValidator | None = None,
) -> DelegationRuntime:
    root = root or make_root()
    run_id = "run-1"
    store = ExecutionContextStore(tmp_path / "run_execution_context.json", run_id=run_id)
    return DelegationRuntime(
        run_id=run_id,
        store=store,
        root_context=root,
        reviewer=reviewer if reviewer is not None else StaticReviewer("AGREE"),
        policy_gate=policy_gate,
        approval_gate=approval_gate,
        review_mode=review_mode,  # type: ignore[arg-type]
        validator=validator,
    )


# T01/T02
def test_root_grant_bootstrap_is_runtime_only_and_request_rejects_identity(
    tmp_path: Path,
) -> None:
    root = make_root(scopes=("github.read",))
    assert root.principal == "user:wang"
    assert root.delegation[0].delegator == "user:wang"
    assert root.delegation[0].delegatee == "agent:planner"
    assert root.delegation[0].scopes == ("github.read",)

    with pytest.raises(ValidationError):
        DelegationRequest.model_validate(
            {
                "delegatee": "agent:worker",
                "intent": "research",
                "requested_scopes": ["github.read"],
                "task_summary": "x",
                "principal": "user:forged",
            }
        )
    with pytest.raises(ValidationError):
        DelegationRequest.model_validate(
            {
                "delegatee": "agent:worker",
                "intent": "research",
                "requested_scopes": ["github.read"],
                "task_summary": "x",
                "delegator": "agent:planner",
            }
        )


# T03
def test_chain_root_must_match_principal() -> None:
    bad = ExecutionContext(
        principal="user:wang",
        delegation=(
            DelegationGrant(
                delegator="user:other",
                delegatee="agent:planner",
                scopes=("github.read",),
            ),
        ),
    )
    with pytest.raises(DelegationValidationError, match="principal"):
        DelegationValidator().validate_context(bad)


# T04
def test_chain_must_be_contiguous() -> None:
    bad = ExecutionContext(
        principal="user:wang",
        delegation=(
            DelegationGrant(
                delegator="user:wang",
                delegatee="agent:planner",
                scopes=("github.read",),
            ),
            DelegationGrant(
                delegator="agent:other",
                delegatee="agent:worker",
                scopes=("github.read",),
            ),
        ),
    )
    with pytest.raises(DelegationValidationError, match="delegatee"):
        DelegationValidator().validate_context(bad)


# T05
def test_actor_must_be_chain_tail() -> None:
    root = make_root()
    with pytest.raises(DelegationValidationError, match="tail"):
        DelegationValidator().validate_context(root, expected_actor="agent:other")


# T06
def test_delegation_cycle_is_rejected() -> None:
    root = make_root()
    child = root.model_copy(
        update={
            "delegation": (
                *root.delegation,
                DelegationGrant(
                    delegator="agent:planner",
                    delegatee="agent:worker",
                    scopes=("github.read",),
                ),
            )
        }
    )
    validator = DelegationValidator()
    with pytest.raises(DelegationValidationError, match="ancestor delegation path"):
        validator.validate_request(child, "agent:worker", request(delegatee="agent:planner"))


# T07
def test_expired_ancestor_is_rejected() -> None:
    expired = datetime.now(UTC) - timedelta(seconds=1)
    root = make_root(expires_at=expired)
    with pytest.raises(DelegationValidationError, match="expired"):
        DelegationValidator().validate_context(root)


# T08
def test_empty_delegation_has_zero_capabilities() -> None:
    context = ExecutionContext(principal="user:wang")
    assert effective_scopes(context) == frozenset()


# T09
def test_scope_format_and_canonical_order() -> None:
    assert canonicalize_scopes(["github.read", "rag.read", "github.read"]) == (
        "github.read",
        "rag.read",
    )
    for invalid in ("github", "github.*", "github.read:repo", ""):
        with pytest.raises(InvalidScope):
            canonicalize_scopes([invalid])
    assert canonical_actor("planner") == canonical_actor("agent:planner")


# T10
async def test_requested_scopes_must_be_parent_subset(tmp_path: Path) -> None:
    root = make_root(scopes=("github.read",))
    reviewer = StaticReviewer("AGREE")
    runtime = make_runtime(tmp_path, root=root, reviewer=reviewer)
    outcome = await runtime.propose(
        tool_context("planner", root),
        request(delegatee="agent:worker", scopes=("database.write",)),
    )
    assert outcome.status == "denied"
    assert outcome.reason_codes == ("scope_escalation",)
    assert outcome.route is None
    assert reviewer.calls == 0


# T11
def test_effective_scopes_are_intersection() -> None:
    context = ExecutionContext(
        principal="user:wang",
        delegation=(
            DelegationGrant(
                delegator="user:wang",
                delegatee="agent:a",
                scopes=("github.read", "rag.read"),
            ),
            DelegationGrant(
                delegator="agent:a",
                delegatee="agent:b",
                scopes=("rag.read", "email.send"),
            ),
        ),
    )
    assert effective_scopes(context) == frozenset({"rag.read"})


# T12
async def test_child_expiry_cannot_outlive_ancestor(tmp_path: Path) -> None:
    expires = datetime.now(UTC) + timedelta(hours=1)
    root = make_root(expires_at=expires)
    runtime = make_runtime(tmp_path, root=root)
    outcome = await runtime.propose(
        tool_context("planner", root),
        request(delegatee="agent:github_worker", scopes=("github.read",)),
    )
    assert outcome.status == "granted"
    assert outcome.grant is not None
    assert outcome.grant.expires_at == expires
    assert derive_child_expiry(root) == expires


# T13-T17
def test_intent_routes_and_multiple_scope_union() -> None:
    router = IntentRouter()
    assert router.route("research", ("github.read",)).required_gates == ()
    assert router.route("modify_data", ("github.write",)).required_gates == ("POLICY",)
    assert router.route("external_communication", ("email.send",)).required_gates == ("HITL",)
    unmatched = router.route("research", ("database.write",))
    assert unmatched.required_gates == ("HITL",)
    assert "unmatched" in unmatched.matched_rules

    combined = router.route("modify_data", ("github.write", "email.send"))
    assert combined.required_gates == ("POLICY", "HITL")
    # The email rule is intent-specific; a mismatched intent safely falls back to HITL.
    assert combined.matched_rules == ("modify_data.github.write", "unmatched")
    assert "unmatched_intent_capability" in combined.reason_codes


# T18
def test_routing_is_deterministic() -> None:
    router = IntentRouter()
    first = router.route("manage_credentials", ("credentials.read", "email.send"))
    second = router.route("manage_credentials", ("credentials.read", "email.send"))
    assert first == second
    assert first.required_gates == ("POLICY", "HITL")
    assert first.model_dump(mode="json") == second.model_dump(mode="json")
