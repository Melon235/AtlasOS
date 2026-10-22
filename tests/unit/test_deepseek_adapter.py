"""DeepSeek adapter tests against a stubbed OpenAI-compatible client (no network)."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from anycode.plugins.registry import get_provider_factory
from anycode.types import LLMChatOptions, LLMMessage, TextBlock

from atlasos.integrations.deepseek import (
    DeepSeekAdapter,
    _load_dotenv,
    register_deepseek_provider,
)
from atlasos.metrics.fingerprint import full_prompt_hash, stable_prefix_hash


def _completion(usage: Any) -> Any:
    return SimpleNamespace(
        id="chatcmpl-test",
        model="deepseek-chat",
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content="ok", tool_calls=None),
                finish_reason="stop",
            )
        ],
        usage=usage,
    )


def _usage(**fields: Any) -> Any:
    values: dict[str, Any] = {
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "prompt_tokens_details": None,
    }
    values.update(fields)
    return SimpleNamespace(**values, model_dump=lambda: dict(values))


class _Completions:
    def __init__(self, completion: Any) -> None:
        self._completion = completion
        self.kwargs: dict[str, Any] | None = None

    async def create(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        return self._completion


class _Client:
    def __init__(self, completion: Any) -> None:
        self.chat = SimpleNamespace(completions=_Completions(completion))


def _messages() -> list[LLMMessage]:
    return [LLMMessage(role="user", content=[TextBlock(text="hi")])]


async def test_chat_preserves_provider_cache_split() -> None:
    captured = []
    completion = _completion(_usage(prompt_cache_hit_tokens=40, prompt_cache_miss_tokens=60))
    adapter = DeepSeekAdapter(client=_Client(completion), on_call=captured.append)

    response = await adapter.chat(_messages(), LLMChatOptions(model="deepseek-chat"))

    assert adapter.name == "deepseek"
    assert len(captured) == 1
    assert captured[0].usage.cache_hit_tokens == 40
    assert captured[0].usage.cache_miss_tokens == 60
    assert captured[0].usage.cache_hit_ratio == pytest.approx(0.4)
    # AnyCode's TokenUsage receives the same split.
    assert response.usage.cache_read_input_tokens == 40
    assert response.usage.input_tokens == 60
    assert response.usage.output_tokens == 20
    block = response.content[0]
    assert isinstance(block, TextBlock)
    assert block.text == "ok"


async def test_observation_carries_deterministic_fingerprints() -> None:
    captured = []
    adapter = DeepSeekAdapter(
        client=_Client(_completion(_usage(prompt_cache_hit_tokens=1, prompt_cache_miss_tokens=99))),
        on_call=captured.append,
    )

    await adapter.chat(
        _messages(),
        LLMChatOptions(model="deepseek-chat", system_prompt="You are terse."),
    )

    observation = captured[0]
    assert observation.stable_prefix_hash == stable_prefix_hash("You are terse.")
    assert observation.full_prompt_hash == full_prompt_hash("You are terse.", _messages())
    # Fingerprints are metadata, never prompt content.
    assert "stable_prefix_hash" not in " ".join(
        block.text
        for message in _messages()
        for block in message.content
        if isinstance(block, TextBlock)
    )


async def test_missing_cache_fields_stay_none_in_metrics() -> None:
    captured = []
    adapter = DeepSeekAdapter(client=_Client(_completion(_usage())), on_call=captured.append)

    response = await adapter.chat(_messages(), LLMChatOptions(model="deepseek-chat"))

    assert captured[0].usage.cache_hit_tokens is None
    assert captured[0].usage.cache_miss_tokens is None
    assert captured[0].usage.cache_hit_ratio is None
    # AnyCode accounting degrades to a fully fresh prompt rather than inventing a hit.
    assert response.usage.cache_read_input_tokens == 0
    assert response.usage.input_tokens == 100


def test_register_deepseek_provider_is_idempotent() -> None:
    register_deepseek_provider()
    register_deepseek_provider()

    assert get_provider_factory("deepseek") is not None


def test_from_env_requires_api_key() -> None:
    with pytest.raises(RuntimeError, match="DEEPSEEK_API_KEY"):
        DeepSeekAdapter.from_env({})


def test_dotenv_loader_reads_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("DEEPSEEK_API_KEY=sk-dotenv-test\n", encoding="utf-8")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    try:
        _load_dotenv(env_file)
        assert os.environ["DEEPSEEK_API_KEY"] == "sk-dotenv-test"
    finally:
        os.environ.pop("DEEPSEEK_API_KEY", None)


def test_from_env_prefers_process_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-env-test")

    adapter = DeepSeekAdapter.from_env()

    assert adapter.name == "deepseek"
