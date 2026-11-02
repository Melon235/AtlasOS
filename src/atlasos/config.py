"""Experiment configuration for AtlasOS.

Configuration is the ablation boundary: every optimization must be switchable here so the
baseline stays reproducible. Unknown keys are rejected so typos cannot silently disable a
mechanism.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Literal

import yaml
from anycode.types import MCPServerConfig
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SchedulerPolicy = Literal["upstream_default", "capability", "context_aware", "cache_aware"]
ContextPolicy = Literal["upstream_default", "task_scoped"]
RendererPolicy = Literal[
    "upstream_default",
    "naive_execution_order",
    "stable_deterministic",
]

# Only these policy/renderer pairs are honest for Stage 2; anything else fails fast.
_ALLOWED_CONTEXT_MODES: frozenset[tuple[str, str]] = frozenset(
    {
        ("upstream_default", "upstream_default"),
        ("task_scoped", "naive_execution_order"),
        ("task_scoped", "stable_deterministic"),
    }
)


class Pricing(BaseModel):
    """Provider pricing snapshot used to estimate run cost.

    Cost is optional: when pricing is absent or incomplete, cost is ``None`` and token metrics
    still work.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str = "deepseek"
    cache_hit_input_per_million: float | None = Field(default=None, ge=0)
    cache_miss_input_per_million: float | None = Field(default=None, ge=0)
    output_per_million: float | None = Field(default=None, ge=0)
    snapshot_date: date | None = None

    @property
    def is_complete(self) -> bool:
        return (
            self.cache_hit_input_per_million is not None
            and self.cache_miss_input_per_million is not None
            and self.output_per_million is not None
        )


class ContextPilotOptions(BaseModel):
    """Options forwarded to the ContextPilot adapter (Stage 3)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reorder: bool = True
    deduplicate: bool = True


class ContextTransformConfig(BaseModel):
    """Per-request context transform (deduplication only; reorder is a batch policy)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: Literal["identity", "contextpilot_dedup"] = "identity"


class ContextConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    policy: ContextPolicy = "upstream_default"
    renderer: RendererPolicy = "upstream_default"
    transform: ContextTransformConfig = ContextTransformConfig()
    # Deprecated Stage 1/2 fields retained only so historical configs still parse.
    contextpilot_enabled: bool = False
    contextpilot: ContextPilotOptions = ContextPilotOptions()

    @model_validator(mode="after")
    def _validate_mode_pair(self) -> ContextConfig:
        if (self.policy, self.renderer) not in _ALLOWED_CONTEXT_MODES:
            raise ValueError(
                "unsupported context policy/renderer pair: "
                f"{self.policy!r}/{self.renderer!r}; allowed pairs are "
                f"{sorted(_ALLOWED_CONTEXT_MODES)}"
            )
        if self.transform.name != "identity" and (
            self.policy != "task_scoped" or self.renderer != "stable_deterministic"
        ):
            raise ValueError(
                "a non-identity context transform requires policy='task_scoped' and "
                "renderer='stable_deterministic'"
            )
        return self


class SchedulerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    policy: SchedulerPolicy = "upstream_default"


class ProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: Literal["deepseek"] = "deepseek"
    model: str = "deepseek-chat"
    base_url: str | None = None
    api_key_env: str = "DEEPSEEK_API_KEY"


class PlacementWeightsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    locality: float = Field(default=1.0, ge=0)
    load: float = Field(default=0.2, ge=0)


class PlacementConfig(BaseModel):
    """Task placement planning configuration (pre-placement)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    policy: Literal[
        "bypass",
        "static_baseline",
        "capability_only",
        "context_locality",
    ] = "bypass"
    weights: PlacementWeightsConfig = PlacementWeightsConfig()


class ToolsConfig(BaseModel):
    """Atlas V2 M1 tool-surface configuration.

    ``disabled`` preserves V1 behavior exactly. ``progressive`` exposes only core tools natively
    and hides external tools behind ``tool.search`` / ``dispatch_action``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["disabled", "progressive"] = "disabled"
    search_backend: Literal["keyword", "semantic"] = "keyword"
    embedding_model: str | None = None
    max_results: int = Field(default=3, ge=1, le=10)
    mcp_servers: tuple[MCPServerConfig, ...] = ()


class TrajectoryConfig(BaseModel):
    """M7 durable trajectory evidence (never an execution semantic)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = False
    store_root: str = "trajectory"

    @field_validator("store_root")
    @classmethod
    def _safe_store_root(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("trajectory.store_root must not be empty")
        path = Path(normalized)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("trajectory.store_root must be relative and traversal-free")
        return normalized


class AtlasConfig(BaseModel):
    """Top-level experiment configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: ProviderConfig = ProviderConfig()
    context: ContextConfig = ContextConfig()
    scheduler: SchedulerConfig = SchedulerConfig()
    placement: PlacementConfig = PlacementConfig()
    tools: ToolsConfig = ToolsConfig()
    trajectory: TrajectoryConfig = TrajectoryConfig()
    pricing: Pricing | None = None


def load_config(path: str | Path) -> AtlasConfig:
    """Load and validate an experiment configuration from YAML."""

    text = Path(path).read_text(encoding="utf-8")
    data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        raise ValueError(f"configuration root must be a mapping: {path}")
    return AtlasConfig.model_validate(data)
