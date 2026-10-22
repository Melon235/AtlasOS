"""DeepSeek adapter: usage normalization.

Live transport wiring against the AnyCode provider boundary lands in Stage 1. This module owns
the parsing rule: provider-reported cache fields are copied verbatim or left ``None``; nothing is
inferred from prompt length unless the provider itself reported the split.
"""

from __future__ import annotations

import os
import time
from collections.abc import AsyncIterator, Callable, Mapping
from pathlib import Path
from typing import Any

from anycode.plugins.registry import register_provider_factory
from anycode.providers._openai_compat import (
    apply_model_params,
    map_messages,
    map_tool_def,
    parse_chat_response,
)
from anycode.types import (
    LLMChatOptions,
    LLMMessage,
    LLMResponse,
    LLMStreamOptions,
    StreamEvent,
    TextBlock,
    TokenUsage,
)

from atlasos.metrics.fingerprint import full_prompt_hash, stable_prefix_hash
from atlasos.metrics.models import InferenceObservation, InferenceUsage


class UsageParseError(ValueError):
    """Raised when a provider usage payload lacks fields required for accounting."""


def _as_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise UsageParseError(f"usage field {field!r} must be an integer, got {value!r}")
    if value < 0:
        raise UsageParseError(f"usage field {field!r} must be non-negative, got {value!r}")
    return value


def parse_usage(usage: Mapping[str, Any], *, latency_ms: float) -> InferenceUsage:
    """Normalize a DeepSeek ``usage`` object.

    DeepSeek reports ``prompt_cache_hit_tokens`` and ``prompt_cache_miss_tokens``. When those are
    absent, an OpenAI-style ``prompt_tokens_details.cached_tokens`` is accepted as the hit count
    and the miss count is derived from the reported prompt total. When neither is present, both
    cache fields stay ``None``.
    """

    if "prompt_tokens" not in usage:
        raise UsageParseError("usage payload is missing 'prompt_tokens'")

    prompt_tokens = _as_int(usage["prompt_tokens"], field="prompt_tokens")
    completion_tokens = _as_int(usage.get("completion_tokens", 0), field="completion_tokens")

    hit: int | None
    miss: int | None
    raw_hit = usage.get("prompt_cache_hit_tokens")
    raw_miss = usage.get("prompt_cache_miss_tokens")
    if raw_hit is not None:
        hit = _as_int(raw_hit, field="prompt_cache_hit_tokens")
    else:
        details = usage.get("prompt_tokens_details")
        cached = details.get("cached_tokens") if isinstance(details, Mapping) else None
        hit = (
            _as_int(cached, field="prompt_tokens_details.cached_tokens")
            if cached is not None
            else None
        )

    if raw_miss is not None:
        miss = _as_int(raw_miss, field="prompt_cache_miss_tokens")
    elif hit is not None:
        miss = max(prompt_tokens - hit, 0)
    else:
        miss = None

    # Keep the provider's numbers as reported. If hit + miss differs from prompt_tokens, that is a
    # provider accounting detail, not something to "fix" into a fabricated split.
    return InferenceUsage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cache_hit_tokens=hit,
        cache_miss_tokens=miss,
        latency_ms=latency_ms,
    )


def parse_response_usage(response: Mapping[str, Any], *, latency_ms: float) -> InferenceUsage:
    """Normalize the ``usage`` object of a chat-completion response."""

    usage = response.get("usage")
    if not isinstance(usage, Mapping):
        raise UsageParseError("response payload is missing a 'usage' object")
    return parse_usage(usage, latency_ms=latency_ms)


DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"


def _load_dotenv(dotenv_path: str | Path | None = None) -> None:
    """Load ``.env`` (default: nearest to this module); real environment variables win."""

    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - python-dotenv is a declared dependency
        return
    load_dotenv(dotenv_path=dotenv_path, override=False)


def _usage_mapping(usage: Any) -> Mapping[str, Any] | None:
    if usage is None:
        return None
    if isinstance(usage, Mapping):
        return usage
    dump = getattr(usage, "model_dump", None)
    if callable(dump):
        dumped = dump()
        return dumped if isinstance(dumped, Mapping) else None
    return None


def _to_token_usage(usage: InferenceUsage) -> TokenUsage:
    """Map normalized usage onto AnyCode's token model.

    ``TokenUsage`` cannot express "provider did not report cache", so the AtlasOS metric record
    (which can) is the source of truth; AnyCode's accounting treats an unreported split as a
    fully fresh prompt.
    """

    hit = usage.cache_hit_tokens
    miss = usage.cache_miss_tokens
    if hit is not None and miss is not None:
        return TokenUsage(
            input_tokens=miss,
            output_tokens=usage.completion_tokens,
            cache_read_input_tokens=hit,
        )
    return TokenUsage(input_tokens=usage.prompt_tokens, output_tokens=usage.completion_tokens)


