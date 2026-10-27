"""M3 action safety tests: policy core, enforcement, hard security, HITL, idempotency."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from anycode.hitl.approval import ApprovalManager
from anycode.identity.context import ExecutionContext
from anycode.tools.executor import ToolExecutor
from anycode.tools.idempotency import (
    InMemoryToolIdempotencyStore,
)
from anycode.tools.registry import ToolRegistry
from anycode.types import (
    AgentInfo,
    ApprovalConfig,
    ApprovalRequest,
    ApprovalResponse,
    BatchToolCall,
    ToolDefinition,
    ToolResult,
    ToolSecurityPolicy,
    ToolUseContext,
)
from pydantic import BaseModel, ValidationError

from atlasos.actions import (
    DEFAULT_MAPPINGS,
    ActionCatalog,
    ActionMapping,
    ActionPolicy,
    ActionRequest,
    ActionRule,
    ActionSafetyGate,
    PolicyLoadError,
    StaticActionReviewer,
)
from atlasos.tools import AtlasToolRuntime


class WebInput(BaseModel):
    query: str
    principal: str | None = None
    agent: str | None = None


class EmailInput(BaseModel):
    to: str
    subject: str = "subject"
    body: str = "body"
    idempotency_key: str | None = None


class PathInput(BaseModel):
    path: str


class BashInput(BaseModel):
    command: str


class GenericInput(BaseModel):
    value: str = "x"


class GithubInput(BaseModel):
    repo: str


def make_context(
    *,
    actor: str = "worker",
    scope: str | None = "research",
    principal: str = "user:test",
    security_policy: ToolSecurityPolicy | None = None,
) -> ToolUseContext:
    attributes: dict[str, Any] = {"run_id": "r1", "task_id": "t1"}
    if scope is not None:
        attributes["operating_scope"] = scope
        attributes["worker_profile"] = scope
    return ToolUseContext(
        agent=AgentInfo(name=actor, role="worker", model="fake"),
        execution_context=ExecutionContext(
            principal=principal,
            tenant_scope="tenant-a",
            attributes=attributes,
        ),
        security_policy=security_policy,
    )


def make_policy(
    rules: tuple[ActionRule, ...] = (),
    *,
    default: str = "REQUIRE_HUMAN",
) -> ActionPolicy:
    return ActionPolicy(name="test", default=default, rules=rules)  # type: ignore[arg-type]


def allow(action: str, rule_id: str = "allow") -> ActionRule:
    return ActionRule(id=rule_id, decision="ALLOW", action=action)


def deny(action: str, rule_id: str = "deny") -> ActionRule:
    return ActionRule(id=rule_id, decision="DENY", action=action)


def human(action: str, rule_id: str = "human") -> ActionRule:
    return ActionRule(id=rule_id, decision="REQUIRE_HUMAN", action=action)


def make_gate(
    *tools: ToolDefinition,
    policy: ActionPolicy,
    reviewer=None,
    approval_manager: ApprovalManager | None = None,
    idempotency_store=None,
    catalog: ActionCatalog | None = None,
):
    registry = ToolRegistry()
    calls: list[dict[str, Any]] = []

    for tool in tools:

        async def execute(validated, context: ToolUseContext, _name=tool.name) -> ToolResult:
            del context
            calls.append(validated.model_dump(mode="json"))
            return ToolResult(
                data=f"{_name}:{json.dumps(validated.model_dump(mode='json'), sort_keys=True)}"
            )

        registry.register(
            tool.model_copy(
                update={
                    "execute": execute,
                }
            )
        )
    inner = ToolExecutor(
        registry,
        idempotency_store=idempotency_store or InMemoryToolIdempotencyStore(),
    )
    gate = ActionSafetyGate(
        inner,
        registry,
        policy=policy,
        reviewer=reviewer,
        approval_manager=approval_manager,
        catalog=catalog,
    )
    return gate, calls


def web_tool(side_effecting: bool = False, name: str = "web.search") -> ToolDefinition:
    async def execute(validated, context):
        del validated, context
        return ToolResult(data="unused")

    return ToolDefinition(
        name=name,
        description="search",
        input_model=WebInput,
        execute=execute,
        side_effecting=side_effecting,
    )


def email_tool() -> ToolDefinition:
    async def execute(validated, context):
        del validated, context
        return ToolResult(data="unused")

    return ToolDefinition(
        name="email.send",
        description="send",
        input_model=EmailInput,
        execute=execute,
        side_effecting=True,
    )


class RaisingReviewer:
    async def review(self, request):
        del request
        raise RuntimeError("reviewer unavailable")


class ApprovalGate:
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
        if modified is not None:
            self.modified_input = None
        return ApprovalResponse(
            request_id=request.id,
            approved=self.approved,
            reason="test",
            modified_input=modified,
            responded_at=datetime.now(UTC),
        )


def approval_manager(gate: ApprovalGate) -> ApprovalManager:
    return ApprovalManager(
        ApprovalConfig(
            enabled=True,
            timeout_seconds=5.0,
            default_on_timeout="reject",
        ),
        gate,
    )


def policy_request(action: str = "web.search", *, agent: str = "agent:worker") -> ActionRequest:
    return ActionRequest(
        principal="user:test",
        agent=agent,
        action=action,
        resource="web:x",
        context={"run_id": "r1", "operating_scope": "research"},  # type: ignore[arg-type]
        tool_name="web.search",
        call_id="call",
        canonical_arguments={"query": "x"},
    )


# M3-01..06
def test_policy_requires_default_and_first_match_is_deterministic() -> None:
    with pytest.raises(ValidationError):
        ActionPolicy.model_validate({"name": "x", "rules": []})
    with pytest.raises(ValidationError):
        ActionPolicy(name="x", default="ALLOW")  # type: ignore[arg-type]

    policy = make_policy(
        (
            ActionRule(id="first-deny", decision="DENY", action="web.search"),
            ActionRule(id="later-allow", decision="ALLOW", action="web.search"),
        )
    )
    first = policy.decide(policy_request())
    second = policy.decide(policy_request())
    assert first == second
    assert first.decision == "DENY"
    assert first.matched_rule == "first-deny"

    explicit_allow = make_policy((allow("web.search"),))
    assert explicit_allow.decide(policy_request()).decision == "ALLOW"
    explicit_human = make_policy((human("web.search"),))
    assert explicit_human.decide(policy_request()).decision == "REQUIRE_HUMAN"
    unmatched = make_policy()
    assert unmatched.decide(policy_request()).decision == "REQUIRE_HUMAN"
    assert "default_require_human" in unmatched.decide(policy_request()).reason_codes


# M3-07/M3-08
def test_policy_load_failure_and_immutability(tmp_path: Path) -> None:
    with pytest.raises(PolicyLoadError):
        ActionPolicy.from_file(tmp_path / "missing.json")
    (tmp_path / "bad.json").write_text("{", encoding="utf-8")
    with pytest.raises(PolicyLoadError):
        ActionPolicy.from_file(tmp_path / "bad.json")
    (tmp_path / "bad-default.json").write_text(
        '{"name": "x", "default": "ALLOW", "rules": []}', encoding="utf-8"
    )
    with pytest.raises(PolicyLoadError):
        ActionPolicy.from_file(tmp_path / "bad-default.json")

    example = ActionPolicy.from_file(
        Path(__file__).resolve().parents[2] / "examples" / "policies" / "m3-default.json"
    )
    assert example.default == "REQUIRE_HUMAN"
    assert example.name == "m3-default"

    policy = make_policy((allow("web.search"),))
    with pytest.raises(ValidationError):
        policy.rules = ()  # type: ignore[misc]
    with pytest.raises(ValidationError):
        policy.default = "ALLOW"  # type: ignore[misc]


# M3-09/M3-10
async def test_unknown_tool_is_unknown_and_requires_human() -> None:
    policy = make_policy((human("unknown"),))
    gate, calls = make_gate(web_tool(name="vendor.magic_action"), policy=policy)
    result = await gate.execute(
        "vendor.magic_action", {"query": "x"}, make_context(scope="research")
    )
    assert result.is_error is True
    assert "human approval unavailable" in result.data
    assert calls == []
    assert gate.audits[-1].action == "unknown"
    assert gate.audits[-1].decision == "REQUIRE_HUMAN"


# M3-11..M3-19
async def test_request_identity_canonical_args_and_digest() -> None:
    policy = make_policy((allow("web.search"),))
    gate, calls = make_gate(web_tool(), policy=policy)
    context = make_context(actor="worker", principal="user:real")
    first = await gate.execute(
        "web.search",
        {"query": "x", "principal": "user:forged", "agent": "agent:forged"},
        context,
    )
    second = await gate.execute("web.search", {"query": "x"}, context)
    third = await gate.execute("web.search", {"query": "x"}, context)
    assert first.is_error is not True
    assert second.is_error is not True
    assert third.is_error is not True
    assert len(calls) == 3
    audit_first, audit_second, audit_third = gate.audits[-3], gate.audits[-2], gate.audits[-1]
    assert audit_first.principal == "user:real"
    assert audit_first.agent == "agent:worker"
    assert audit_second.digest == audit_third.digest
    changed = await gate.execute("web.search", {"query": "y"}, context)
    assert changed.is_error is not True
    assert gate.audits[-1].digest != audit_third.digest

    catalog = ActionCatalog()
    resolution = catalog.resolve("email.send", {"to": "a@example.com"})
    assert resolution.action == "communication.send"
    assert resolution.resource == "email:a@example.com"
    unknown = catalog.resolve("vendor.magic_action", {"query": "x"})
    assert unknown.action == "unknown"
    assert unknown.resource == "tool:vendor.magic_action"


async def test_malformed_input_never_reaches_policy_or_execution() -> None:
    policy = make_policy((allow("web.search"),))
    gate, calls = make_gate(web_tool(), policy=policy)
    result = await gate.execute("web.search", {}, make_context())
    assert result.is_error is True
    assert "Invalid input" in result.data
    assert calls == []
    assert gate.audits == []


# M3-20..M3-25 (Atlas runtime integration)
async def test_atlas_native_searchable_dispatch_and_batch_are_gated(tmp_path: Path) -> None:
    policy = make_policy(
        (
            allow("control.search"),
            allow("control.dispatch"),
            allow("filesystem.read"),
            allow("web.search"),
        )
    )
    web = web_tool()
    unknown = web_tool(name="vendor.unknown")
    runtime = AtlasToolRuntime(
        "m3",
        external_tools=(web, unknown),
        action_policy=policy,
        action_reviewer=StaticActionReviewer("CONSISTENT"),
    )
    context = make_context(security_policy=ToolSecurityPolicy(workspace_root=str(tmp_path)))
    target = tmp_path / "note.txt"
    target.write_text("hello", encoding="utf-8")
    native = await runtime.executor.execute("file_read", {"path": str(target)}, context)
    assert native.is_error is not True

    control = await runtime.executor.execute("tool_search", {"query": "search"}, context)
    assert control.is_error is not True
    await runtime.search("search")
    dispatched = await runtime.executor.execute(
        "dispatch_action",
        {"tool_id": "web.search", "arguments": {"query": "x"}},
        context,
    )
    assert dispatched.is_error is not True
    assert runtime.action_safety_gate is not None
    assert any(audit.action == "control.search" for audit in runtime.action_safety_gate.audits)
    assert any(audit.action == "web.search" for audit in runtime.action_safety_gate.audits)

    batch = await runtime.executor.execute_batch(
        [
            BatchToolCall(id="one", name="web.search", input={"query": "x"}),
            BatchToolCall(id="two", name="vendor.unknown", input={"value": "x"}),
        ],
        context,
    )
    assert batch["one"].is_error is not True
    assert batch["two"].is_error is True


# M3-24/M3-25/M3-22
async def test_main_agent_and_known_mcp_action_use_the_same_gate() -> None:
    reviewer = StaticActionReviewer("CONSISTENT")
    main_gate, main_calls = make_gate(
        web_tool(),
        policy=make_policy((allow("web.search"),)),
        reviewer=reviewer,
    )
    main_context = make_context(actor="planner", scope=None)
    main_result = await main_gate.execute("web.search", {"query": "x"}, main_context)
    assert main_result.is_error is not True
    assert main_calls
    assert reviewer.calls == 1
    assert main_gate.audits[-1].agent == "agent:planner"

    catalog = ActionCatalog(
        DEFAULT_MAPPINGS
        + (
            ActionMapping(
                "vendor.magic_action",
                "web.search",
                "web",
                frozenset({"research"}),
            ),
        )
    )
    mcp_gate, mcp_calls = make_gate(
        web_tool(name="vendor.magic_action"),
        policy=make_policy((allow("web.search"),)),
        catalog=catalog,
    )
    mcp_result = await mcp_gate.execute(
        "vendor.magic_action", {"query": "x"}, make_context(scope="research")
    )
    assert mcp_result.is_error is not True
    assert len(mcp_calls) == 1
    assert mcp_gate.audits[-1].action == "web.search"


# M3-26/M3-27/M3-28
async def test_deny_and_rejected_hitl_never_execute() -> None:
    denied_gate, denied_calls = make_gate(web_tool(), policy=make_policy((deny("web.search"),)))
    denied = await denied_gate.execute("web.search", {"query": "x"}, make_context())
    assert denied.is_error is True
    assert denied_calls == []

    approval = ApprovalGate(False)
    human_gate, human_calls = make_gate(
        web_tool(),
        policy=make_policy((human("web.search"),)),
        approval_manager=approval_manager(approval),
    )
    rejected = await human_gate.execute("web.search", {"query": "x"}, make_context())
    assert rejected.is_error is True
    assert human_calls == []
    assert human_gate.audits[-1].human_outcome == "rejected"

    no_approver_gate, no_calls = make_gate(web_tool(), policy=make_policy((human("web.search"),)))
    no_approver = await no_approver_gate.execute("web.search", {"query": "x"}, make_context())
    assert no_approver.is_error is True
    assert no_calls == []


# M3-30..M3-34 (hard security)
async def test_hard_security_precedes_policy_and_cannot_be_overridden(tmp_path: Path) -> None:
    reviewer = StaticActionReviewer("CONSISTENT")
    approval = ApprovalGate(True)
    gate, calls = make_gate(
        web_tool(),
        policy=make_policy((allow("web.search"),)),
        reviewer=reviewer,
        approval_manager=approval_manager(approval),
    )
    hard_access = await gate.execute(
        "web.search",
        {"query": "x"},
        make_context(
            security_policy=ToolSecurityPolicy(denied_tools=("web.search",)),
        ),
    )
    assert hard_access.is_error is True
    assert "action_hard_denied" in hard_access.data
    assert calls == []
    assert reviewer.calls == 0
    assert approval.requests == []

    path_gate, path_calls = make_gate(
        ToolDefinition(
            name="file_read",
            description="read",
            input_model=PathInput,
            execute=web_tool().execute,
            side_effecting=False,
        ),
        policy=make_policy((allow("filesystem.read"),)),
        reviewer=reviewer,
        approval_manager=approval_manager(approval),
    )
    outside = await path_gate.execute(
        "file_read",
        {"path": str(tmp_path / "outside.txt")},
        make_context(
            security_policy=ToolSecurityPolicy(workspace_root=str(tmp_path / "workspace"))
        ),
    )
    assert outside.is_error is True
    assert "action_hard_denied" in outside.data
    assert path_calls == []
    assert reviewer.calls == 0

    shell_gate, shell_calls = make_gate(
        ToolDefinition(
            name="bash",
            description="shell",
            input_model=BashInput,
            execute=web_tool().execute,
            side_effecting=True,
        ),
        policy=make_policy((allow("shell.execute"),)),
        approval_manager=approval_manager(approval),
    )
    shell = await shell_gate.execute(
        "bash",
        {"command": "rm -rf /"},
        make_context(security_policy=ToolSecurityPolicy(allow_shell=False)),
    )
    assert shell.is_error is True
    assert "action_hard_denied" in shell.data
    assert shell_calls == []
    assert approval.requests == []


# M3-35..M3-42
async def test_scope_reviewer_only_runs_on_policy_allow_and_can_only_tighten() -> None:
    aligned_reviewer = StaticActionReviewer("SUSPICIOUS")
    gate, calls = make_gate(
        web_tool(),
        policy=make_policy((allow("web.search"),)),
        reviewer=aligned_reviewer,
    )
    aligned = await gate.execute("web.search", {"query": "x"}, make_context(scope="research"))
    assert aligned.is_error is not True
    assert aligned_reviewer.calls == 0

    mismatch_reviewer = StaticActionReviewer("CONSISTENT")
    gate, calls = make_gate(
        web_tool(),
        policy=make_policy((allow("web.search"),)),
        reviewer=mismatch_reviewer,
        approval_manager=approval_manager(ApprovalGate(True)),
    )
    mismatched = await gate.execute("web.search", {"query": "x"}, make_context(scope="coding"))
    assert mismatched.is_error is not True
    assert mismatch_reviewer.calls == 1
    assert gate.audits[-1].reviewer_verdict == "CONSISTENT"

    suspicious = StaticActionReviewer("SUSPICIOUS")
    approval = ApprovalGate(True)
    gate, _ = make_gate(
        web_tool(),
        policy=make_policy((allow("web.search"),)),
        reviewer=suspicious,
        approval_manager=approval_manager(approval),
    )
    result = await gate.execute("web.search", {"query": "x"}, make_context(scope="coding"))
    assert result.is_error is not True
    assert len(approval.requests) == 1
    assert gate.audits[-1].reviewer_verdict == "SUSPICIOUS"

    for _verdict in ("UNCERTAIN", "ERROR"):
        reviewer = StaticActionReviewer("UNCERTAIN")
        approval = ApprovalGate(True)
        gate, _ = make_gate(
            web_tool(),
            policy=make_policy((allow("web.search"),)),
            reviewer=reviewer,
            approval_manager=approval_manager(approval),
        )
        result = await gate.execute("web.search", {"query": "x"}, make_context(scope="coding"))
        assert result.is_error is not True
        assert len(approval.requests) == 1

    error_approval = ApprovalGate(True)
    error_gate, _ = make_gate(
        web_tool(),
        policy=make_policy((allow("web.search"),)),
        reviewer=RaisingReviewer(),
        approval_manager=approval_manager(error_approval),
    )
    error_result = await error_gate.execute(
        "web.search", {"query": "x"}, make_context(scope="coding")
    )
    assert error_result.is_error is not True
    assert error_gate.audits[-1].reviewer_verdict == "UNCERTAIN"
    assert len(error_approval.requests) == 1

    policy_deny_reviewer = StaticActionReviewer("CONSISTENT")
    gate, _ = make_gate(
        web_tool(),
        policy=make_policy((deny("web.search"),)),
        reviewer=policy_deny_reviewer,
    )
    denied = await gate.execute("web.search", {"query": "x"}, make_context(scope="coding"))
    assert denied.is_error is True
    assert policy_deny_reviewer.calls == 0

    policy_human_reviewer = StaticActionReviewer("CONSISTENT")
    gate, _ = make_gate(
        web_tool(),
        policy=make_policy((human("web.search"),)),
        reviewer=policy_human_reviewer,
        approval_manager=approval_manager(ApprovalGate(True)),
    )
    await gate.execute("web.search", {"query": "x"}, make_context(scope="coding"))
    assert policy_human_reviewer.calls == 0


# M3-43..M3-48
async def test_approval_digest_binding_modified_input_and_stale_denial() -> None:
    approval = ApprovalGate(True)
    gate, calls = make_gate(
        web_tool(),
        policy=make_policy((human("web.search"),)),
        approval_manager=approval_manager(approval),
    )
    result = await gate.execute("web.search", {"query": "x"}, make_context())
    assert result.is_error is not True
    request = approval.requests[0]
    assert request.context is not None
    assert request.context["call_id"] == gate.audits[-1].call_id
    assert request.context["digest"] == gate.audits[-1].digest
    assert request.context["canonical_arguments"] == {
        "query": "x",
        "principal": None,
        "agent": None,
    }

    modified_gate = ApprovalGate(True, modified_input={"query": "safe"})
    approved_gate, approved_calls = make_gate(
        web_tool(),
        policy=make_policy((human("web.search"),)),
        approval_manager=approval_manager(modified_gate),
    )
    modified = await approved_gate.execute("web.search", {"query": "unsafe"}, make_context())
    assert modified.is_error is not True
    assert approved_calls[-1]["query"] == "safe"
    last_context = modified_gate.requests[-1].context
    assert last_context is not None
    assert approved_calls[-1] == last_context["canonical_arguments"]
    assert len(modified_gate.requests) == 2
    first_context = modified_gate.requests[0].context
    assert first_context is not None
    assert first_context["digest"] != last_context["digest"]

    class StaleManager:
        def __init__(self) -> None:
            self.history = [
                (
                    ApprovalRequest(
                        id="old",
                        type="tool_call",
                        agent="worker",
                        description="old",
                        context={"digest": "old"},
                        created_at=datetime.now(UTC),
                    ),
                    ApprovalResponse(
                        request_id="old",
                        approved=True,
                        responded_at=datetime.now(UTC),
                    ),
                )
            ]

        async def check_and_request(self, **kwargs):
            del kwargs
            return ApprovalResponse(request_id="old", approved=True, responded_at=datetime.now(UTC))

    stale_gate, stale_calls = make_gate(
        web_tool(),
        policy=make_policy((human("web.search"),)),
        approval_manager=StaleManager(),  # type: ignore[arg-type]
    )
    stale = await stale_gate.execute("web.search", {"query": "x"}, make_context())
    assert stale.is_error is True
    assert "human approval unavailable" in stale.data
    assert stale_calls == []


# M3-49..M3-53
async def test_idempotency_ordering_replay_and_conflict() -> None:
    class CountingStore(InMemoryToolIdempotencyStore):
        def __init__(self) -> None:
            super().__init__()
            self.claims = 0

        async def claim(self, tool_name: str, key: str, fingerprint: str):
            self.claims += 1
            return await super().claim(tool_name, key, fingerprint)

    store = CountingStore()
    denied_gate, denied_calls = make_gate(
        email_tool(),
        policy=make_policy((deny("communication.send"),)),
        idempotency_store=store,
    )
    denied = await denied_gate.execute(
        "email.send",
        {"to": "a@example.com", "idempotency_key": "key-1"},
        make_context(),
    )
    assert denied.is_error is True
    assert denied_calls == []
    assert store.claims == 0

    rejected_store = CountingStore()
    rejected_gate, rejected_calls = make_gate(
        email_tool(),
        policy=make_policy((human("communication.send"),)),
        approval_manager=approval_manager(ApprovalGate(False)),
        idempotency_store=rejected_store,
    )
    rejected = await rejected_gate.execute(
        "email.send",
        {"to": "a@example.com", "idempotency_key": "key-1"},
        make_context(),
    )
    assert rejected.is_error is True
    assert rejected_calls == []
    assert rejected_store.claims == 0

    approved_store = CountingStore()
    approved_approval = ApprovalGate(True)
    gate, calls = make_gate(
        email_tool(),
        policy=make_policy((human("communication.send"),)),
        approval_manager=approval_manager(approved_approval),
        idempotency_store=approved_store,
    )
    first = await gate.execute(
        "email.send",
        {"to": "a@example.com", "idempotency_key": "key-2"},
        make_context(),
    )
    assert first.is_error is not True
    assert len(calls) == 1
    assert approved_store.claims == 1

    replay = await gate.execute(
        "email.send",
        {"to": "a@example.com", "idempotency_key": "key-2"},
        make_context(),
    )
    assert replay.is_error is not True
    assert len(calls) == 1
    assert approved_store.claims == 2

    conflict = await gate.execute(
        "email.send",
        {"to": "b@example.com", "idempotency_key": "key-2"},
        make_context(),
    )
    assert conflict.is_error is True
    assert "conflict" in conflict.data
    assert len(calls) == 1


# E2E
async def test_e2e_safe_research_scope_anomaly_external_and_unknown(tmp_path: Path) -> None:
    github = ToolDefinition(
        name="github.issue.get",
        description="read issue",
        input_model=GithubInput,
        execute=web_tool().execute,
        side_effecting=False,
    )
    research_reviewer = StaticActionReviewer("SUSPICIOUS")
    research_gate, _ = make_gate(
        github,
        policy=make_policy((allow("github.issue.read"),)),
        reviewer=research_reviewer,
        approval_manager=approval_manager(ApprovalGate(True)),
    )
    research = await research_gate.execute(
        "github.issue.get", {"repo": "atlasos"}, make_context(scope="research")
    )
    assert research.is_error is not True
    assert research_reviewer.calls == 0
    assert research_gate.audits[-1].action == "github.issue.read"

    safe_gate, _ = make_gate(
        web_tool(name="web.search"),
        policy=make_policy((allow("web.search"),)),
        reviewer=StaticActionReviewer("SUSPICIOUS"),
        approval_manager=approval_manager(ApprovalGate(True)),
    )
    safe = await safe_gate.execute("web.search", {"query": "x"}, make_context(scope="research"))
    assert safe.is_error is not True
    assert safe_gate.audits[-1].reviewer_verdict is None

    anomaly_gate, _ = make_gate(
        web_tool(name="web.search"),
        policy=make_policy((allow("web.search"),)),
        reviewer=StaticActionReviewer("CONSISTENT"),
        approval_manager=approval_manager(ApprovalGate(True)),
    )
    anomaly = await anomaly_gate.execute(
        "web.search", {"query": "pytest"}, make_context(scope="coding")
    )
    assert anomaly.is_error is not True
    assert anomaly_gate.audits[-1].reviewer_verdict == "CONSISTENT"

    email_gate, email_calls = make_gate(
        email_tool(),
        policy=make_policy((human("communication.send"),)),
        approval_manager=approval_manager(ApprovalGate(True)),
    )
    email = await email_gate.execute(
        "email.send",
        {"to": "a@example.com", "idempotency_key": "k"},
        make_context(scope="communication"),
    )
    assert email.is_error is not True
    assert len(email_calls) == 1

    unknown_tool = ToolDefinition(
        name="vendor.magic_action",
        description="unknown",
        input_model=GenericInput,
        execute=web_tool().execute,
        side_effecting=False,
    )
    unknown_gate, unknown_calls = make_gate(
        unknown_tool,
        policy=make_policy(),
        approval_manager=approval_manager(ApprovalGate(True)),
    )
    unknown = await unknown_gate.execute("vendor.magic_action", {"value": "x"}, make_context())
    assert unknown.is_error is not True
    assert unknown_gate.audits[-1].action == "unknown"
    assert unknown_gate.audits[-1].human_outcome == "approved"
    assert len(unknown_calls) == 1
