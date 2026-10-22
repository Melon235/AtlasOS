"""Opt-in live DeepSeek test.

Never part of the default suite: requires ``ATLASOS_RUN_LIVE_TESTS=1`` and ``DEEPSEEK_API_KEY``.
Cache hits are best-effort, so this test never asserts a positive hit count.
"""

from __future__ import annotations

import os

import pytest
from anycode.types import LLMChatOptions, LLMMessage, TextBlock

from atlasos.integrations.deepseek import DeepSeekAdapter

pytestmark = pytest.mark.live


def _live_enabled() -> bool:
    return os.environ.get("ATLASOS_RUN_LIVE_TESTS") == "1" and bool(
        os.environ.get("DEEPSEEK_API_KEY")
    )


@pytest.mark.skipif(_live_enabled() is False, reason="live tests are opt-in")
async def test_live_deepseek_call_reports_provider_usage() -> None:
    captured: list = []
    adapter = DeepSeekAdapter.from_env(on_call=captured.append)

    response = await adapter.chat(
        [LLMMessage(role="user", content=[TextBlock(text="Reply with the single word: ok")])],
        LLMChatOptions(model="deepseek-chat", max_tokens=16, temperature=0),
    )

    assert response.content
    assert captured, "adapter must emit one normalized observation per call"
    observation = captured[0]
    usage = observation.usage
    assert usage.prompt_tokens > 0
    assert usage.completion_tokens > 0
    assert usage.cache_hit_tokens is None or usage.cache_hit_tokens >= 0
    assert len(observation.stable_prefix_hash) == 64
    assert len(observation.full_prompt_hash) == 64