class DeepSeekAdapter:
    """Minimal DeepSeek adapter over the OpenAI-compatible chat API.

    AnyCode's built-in OpenAI adapter cannot set a base URL and drops the provider-specific
    ``prompt_cache_hit_tokens``/``prompt_cache_miss_tokens`` fields, so AtlasOS registers this
    adapter through AnyCode's provider-factory extension point instead of patching upstream.
    """

    def __init__(
        self,
        *,
        client: Any,
        model: str = DEFAULT_MODEL,
        on_call: Callable[[InferenceObservation], None] | None = None,
    ) -> None:
        self._client = client
        self._model = model
        self._on_call = on_call

    @property
    def name(self) -> str:
        return "deepseek"

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        on_call: Callable[[InferenceObservation], None] | None = None,
    ) -> DeepSeekAdapter:
        if env is None:
            _load_dotenv()
            values = os.environ
        else:
            values = env
        api_key = values.get("DEEPSEEK_API_KEY")
        if not api_key:
            raise RuntimeError("DEEPSEEK_API_KEY is not set; live DeepSeek calls are opt-in")

        from openai import AsyncOpenAI

        client = AsyncOpenAI(
            api_key=api_key,
            base_url=values.get("DEEPSEEK_BASE_URL", DEFAULT_BASE_URL),
        )
        return cls(
            client=client,
            model=values.get("DEEPSEEK_MODEL", DEFAULT_MODEL),
            on_call=on_call,
        )

    async def chat(
        self,
        messages: list[LLMMessage],
        options: LLMChatOptions,
        *,
        response_format: dict[str, Any] | None = None,
    ) -> LLMResponse:
        started = time.perf_counter()
        kwargs: dict[str, Any] = {
            "model": options.model or self._model,
            "messages": map_messages(messages, options.system_prompt),
        }
        apply_model_params(
            kwargs,
            options.model,
            max_tokens=options.max_tokens,
            temperature=options.temperature,
            reasoning_effort=options.reasoning_effort,
        )
        if options.tools:
            kwargs["tools"] = [map_tool_def(tool) for tool in options.tools]
        if response_format:
            kwargs["response_format"] = response_format

        completion = await self._client.chat.completions.create(**kwargs)
        latency_ms = (time.perf_counter() - started) * 1000.0

        content, stop_reason, fallback = parse_chat_response(completion)
        usage = self._normalize_usage(getattr(completion, "usage", None), latency_ms, fallback)
        if self._on_call is not None:
            self._on_call(
                InferenceObservation(
                    usage=usage,
                    stable_prefix_hash=stable_prefix_hash(options.system_prompt, options.tools),
                    full_prompt_hash=full_prompt_hash(
                        options.system_prompt, messages, options.tools
                    ),
                )
            )

        return LLMResponse(
            id=completion.id,
            content=content,
            model=completion.model,
            stop_reason=stop_reason,
            usage=_to_token_usage(usage),
        )

    def stream(
        self,
        messages: list[LLMMessage],
        options: LLMStreamOptions,
    ) -> AsyncIterator[StreamEvent]:
        """Non-incremental fallback: one text event followed by the final response."""

        async def _generate() -> AsyncIterator[StreamEvent]:
            response = await self.chat(messages, options)
            for block in response.content:
                if isinstance(block, TextBlock) and block.text:
                    yield StreamEvent(type="text", data=block.text)
            yield StreamEvent(type="done", data=response)

        return _generate()

    @staticmethod
    def _normalize_usage(usage: Any, latency_ms: float, fallback: TokenUsage) -> InferenceUsage:
        mapping = _usage_mapping(usage)
        if mapping is not None:
            try:
                return parse_usage(mapping, latency_ms=latency_ms)
            except UsageParseError:
                pass
        return InferenceUsage(
            prompt_tokens=fallback.input_tokens
            + fallback.cache_read_input_tokens
            + fallback.cache_creation_input_tokens,
            completion_tokens=fallback.output_tokens,
            latency_ms=latency_ms,
        )


async def _deepseek_factory(**_: Any) -> DeepSeekAdapter:
    return DeepSeekAdapter.from_env()


def register_deepseek_provider(name: str = "deepseek") -> None:
    """Register the DeepSeek adapter as an AnyCode provider.

    Idempotent: the factory is a module-level function, so repeated calls register the same
    object and the provider registry accepts them.
    """

    register_provider_factory(name, _deepseek_factory)
