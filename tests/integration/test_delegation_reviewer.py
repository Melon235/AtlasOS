"""M2 reviewer seam: AnyCode runner, structured output, no normal tools."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterable

from anycode.types import (
    LLMChatOptions,
    LLMMessage,
    LLMResponse,
    LLMStreamOptions,
    StreamEvent,
    TextBlock,
    TokenUsage,
    ToolUseBlock,
)

from atlasos.delegation import AnyCodeDecisionReviewer, ReviewerInput


class StructuredReviewerAdapter:
    def __init__(self, verdict: str = "AGREE", *, delay: float = 0.0) -> None:
        self.verdict = verdict
        self.delay = delay
        self.seen_tools: list[tuple[str, ...]] = []

    @property
    def name(self) -> str:
        return "test-reviewer"

    async def chat(self, messages: list[LLMMessage], options: LLMChatOptions) -> LLMResponse:
        del messages
        self.seen_tools.append(tuple(tool.name for tool in options.tools or ()))
        if self.delay:
            await asyncio.sleep(self.delay)
        return LLMResponse(
            id="review-response",
            content=[
                ToolUseBlock(
                    id="structured-1",
                    name="_structured_output",
                    input={"verdict": self.verdict, "reason_codes": ["test"]},
                )
            ],
            model=options.model or "fake",
            stop_reason="tool_use",
            usage=TokenUsage(input_tokens=10, output_tokens=5),
        )

    def stream(
        self,
        messages: list[LLMMessage],
        options: LLMStreamOptions,
    ) -> AsyncIterable[StreamEvent]:
        async def _generate() -> AsyncIterable[StreamEvent]:
            yield StreamEvent(type="done", data=await self.chat(messages, options))

        return _generate()


class InvalidStructuredAdapter(StructuredReviewerAdapter):
    async def chat(self, messages: list[LLMMessage], options: LLMChatOptions) -> LLMResponse:
        del messages
        self.seen_tools.append(tuple(tool.name for tool in options.tools or ()))
        return LLMResponse(
            id="bad",
            content=[TextBlock(text="not structured")],
            model=options.model or "fake",
            stop_reason="end_turn",
            usage=TokenUsage(input_tokens=10, output_tokens=5),
        )


def reviewer_input() -> ReviewerInput:
    return ReviewerInput(
        principal="user:wang",
        current_actor="agent:planner",
        effective_scopes=("github.read",),
        delegatee="agent:github_worker",
        intent="research",
        requested_scopes=("github.read",),
        task_summary="read issues",
        matched_rules=("research.github.read",),
        router_gates=(),
        router_reason_codes=("research_read",),
    )


async def test_reviewer_uses_anycode_structured_runner_without_normal_tools() -> None:
    adapter = StructuredReviewerAdapter("AGREE")
    reviewer = AnyCodeDecisionReviewer(adapter, model="fake-model")
    result = await reviewer.review(reviewer_input())
    assert result.verdict == "AGREE"
    assert result.reason_codes == ("test",)
    assert adapter.seen_tools
    # Only AnyCode's structured-output tool is exposed; no operational tools.
    assert adapter.seen_tools[0] == ("_structured_output",)


async def test_reviewer_disagreement_and_invalid_output_are_safe() -> None:
    disagree = AnyCodeDecisionReviewer(StructuredReviewerAdapter("DISAGREE"), model="fake-model")
    result = await disagree.review(reviewer_input())
    assert result.verdict == "DISAGREE"

    invalid = AnyCodeDecisionReviewer(InvalidStructuredAdapter(), model="fake-model")
    invalid_result = await invalid.review(reviewer_input())
    assert invalid_result.verdict == "UNCERTAIN"
    assert invalid_result.reason_codes == ("reviewer_invalid_output",)


async def test_reviewer_timeout_is_uncertain() -> None:
    reviewer = AnyCodeDecisionReviewer(
        StructuredReviewerAdapter("AGREE", delay=0.05),
        model="fake-model",
        timeout_seconds=0.001,
    )
    result = await reviewer.review(reviewer_input())
    assert result.verdict == "UNCERTAIN"
    assert result.reason_codes == ("reviewer_error",)
