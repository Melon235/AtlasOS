"""Stateless one-shot Decision Reviewer built on the AnyCode runner/provider seam."""

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

from atlasos.delegation.models import ReviewerInput, ReviewerResult, ReviewerVerdict


class ReviewerOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    verdict: Literal["AGREE", "DISAGREE", "UNCERTAIN"]
    reason_codes: tuple[str, ...] = ()


class DecisionReviewer(Protocol):
    async def review(self, reviewer_input: ReviewerInput) -> ReviewerResult: ...


class StaticReviewer:
    """Deterministic reviewer used by tests and experiments."""

    def __init__(
        self,
        verdict: ReviewerVerdict = "AGREE",
        reason_codes: tuple[str, ...] = (),
    ) -> None:
        self.verdict: ReviewerVerdict = verdict
        self.reason_codes = reason_codes
        self.calls = 0

    async def review(self, reviewer_input: ReviewerInput) -> ReviewerResult:
        del reviewer_input
        self.calls += 1
        return ReviewerResult(verdict=self.verdict, reason_codes=self.reason_codes)


class AnyCodeDecisionReviewer:
    """One-shot reviewer with an empty tool registry and no grant/runtime reference."""

    def __init__(
        self,
        adapter: LLMAdapter,
        *,
        model: str,
        timeout_seconds: float = 15.0,
        max_tokens: int = 160,
    ) -> None:
        self._adapter = adapter
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._max_tokens = max_tokens

    async def review(self, reviewer_input: ReviewerInput) -> ReviewerResult:
        registry = ToolRegistry()
        executor = ToolExecutor(registry)
        options = RunnerOptions(
            model=self._model,
            system_prompt=(
                "You are a stateless delegation decision reviewer. Assess whether the declared "
                "intent, requested scopes, and task are consistent and proportionate. Return "
                "only the structured output. You cannot approve grants or change routes."
            ),
            max_turns=1,
            max_tokens=self._max_tokens,
            temperature=0.0,
            allowed_tools=[],
            agent_name="delegation_reviewer",
            agent_role="stateless delegation reviewer",
        )
        runner = AgentRunner(
            self._adapter,
            registry,
            executor,
            options,
            output_schema=ReviewerOutput,
        )
        prompt = (
            "Review this delegation input and return AGREE, DISAGREE, or UNCERTAIN with reason "
            "codes.\n" + json.dumps(reviewer_input.model_dump(mode="json"), sort_keys=True)
        )
        try:
            result = await asyncio.wait_for(
                runner.run([LLMMessage(role="user", content=[TextBlock(text=prompt)])]),
                timeout=self._timeout_seconds,
            )
            parsed = parse_structured_output(result.output, ReviewerOutput)
        except Exception:
            return ReviewerResult(verdict="UNCERTAIN", reason_codes=("reviewer_error",))
        if parsed is None:
            return ReviewerResult(verdict="UNCERTAIN", reason_codes=("reviewer_invalid_output",))
        return ReviewerResult(verdict=parsed.verdict, reason_codes=parsed.reason_codes)
