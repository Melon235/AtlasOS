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
from pydantic import BaseModel, ConfigDict, Field

SchedulerPolicy = Literal["upstream_default", "capability", "context_aware", "cache_aware"]
ContextSelectorName = Literal["full", "task_scoped", "relevance"]


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


class ContextConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    selector: ContextSelectorName = "full"
    contextpilot_enabled: bool = False
    contextpilot: ContextPilotOptions = ContextPilotOptions()


class SchedulerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    policy: SchedulerPolicy = "upstream_default"


class ProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: Literal["deepseek"] = "deepseek"
    model: str = "deepseek-chat"
    base_url: str | None = None
    api_key_env: str = "DEEPSEEK_API_KEY"


class AtlasConfig(BaseModel):
    """Top-level experiment configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: ProviderConfig = ProviderConfig()
    context: ContextConfig = ContextConfig()
    scheduler: SchedulerConfig = SchedulerConfig()
    pricing: Pricing | None = None


def load_config(path: str | Path) -> AtlasConfig:
    """Load and validate an experiment configuration from YAML."""

    text = Path(path).read_text(encoding="utf-8")
    data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        raise ValueError(f"configuration root must be a mapping: {path}")
    return AtlasConfig.model_validate(data)
