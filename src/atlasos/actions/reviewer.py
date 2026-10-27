"""Stateless semantic anomaly reviewer for M3; it may only tighten an existing ALLOW."""

from __future__ import annotations

import asyncio
import json
from typing import Literal, Protocol

from anycode.core.runner import AgentRunner
from anycode.structured.output import parse_structured_output
from anycode.tools.executor import ToolExecutor
from anycode.tools.registry import ToolRegistry
from anycode.types import LLMAdapter, LLMMessage, RunnerOptions, TextBlock
from pydantic import BaseModel, ConfigDict

from atlasos.actions.models import (
    ActionReviewRequest,
    ActionReviewResult,
    ReviewerVerdict,
)


class ActionReviewerOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    verdict: Literal["CONSISTENT", "SUSPICIOUS", "UNCERTAIN"]
    reason_codes: tuple[str, ...] = ()


class ActionReviewer(Protocol):
    async def review(self, request: ActionReviewRequest) -> ActionReviewResult: ...


class StaticActionReviewer:
    def __init__(
        self,
        verdict: ReviewerVerdict = "CONSISTENT",
        reason_codes: tuple[str, ...] = (),
    ) -> None:
        self.verdict: ReviewerVerdict = verdict
        self.reason_codes = reason_codes
        self.calls = 0

    async def review(self, request: ActionReviewRequest) -> ActionReviewResult:
        del request
        self.calls += 1
        return ActionReviewResult(verdict=self.verdict, reason_codes=self.reason_codes)


class AnyCodeActionReviewer:
    """One-shot AnyCode runner; empty registry, no approval/gate reference."""

    def __init__(
        self,
        adapter: LLMAdapter,
        *,
        model: str,
        timeout_seconds: float = 10.0,
        max_tokens: int = 120,
    ) -> None:
        self._adapter = adapter
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._max_tokens = max_tokens

    async def review(self, request: ActionReviewRequest) -> ActionReviewResult:
        registry = ToolRegistry()
        executor = ToolExecutor(registry)
        options = RunnerOptions(
            model=self._model,
            system_prompt=(
                "You are a stateless task/action consistency reviewer. Judge whether the "
                "concrete tool action is consistent with the declared worker scope. Return only "
                "the structured output. You cannot authorize or alter the action."
            ),
            max_turns=1,
            max_tokens=self._max_tokens,
            temperature=0.0,
            allowed_tools=[],
            agent_name="action_reviewer",
            agent_role="stateless action reviewer",
        )
        runner = AgentRunner(
            self._adapter,
            registry,
            executor,
            options,
            output_schema=ActionReviewerOutput,
        )
        prompt = (
            "Review this action for scope consistency. Return CONSISTENT, SUSPICIOUS, or "
            "UNCERTAIN with reason codes.\n"
            + json.dumps(request.model_dump(mode="json"), sort_keys=True)
        )
        try:
            result = await asyncio.wait_for(
                runner.run([LLMMessage(role="user", content=[TextBlock(text=prompt)])]),
                timeout=self._timeout_seconds,
            )
            parsed = parse_structured_output(result.output, ActionReviewerOutput)
        except Exception:
            return ActionReviewResult(verdict="UNCERTAIN", reason_codes=("reviewer_error",))
        if parsed is None:
            return ActionReviewResult(
                verdict="UNCERTAIN", reason_codes=("reviewer_invalid_output",)
            )
        return ActionReviewResult(verdict=parsed.verdict, reason_codes=parsed.reason_codes)
