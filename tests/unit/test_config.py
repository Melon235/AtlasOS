from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from atlasos.config import AtlasConfig, load_config

BASELINE = Path(__file__).resolve().parents[2] / "experiments" / "configs" / "baseline.yaml"


def test_loads_baseline_configuration() -> None:
    config = load_config(BASELINE)

    assert config.provider.name == "deepseek"
    assert config.scheduler.policy == "upstream_default"
    assert config.context.policy == "upstream_default"
    assert config.context.renderer == "upstream_default"
    assert config.context.contextpilot_enabled is False
    assert config.pricing is None


def test_rejects_invalid_context_mode_pair() -> None:
    with pytest.raises(ValidationError, match="unsupported context policy/renderer"):
        AtlasConfig.model_validate(
            {"context": {"policy": "upstream_default", "renderer": "stable_deterministic"}}
        )


def test_loads_stage2_variants() -> None:
    configs = Path(__file__).resolve().parents[2] / "experiments" / "configs"
    naive = load_config(configs / "stage2-naive.yaml")
    stable = load_config(configs / "stage2-stable.yaml")

    assert (naive.context.policy, naive.context.renderer) == (
        "task_scoped",
        "naive_execution_order",
    )
    assert (stable.context.policy, stable.context.renderer) == (
        "task_scoped",
        "stable_deterministic",
    )


def test_rejects_unknown_scheduler_policy() -> None:
    with pytest.raises(ValidationError):
        AtlasConfig.model_validate({"scheduler": {"policy": "fastest"}})


def test_rejects_unknown_keys() -> None:
    with pytest.raises(ValidationError):
        AtlasConfig.model_validate({"schedular": {"policy": "capability"}})


def test_incomplete_pricing_is_not_complete() -> None:
    config = AtlasConfig.model_validate({"pricing": {"cache_hit_input_per_million": 1.0}})

    assert config.pricing is not None
    assert config.pricing.is_complete is False


def test_complete_pricing_snapshot() -> None:
    config = AtlasConfig.model_validate(
        {
            "pricing": {
                "cache_hit_input_per_million": 0.1,
                "cache_miss_input_per_million": 1.0,
                "output_per_million": 2.0,
                "snapshot_date": "2026-10-22",
            }
        }
    )

    assert config.pricing is not None
    assert config.pricing.is_complete is True
    assert config.pricing.snapshot_date is not None
    assert config.pricing.snapshot_date.isoformat() == "2026-10-22"


def test_historical_optimizer_config_is_rejected() -> None:
    with pytest.raises(ValidationError):
        AtlasConfig.model_validate(
            {"context": {"optimizer": {"name": "contextpilot", "reorder": True}}}
        )
