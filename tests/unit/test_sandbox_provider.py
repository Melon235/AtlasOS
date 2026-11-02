"""Trusted Atlas Sandbox provider selection and capability negotiation."""

from __future__ import annotations

import pytest
from anycode.identity.context import ExecutionContext
from anycode.sandbox import SandboxCapabilities, SandboxSpec
from tests.helpers.fake_sandbox_provider import FakeSandboxProvider

from atlasos.sandbox import (
    SandboxCapabilityError,
    SandboxCredentialError,
    build_sandbox_provider,
    capability_digest,
    capability_failures,
    capability_summary,
    is_real_sandbox_provider,
    require_capabilities,
    require_real_sandbox_provider,
)


def daytona_capabilities() -> SandboxCapabilities:
    return SandboxCapabilities(
        provider="daytona",
        isolation="remote",
        networking="allowlist",
        persistent_filesystem=True,
        snapshots=False,
        command_streaming=True,
        cancellation=True,
        file_transfer=True,
        evidence=True,
    )


def test_real_provider_requires_host_credentials() -> None:
    with pytest.raises(SandboxCredentialError, match="DAYTONA_API_KEY"):
        build_sandbox_provider("daytona", environ={})
    with pytest.raises(SandboxCredentialError, match="not a configured real"):
        build_sandbox_provider("fake", environ={"DAYTONA_API_KEY": "not-used"})


def test_daytona_provider_construction_uses_anycode_factory() -> None:
    provider = build_sandbox_provider(
        "daytona", environ={"DAYTONA_API_KEY": "test-only-not-a-real-key"}
    )
    capabilities = provider.capabilities()
    assert capabilities.provider == "daytona"
    assert capabilities.isolation == "remote"
    assert capabilities.file_transfer is True
    assert capabilities.persistent_filesystem is True
    assert is_real_sandbox_provider(provider, expected_name="daytona") is True
    assert capability_failures(capabilities) == ()
    require_capabilities(capabilities)
    require_real_sandbox_provider(provider, expected_name="daytona")


def test_fake_provider_is_not_real_and_fails_real_requirement(tmp_path) -> None:
    provider = FakeSandboxProvider(tmp_path / "provider")
    assert is_real_sandbox_provider(provider) is False
    with pytest.raises(SandboxCapabilityError, match="real sandbox provider is required"):
        require_real_sandbox_provider(provider)


def test_allowlist_capability_is_compatible_with_network_none() -> None:
    # AnyCode reports allowlist for Daytona/Modal because they can enforce an allowlist or a
    # complete block. Atlas M5 requests the stricter subset network='none'.
    assert capability_failures(daytona_capabilities()) == ()


def test_capability_failures_fail_closed() -> None:
    unrestricted = daytona_capabilities().model_copy(update={"networking": "unrestricted"})
    assert "network='none'" in capability_failures(unrestricted)[0]
    process = daytona_capabilities().model_copy(update={"isolation": "process"})
    assert any("isolation" in failure for failure in capability_failures(process))
    no_files = daytona_capabilities().model_copy(update={"file_transfer": False})
    assert any("file_transfer" in failure for failure in capability_failures(no_files))
    with pytest.raises(SandboxCapabilityError):
        require_capabilities(unrestricted)


def test_capability_digest_is_stable_and_secret_free() -> None:
    capabilities = daytona_capabilities()
    first = capability_digest(capabilities)
    second = capability_digest(capabilities)
    assert first == second
    assert first.startswith("sha256:")
    summary = capability_summary(capabilities)
    assert summary["provider"] == "daytona"
    assert "secret" not in str(summary).casefold()


def test_daytona_adapter_maps_network_none_to_block_all() -> None:
    pytest.importorskip("daytona")
    provider = build_sandbox_provider(
        "daytona", environ={"DAYTONA_API_KEY": "test-only-not-a-real-key"}
    )
    spec = SandboxSpec(
        run_id="run-network-none",
        task_id="task-network-none",
        correlation_id="run-network-none:task-network-none",
        context=ExecutionContext(principal="user:test", tenant_scope="test"),
        image="python:3.12@sha256:" + "0" * 64,
        network="none",
        persistent=True,
        labels={"atlas_m5": "1"},
    )
    params = getattr(provider, "_creation_params")(spec)  # adapter contract check
    assert params is not None
    assert params.network_block_all is True
    assert params.ephemeral is False
    assert params.image == spec.image
    assert params.secrets in (None, {})
