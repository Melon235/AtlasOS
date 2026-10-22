from __future__ import annotations

import pytest

from atlasos.config import Pricing
from atlasos.metrics import (
    InferenceRecord,
    InferenceUsage,
    MetricsCollector,
    RunMetrics,
    estimate_cost,
)


def _record(
    *,
    hit: int | None,
    miss: int | None,
    prompt: int,
    completion: int = 10,
    latency_ms: float = 5.0,
) -> InferenceRecord:
    usage = InferenceUsage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        cache_hit_tokens=hit,
        cache_miss_tokens=miss,
        latency_ms=latency_ms,
    )
    return InferenceRecord.from_usage(usage, session_id="session-1", model="deepseek-chat")


def test_weighted_ratio_is_token_weighted_not_mean_of_ratios() -> None:
    collector = MetricsCollector()
    collector.add(_record(hit=100, miss=900, prompt=1000))
    collector.add(_record(hit=100, miss=100, prompt=200))

    metrics = collector.aggregate()

    assert metrics.total_cache_hit_tokens == 200
    assert metrics.total_cache_miss_tokens == 1000
    assert metrics.weighted_cache_hit_ratio == pytest.approx(200 / 1200)
    # The arithmetic mean of per-call ratios would be 0.3, which would misreport the run.
    assert metrics.weighted_cache_hit_ratio != pytest.approx(0.3)


def test_missing_cache_on_one_record_nulls_run_totals() -> None:
    collector = MetricsCollector()
    collector.add(_record(hit=100, miss=900, prompt=1000))
    collector.add(_record(hit=None, miss=None, prompt=500))

    metrics = collector.aggregate()

    assert metrics.total_cache_hit_tokens is None
    assert metrics.total_cache_miss_tokens is None
    assert metrics.weighted_cache_hit_ratio is None


def test_empty_collector_reports_zero_totals() -> None:
    metrics = MetricsCollector().aggregate()

    assert isinstance(metrics, RunMetrics)
    assert metrics.total_prompt_tokens == 0
    assert metrics.total_latency_ms == 0
    assert metrics.weighted_cache_hit_ratio is None


def test_estimated_cost_uses_pricing_snapshot() -> None:
    usage = InferenceUsage(
        prompt_tokens=1000,
        completion_tokens=100,
        cache_hit_tokens=200,
        cache_miss_tokens=800,
        latency_ms=1.0,
    )
    pricing = Pricing(
        cache_hit_input_per_million=1.0,
        cache_miss_input_per_million=2.0,
        output_per_million=4.0,
    )

    cost = estimate_cost(usage, pricing)

    assert cost == pytest.approx((200 * 1.0 + 800 * 2.0 + 100 * 4.0) / 1_000_000)


def test_estimated_cost_is_none_without_pricing() -> None:
    usage = InferenceUsage(
        prompt_tokens=10,
        completion_tokens=1,
        cache_hit_tokens=5,
        cache_miss_tokens=5,
        latency_ms=1.0,
    )

    assert estimate_cost(usage, None) is None


def test_estimated_cost_is_none_without_cache_split() -> None:
    usage = InferenceUsage(prompt_tokens=10, completion_tokens=1, latency_ms=1.0)
    pricing = Pricing(
        cache_hit_input_per_million=1.0,
        cache_miss_input_per_million=2.0,
        output_per_million=4.0,
    )

    assert estimate_cost(usage, pricing) is None
