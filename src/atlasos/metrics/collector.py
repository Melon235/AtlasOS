"""Run-level metric aggregation."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from atlasos.config import Pricing
from atlasos.metrics.models import InferenceRecord, InferenceUsage


class RunMetrics(BaseModel):
    """Aggregated metrics for one experiment run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    total_prompt_tokens: int = Field(ge=0)
    total_completion_tokens: int = Field(ge=0)
    total_cache_hit_tokens: int | None = Field(default=None, ge=0)
    total_cache_miss_tokens: int | None = Field(default=None, ge=0)
    weighted_cache_hit_ratio: float | None = None
    total_latency_ms: float = Field(ge=0)
    task_success: bool | None = None
    estimated_cost: float | None = None


def estimate_cost(usage: InferenceUsage, pricing: Pricing | None) -> float | None:
    """Estimate call cost from a pricing snapshot.

    Returns ``None`` when pricing is absent, incomplete, or the provider did not report the
    cache token split. Never guesses a price.
    """

    if pricing is None or not pricing.is_complete:
        return None
    hit = usage.cache_hit_tokens
    miss = usage.cache_miss_tokens
    if hit is None or miss is None:
        return None
    assert pricing.cache_hit_input_per_million is not None
    assert pricing.cache_miss_input_per_million is not None
    assert pricing.output_per_million is not None
    per_million = (
        hit * pricing.cache_hit_input_per_million
        + miss * pricing.cache_miss_input_per_million
        + usage.completion_tokens * pricing.output_per_million
    )
    return per_million / 1_000_000


class MetricsCollector:
    """Collects normalized records and aggregates them per run."""

    def __init__(self) -> None:
        self._records: list[InferenceRecord] = []

    @property
    def records(self) -> tuple[InferenceRecord, ...]:
        return tuple(self._records)

    def add(self, record: InferenceRecord) -> None:
        self._records.append(record)

    def aggregate(
        self,
        *,
        task_success: bool | None = None,
        estimated_cost: float | None = None,
    ) -> RunMetrics:
        records = self._records
        total_prompt = sum(r.prompt_tokens for r in records)
        total_completion = sum(r.completion_tokens for r in records)
        total_latency = sum(r.latency_ms for r in records)

        hits = [r.cache_hit_tokens for r in records]
        misses = [r.cache_miss_tokens for r in records]
        total_hit: int | None
        total_miss: int | None
        if records and all(v is not None for v in hits) and all(v is not None for v in misses):
            total_hit = sum(v for v in hits if v is not None)
            total_miss = sum(v for v in misses if v is not None)
        else:
            total_hit = None
            total_miss = None

        ratio: float | None = None
        if total_hit is not None and total_miss is not None:
            denominator = total_hit + total_miss
            if denominator > 0:
                ratio = total_hit / denominator

        return RunMetrics(
            total_prompt_tokens=total_prompt,
            total_completion_tokens=total_completion,
            total_cache_hit_tokens=total_hit,
            total_cache_miss_tokens=total_miss,
            weighted_cache_hit_ratio=ratio,
            total_latency_ms=total_latency,
            task_success=task_success,
            estimated_cost=estimated_cost,
        )
