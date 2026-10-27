"""One global pre-invoke action safety gate.

The gate wraps only AnyCode's ToolExecutor; it never executes a tool itself. Validation, AnyCode
hard security, policy, reviewer, external policy, and digest-bound HITL all run before the inner
executor can claim idempotency or invoke the tool.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from anycode.hitl.approval import ApprovalManager
from anycode.identity.policy import PolicyEnforcer, PolicyRequest
from anycode.security.policy import (
    ToolSecurityError,
    check_tool_access,
    resolve_tool_path,
    validate_shell_command,
)
from anycode.security.redaction import safe_exception_message
from anycode.tools.executor import ToolExecutor
from anycode.tools.registry import ToolRegistry
from anycode.types import BatchToolCall, ToolResult, ToolUseContext

from atlasos.actions.catalog import ActionCatalog, ActionResolution
from atlasos.actions.models import (
    ActionAudit,
    ActionContext,
    ActionDecision,
    ActionRequest,
    ActionReviewRequest,
    ActionReviewResult,
    PolicyResult,
    ScopeAlignment,
)
from atlasos.actions.policy import ActionPolicy
from atlasos.actions.reviewer import ActionReviewer

logger = logging.getLogger(__name__)

_FILE_TOOLS = frozenset({"file_read", "file_write", "file_edit", "grep", "list_files"})


@dataclass(frozen=True)
class _Evaluation:
    request: ActionRequest
    policy: PolicyResult
    decision: ActionDecision


class ActionSafetyGate:
    """Global gate for every real tool call in a configured Atlas runtime."""

    def __init__(
        self,
        executor: ToolExecutor,
        registry: ToolRegistry,
        *,
        policy: ActionPolicy,
        catalog: ActionCatalog | None = None,
        reviewer: ActionReviewer | None = None,
        approval_manager: ApprovalManager | None = None,
        external_policy: PolicyEnforcer | None = None,
        max_modified_attempts: int = 2,
    ) -> None:
        self._executor = executor
        self._registry = registry
        self.policy = policy
        self.catalog = catalog or ActionCatalog()
        self.reviewer = reviewer
        self.approval_manager = approval_manager
        self.external_policy = external_policy
        self.max_modified_attempts = max_modified_attempts
        self.audits: list[ActionAudit] = []

    async def execute(
        self,
        tool_name: str,
        raw_input: dict[str, Any],
        context: ToolUseContext,
        *,
        idempotency_key: str | None = None,
    ) -> ToolResult:
        tool = self._registry.get(tool_name)
        if tool is None:
            # Let AnyCode produce its canonical unregistered-tool failure.
            return await self._executor.execute(
                tool_name, raw_input, context, idempotency_key=idempotency_key
            )

        try:
            check_tool_access(tool_name, context.security_policy)
        except ToolSecurityError as error:
            return _failure(f"action_hard_denied: {safe_exception_message(error)}")

        try:
            validated = tool.input_model.model_validate(raw_input)
        except Exception as error:
            return _failure(
                f'Invalid input for tool "{tool_name}": {safe_exception_message(error)}'
            )
        canonical_arguments = validated.model_dump(mode="json")

        try:
            self._hard_precheck(tool_name, canonical_arguments, context)
        except ToolSecurityError as error:
            return _failure(f"action_hard_denied: {safe_exception_message(error)}")

        if context.execution_context is None:
            return _failure("action_denied: trusted ExecutionContext is required")

        current_arguments = canonical_arguments
        for attempt in range(self.max_modified_attempts + 1):
            call_id = uuid.uuid4().hex
            evaluation = await self._evaluate(
                tool_name,
                current_arguments,
                context,
                call_id,
            )
            if evaluation.decision.decision == "DENY":
                self._audit(evaluation, context, human_outcome=None, executed=False)
                return _failure(
                    "action_denied: " + ", ".join(evaluation.decision.reason_codes or ("denied",))
                )

            if evaluation.decision.decision == "REQUIRE_HUMAN":
                response = await self._request_approval(evaluation, context)
                if response is None:
                    self._audit(
                        evaluation,
                        context,
                        human_outcome="unavailable_or_rejected",
                        executed=False,
                    )
                    return _failure("action_denied: human approval unavailable or rejected")
                if not response.approved:
                    self._audit(
                        evaluation,
                        context,
                        human_outcome="rejected",
                        executed=False,
                    )
                    return _failure("action_denied: human approval rejected")
                if response.modified_input:
                    try:
                        modified = tool.input_model.model_validate(response.modified_input)
                    except Exception as error:
                        self._audit(
                            evaluation,
                            context,
                            human_outcome="modified_invalid",
                            executed=False,
                        )
                        return _failure(
                            "action_denied: modified input is invalid: "
                            + safe_exception_message(error)
                        )
                    current_arguments = modified.model_dump(mode="json")
                    self._audit(
                        evaluation,
                        context,
                        human_outcome="modified_reauthorize",
                        executed=False,
                    )
                    if attempt >= self.max_modified_attempts:
                        return _failure("action_denied: modified input reauthorization limit")
                    continue
                self._audit(
                    evaluation,
                    context,
                    human_outcome="approved",
                    executed=True,
                )
                return await self._execute(
                    tool_name,
                    current_arguments,
                    context,
                    idempotency_key=idempotency_key,
                )

            # ALLOW, with optional scope reviewer already applied in _evaluate.
            self._audit(evaluation, context, human_outcome=None, executed=True)
            return await self._execute(
                tool_name,
                current_arguments,
                context,
                idempotency_key=idempotency_key,
            )

        return _failure("action_denied: reauthorization exhausted")

    async def execute_batch(
        self, calls: list[BatchToolCall], context: ToolUseContext
    ) -> dict[str, ToolResult]:
        results: dict[str, ToolResult] = {}

        async def run(call: BatchToolCall) -> tuple[str, ToolResult]:
            return call.id, await self.execute(
                call.name, call.input, context, idempotency_key=call.id
            )

        import asyncio

        pairs = await asyncio.gather(*(run(call) for call in calls))
        results.update(pairs)
        return results

    async def _evaluate(
        self,
        tool_name: str,
        canonical_arguments: dict[str, Any],
        context: ToolUseContext,
        call_id: str,
    ) -> _Evaluation:
        execution_context = context.execution_context
        assert execution_context is not None
        principal = execution_context.principal
        agent = _canonical_agent(context.agent.name)
        resolution = self.catalog.resolve(tool_name, canonical_arguments)
        action_context = _action_context(context)
        base = ActionRequest(
            principal=principal,
            agent=agent,
            action=resolution.action,
            resource=resolution.resource,
            context=action_context,
            tool_name=tool_name,
            call_id=call_id,
            canonical_arguments=canonical_arguments,
        )
        policy = self.policy.decide(base)
        alignment = _scope_alignment(resolution, action_context.operating_scope)
        decision = ActionDecision(
            decision=policy.decision,
            reason_codes=policy.reason_codes,
            matched_rule=policy.matched_rule,
            policy_name=policy.policy_name,
            scope_alignment=alignment,
        )

        reviewer_verdict = None
        if policy.decision == "ALLOW" and alignment != "ALIGNED":
            reviewed = await self._review(base, resolution, action_context, context)
            reviewer_verdict = reviewed.verdict
            if reviewed.verdict != "CONSISTENT":
                decision = decision.model_copy(
                    update={
                        "decision": "REQUIRE_HUMAN",
                        "reason_codes": (
                            *decision.reason_codes,
                            *reviewed.reason_codes,
                            "scope_review_escalated",
                        ),
                    }
                )
            else:
                decision = decision.model_copy(
                    update={"reason_codes": (*decision.reason_codes, *reviewed.reason_codes)}
                )

        external_outcome = None
        if self.external_policy is not None and decision.decision != "DENY":
            external_allowed, external_outcome, external_codes = await self._external_policy(
                base, context
            )
            if not external_allowed:
                decision = decision.model_copy(
                    update={
                        "decision": "DENY",
                        "reason_codes": (*decision.reason_codes, *external_codes),
                        "external_policy_outcome": external_outcome,
                    }
                )
            else:
                decision = decision.model_copy(update={"external_policy_outcome": external_outcome})

        request = base.model_copy(update={"digest": _digest(base, decision)})
        decision = decision.model_copy(update={"reviewer_verdict": reviewer_verdict})
        return _Evaluation(request=request, policy=policy, decision=decision)

    async def _review(
        self,
        request: ActionRequest,
        resolution: ActionResolution,
        action_context: ActionContext,
        context: ToolUseContext,
    ) -> ActionReviewResult:
        del resolution
        if self.reviewer is None:
            return ActionReviewResult(verdict="UNCERTAIN", reason_codes=("reviewer_unavailable",))
        review_request = ActionReviewRequest(
            principal=request.principal,
            agent=request.agent,
            action=request.action,
            resource=request.resource,
            task_id=action_context.task_id,
            operating_scope=action_context.operating_scope,
            worker_profile=action_context.worker_profile,
            canonical_arguments=request.canonical_arguments,
        )
        try:
            return await self.reviewer.review(review_request)
        except Exception:
            return ActionReviewResult(verdict="UNCERTAIN", reason_codes=("reviewer_error",))

    async def _external_policy(
        self, request: ActionRequest, context: ToolUseContext
    ) -> tuple[bool, str, tuple[str, ...]]:
        assert context.execution_context is not None
        assert self.external_policy is not None
        policy_request = PolicyRequest(
            run_id=request.context.run_id or request.call_id,
            task_id=request.context.task_id,
            action=request.action,
            resource=request.resource,
            boundary="tool",
            context=context.execution_context,
            correlation_id=request.call_id,
            input={
                "tool_name": request.tool_name,
                "canonical_arguments": request.canonical_arguments,
            },
        )
        try:
            result = await self.external_policy.enforce(policy_request)
        except Exception:
            return False, "error", ("external_policy_error",)
        if not result.allowed:
            return (
                False,
                "deny",
                tuple(result.decision.reason_codes) or ("external_policy_denied",),
            )
        return True, "allow", ()

    async def _request_approval(self, evaluation: _Evaluation, context: ToolUseContext):
        manager = self.approval_manager
        if manager is None:
            return None
        approval_context = {
            "call_id": evaluation.request.call_id,
            "digest": evaluation.request.digest,
            "tool_name": evaluation.request.tool_name,
            "action": evaluation.request.action,
            "resource": evaluation.request.resource,
            "canonical_arguments": evaluation.request.canonical_arguments,
            "policy_decision": evaluation.decision.decision,
            "matched_rule": evaluation.decision.matched_rule,
        }
        try:
            response = await manager.check_and_request(
                request_type="tool_call",
                agent=context.agent.name,
                description=(
                    f"M3 approval required for {evaluation.request.action} on "
                    f"{evaluation.request.resource}"
                ),
                context=approval_context,
            )
        except Exception:
            return None
        if response is None:
            return None
        history = getattr(manager, "history", [])
        if not history:
            return None
        last_request, last_response = history[-1]
        if (
            last_request.id != response.request_id
            or last_response.request_id != response.request_id
            or last_request.context != approval_context
        ):
            return None
        return response

    async def _execute(
        self,
        tool_name: str,
        canonical_arguments: dict[str, Any],
        context: ToolUseContext,
        *,
        idempotency_key: str | None,
    ) -> ToolResult:
        return await self._executor.execute(
            tool_name,
            canonical_arguments,
            context,
            idempotency_key=idempotency_key,
        )

    def _hard_precheck(
        self, tool_name: str, arguments: dict[str, Any], context: ToolUseContext
    ) -> None:
        if tool_name in _FILE_TOOLS:
            path = arguments.get("path")
            if path is not None:
                resolve_tool_path(str(path), context)
        if tool_name == "bash":
            validate_shell_command(str(arguments.get("command", "")), context)

    def _audit(
        self,
        evaluation: _Evaluation,
        context: ToolUseContext,
        *,
        human_outcome: str | None,
        executed: bool,
    ) -> None:
        audit = ActionAudit(
            call_id=evaluation.request.call_id,
            principal=evaluation.request.principal,
            agent=evaluation.request.agent,
            tool_name=evaluation.request.tool_name,
            action=evaluation.request.action,
            resource=evaluation.request.resource,
            digest=evaluation.request.digest,
            decision=evaluation.decision.decision,
            task_id=evaluation.request.context.task_id,
            worker_id=evaluation.request.context.worker_id,
            profile_id=evaluation.request.context.profile_id,
            parent_agent=evaluation.request.context.parent_agent,
            operating_scope=evaluation.request.context.operating_scope,
            matched_rule=evaluation.decision.matched_rule,
            reviewer_verdict=evaluation.decision.reviewer_verdict,
            human_outcome=human_outcome,
            executed=executed,
            reason_codes=evaluation.decision.reason_codes,
            policy_name=evaluation.decision.policy_name,
        )
        self.audits.append(audit)
        logger.info(
            "m3_action_decision action=%s tool=%s agent=%s principal=%s decision=%s "
            "matched=%s call_id=%s digest=%s executed=%s",
            audit.action,
            audit.tool_name,
            audit.agent,
            audit.principal,
            audit.decision,
            audit.matched_rule,
            audit.call_id,
            audit.digest,
            audit.executed,
        )


def _canonical_agent(name: str) -> str:
    value = name.strip()
    if value.startswith("agent:"):
        return value
    return f"agent:{value}"


def _action_context(context: ToolUseContext) -> ActionContext:
    execution_context = context.execution_context
    assert execution_context is not None
    attributes = execution_context.attributes
    nested = attributes.get("atlas_worker")
    worker = nested if isinstance(nested, dict) else {}

    def attr(key: str) -> str | None:
        value = attributes.get(key)
        if value is None:
            value = worker.get(key)
        return str(value) if value is not None else None

    def worker_scope() -> str | None:
        flat = attr("operating_scope")
        if flat is not None:
            return flat
        scopes = worker.get("operating_scope")
        if isinstance(scopes, list) and scopes:
            return str(scopes[0])
        return None

    return ActionContext(
        run_id=attr("run_id"),
        task_id=attr("task_id"),
        worker_profile=attr("worker_profile") or attr("profile_id"),
        worker_id=attr("worker_id"),
        profile_id=attr("profile_id"),
        parent_agent=attr("parent_agent"),
        operating_scope=worker_scope(),
        tenant=execution_context.tenant_scope,
        classification=execution_context.classification,
        trace_id=execution_context.trace_id,
    )


def _scope_alignment(resolution: ActionResolution, operating_scope: str | None) -> ScopeAlignment:
    if not resolution.expected_scopes:
        return "ALIGNED"
    if not operating_scope:
        return "UNKNOWN"
    return "ALIGNED" if operating_scope in resolution.expected_scopes else "MISMATCH"


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _digest(request: ActionRequest, decision: ActionDecision) -> str:
    payload = {
        "principal": request.principal,
        "agent": request.agent,
        "action": request.action,
        "resource": request.resource,
        "canonical_arguments": request.canonical_arguments,
        "decision": decision.decision,
        "matched_rule": decision.matched_rule,
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _failure(message: str, *, retry_safe: bool = False) -> ToolResult:
    return ToolResult(data=message, is_error=True, retry_safe=retry_safe)
