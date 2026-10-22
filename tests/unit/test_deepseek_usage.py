from __future__ import annotations

import pytest

from atlasos.integrations.deepseek import UsageParseError, parse_response_usage, parse_usage


def test_parses_explicit_cache_fields() -> None:
    usage = {
        "prompt_tokens": 1024,
        "completion_tokens": 128,
        "prompt_cache_hit_tokens": 256,
        "prompt_cache_miss_tokens": 768,
    }

    result = parse_usage(usage, latency_ms=42.0)

    assert result.prompt_tokens == 1024
    assert result.completion_tokens == 128
    assert result.cache_hit_tokens == 256
    assert result.cache_miss_tokens == 768
    assert result.cache_hit_ratio == pytest.approx(0.25)
    assert result.latency_ms == 42.0


def test_missing_cache_fields_stay_none() -> None:
    result = parse_usage({"prompt_tokens": 10, "completion_tokens": 3}, latency_ms=1.0)

    assert result.cache_hit_tokens is None
    assert result.cache_miss_tokens is None
    assert result.cache_hit_ratio is None


def test_cached_tokens_details_are_used_when_present() -> None:
    usage = {
        "prompt_tokens": 100,
        "completion_tokens": 5,
        "prompt_tokens_details": {"cached_tokens": 40},
    }

    result = parse_usage(usage, latency_ms=2.0)

    assert result.cache_hit_tokens == 40
    assert result.cache_miss_tokens == 60
    assert result.cache_hit_ratio == pytest.approx(0.4)


def test_reported_zero_hit_is_a_real_zero_not_none() -> None:
    usage = {
        "prompt_tokens": 20,
        "completion_tokens": 1,
        "prompt_cache_hit_tokens": 0,
        "prompt_cache_miss_tokens": 20,
    }

    result = parse_usage(usage, latency_ms=0.5)

    assert result.cache_hit_tokens == 0
    assert result.cache_hit_ratio == 0.0


def test_zero_denominator_yields_none_ratio() -> None:
    usage = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "prompt_cache_hit_tokens": 0,
        "prompt_cache_miss_tokens": 0,
    }

    result = parse_usage(usage, latency_ms=0.0)

    assert result.cache_hit_ratio is None


def test_response_wrapper_is_unwrapped() -> None:
    response = {
        "id": "chatcmpl-1",
        "choices": [],
        "usage": {
            "prompt_tokens": 50,
            "completion_tokens": 7,
            "prompt_cache_hit_tokens": 10,
            "prompt_cache_miss_tokens": 40,
        },
    }

    result = parse_response_usage(response, latency_ms=3.0)

    assert result.prompt_tokens == 50
    assert result.cache_hit_tokens == 10


def test_missing_prompt_tokens_raises() -> None:
    with pytest.raises(UsageParseError):
        parse_usage({"completion_tokens": 1}, latency_ms=0.0)


def test_response_without_usage_raises() -> None:
    with pytest.raises(UsageParseError):
        parse_response_usage({"choices": []}, latency_ms=0.0)


def test_non_integer_token_count_raises() -> None:
    with pytest.raises(UsageParseError):
        parse_usage({"prompt_tokens": "10"}, latency_ms=0.0)
