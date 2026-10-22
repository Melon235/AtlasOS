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


class InferenceObservation(BaseModel):
    """One provider call as measured by the adapter, before semantic attribution.

    The adapter owns token/cache/latency measurement and prompt fingerprints; the runtime owns
    run/task/agent attribution. Keeping them apart means a provider change never touches
    attribution code and vice versa.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    usage: InferenceUsage
    stable_prefix_hash: str
    full_prompt_hash: str


class InferenceRecord(BaseModel):
    """One inference call with the experiment and task identity needed to attribute it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    session_id: str | None = None
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
    renderer_policy: str = "upstream_default"
    contextpilot_enabled: bool

    # Diagnostic metadata only; never rendered into a prompt.
    stable_prefix_hash: str | None = None
    full_prompt_hash: str | None = None
    context_layout_hash: str | None = None

    # Stage 3 per-request transform metadata (identity by default).
    transform_name: str = "identity"
    deduplicated: bool = False
    transform_latency_ms: float | None = None
    original_block_count: int | None = None
    optimized_block_count: int | None = None
    original_context_chars: int | None = None
    optimized_context_chars: int | None = None
    pre_transform_layout_hash: str | None = None
    post_transform_layout_hash: str | None = None

    # Stage 2 structure metrics. ``None`` means "not measured", never a fabricated count.
    rendered_dependency_order: list[str] | None = None
    dependency_completion_order: list[str] | None = None
    stable_prefix_tokens: int | None = None
    context_block_tokens: int | None = None
    dynamic_tail_tokens: int | None = None

    @classmethod
    def from_usage(
        cls,
        usage: InferenceUsage,
        *,
        run_id: str,
        model: str,
        session_id: str | None = None,
        task_id: str | None = None,
        agent_id: str | None = None,
        scheduler_policy: str = "upstream_default",
        context_policy: str = "upstream_default",
        renderer_policy: str = "upstream_default",
        contextpilot_enabled: bool = False,
        stable_prefix_hash: str | None = None,
        full_prompt_hash: str | None = None,
        context_layout_hash: str | None = None,
        rendered_dependency_order: list[str] | None = None,
        dependency_completion_order: list[str] | None = None,
        stable_prefix_tokens: int | None = None,
        context_block_tokens: int | None = None,
        dynamic_tail_tokens: int | None = None,
    ) -> InferenceRecord:
        return cls(
            run_id=run_id,
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
            renderer_policy=renderer_policy,
            contextpilot_enabled=contextpilot_enabled,
            stable_prefix_hash=stable_prefix_hash,
            full_prompt_hash=full_prompt_hash,
            context_layout_hash=context_layout_hash,
            rendered_dependency_order=rendered_dependency_order,
            dependency_completion_order=dependency_completion_order,
            stable_prefix_tokens=stable_prefix_tokens,
            context_block_tokens=context_block_tokens,
            dynamic_tail_tokens=dynamic_tail_tokens,
        )
