from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from atlasos.config import AtlasConfig, load_config

CONFIGS = Path(__file__).resolve().parents[2] / "experiments" / "configs"
BASELINE = CONFIGS / "baseline.yaml"
UAT = CONFIGS / "v2-uat.yaml"


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


def test_loads_the_two_reference_configurations() -> None:
    """The baseline arm and the optimized arm must stay expressible in configuration."""

    baseline = load_config(BASELINE)
    optimized = load_config(CONFIGS / "eval-benchmark.yaml")

    assert (baseline.context.policy, baseline.context.renderer) == (
        "upstream_default",
        "upstream_default",
    )
    # The benchmark arm keeps AnyCode prompt construction and enables the runtime mechanisms.
    assert optimized.context.policy == "upstream_default"
    assert optimized.tools.mode == "progressive"
    assert optimized.trajectory.enabled is True
    assert optimized.sandbox.provider == "opensandbox"


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


def test_loads_real_sandbox_uat_configuration() -> None:
    config = load_config(UAT)
    assert config.sandbox.enabled is True
    assert config.sandbox.provider == "opensandbox"
    assert config.sandbox.endpoint == "localhost:8080"
    assert config.sandbox.protocol == "http"
    assert config.sandbox.network == "none"
    assert config.sandbox.workspace_root == "/workspace/project"
    assert config.sandbox.image is not None
    assert config.sandbox.timeout_minutes > 0
    assert config.sandbox.memory


def test_sandbox_configuration_cannot_be_weakened() -> None:
    with pytest.raises(ValidationError, match="Input should be 'none'"):
        AtlasConfig.model_validate({"sandbox": {"provider": "daytona", "network": "unrestricted"}})
    with pytest.raises(ValidationError, match="workspace_root"):
        AtlasConfig.model_validate({"sandbox": {"provider": "daytona", "workspace_root": "/"}})
    with pytest.raises(ValidationError, match="pinned"):
        AtlasConfig.model_validate({"sandbox": {"provider": "daytona", "image": "latest"}})
    with pytest.raises(ValidationError, match="exactly one"):
        AtlasConfig.model_validate(
            {"sandbox": {"provider": "daytona", "image": "python:3.12", "snapshot": "s"}}
        )
    with pytest.raises(ValidationError, match="exactly one"):
        AtlasConfig.model_validate({"sandbox": {"provider": "daytona", "image": None}})
    with pytest.raises(ValidationError, match="endpoint"):
        AtlasConfig.model_validate(
            {"sandbox": {"provider": "opensandbox", "endpoint": "http://localhost:8080"}}
        )


def test_model_facing_configuration_has_no_sandbox_selection() -> None:
    config = AtlasConfig()
    assert config.sandbox.provider == "fake"
    assert config.sandbox.enabled is False
    # There is no model/task/tool input path to changed sandbox fields in AtlasConfig.
    with pytest.raises(ValidationError):
        AtlasConfig.model_validate({"sandbox": {"provider": "model-choice"}})
