"""Normalized inference usage records.

Provider usage is normalized here so downstream code never reads provider-specific JSON.
Cache fields are optional: the provider may expose none, and a 0% hit run must stay valid.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, computed_field


class InferenceUsage(BaseModel):
    """Normalized token usage for a single inference call."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    cache_hit_tokens: int | None = Field(default=None, ge=0)
    cache_miss_tokens: int | None = Field(default=None, ge=0)
    latency_ms: float = Field(ge=0)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def cache_hit_ratio(self) -> float | None:
        """Token-weighted hit ratio, or ``None`` when the provider omitted cache fields."""

        hit = self.cache_hit_tokens
        miss = self.cache_miss_tokens
        if hit is None or miss is None:
            return None
        total = hit + miss
        if total == 0:
            return None
        return hit / total


class InferenceRecord(BaseModel):
    """One inference call with the experiment identity needed to attribute it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str
    task_id: str | None = None
    agent_id: str | None = None

    model: str

    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)

    cache_hit_tokens: int | None = Field(default=None, ge=0)
    cache_miss_tokens: int | None = Field(default=None, ge=0)
    cache_hit_ratio: float | None = None

    latency_ms: float = Field(ge=0)

    scheduler_policy: str
    context_policy: str
    contextpilot_enabled: bool

    @classmethod
    def from_usage(
        cls,
        usage: InferenceUsage,
        *,
        session_id: str,
        model: str,
        task_id: str | None = None,
        agent_id: str | None = None,
        scheduler_policy: str = "upstream_default",
        context_policy: str = "upstream_default",
        contextpilot_enabled: bool = False,
    ) -> InferenceRecord:
        return cls(
            session_id=session_id,
            task_id=task_id,
            agent_id=agent_id,
            model=model,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            cache_hit_tokens=usage.cache_hit_tokens,
            cache_miss_tokens=usage.cache_miss_tokens,
            cache_hit_ratio=usage.cache_hit_ratio,
            latency_ms=usage.latency_ms,
            scheduler_policy=scheduler_policy,
            context_policy=context_policy,
            contextpilot_enabled=contextpilot_enabled,
        )
