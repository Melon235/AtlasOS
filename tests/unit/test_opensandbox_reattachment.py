"""OpenSandbox 1.2.0 verified-reattachment adapter tests using public SDK seams."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from atlasos.sandbox.opensandbox_provider import OpenSandboxOptions, OpenSandboxProvider
from atlasos.sandbox.reattachment import (
    SandboxAttachFailed,
    SandboxNetworkPolicyUnavailable,
    SandboxNetworkPolicyViolation,
    SandboxProviderUnavailable,
    SandboxRemoteNotFound,
)

CREATED = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _info(*, state: str = "Running", metadata: dict[str, str] | None = None):
    from opensandbox.models.sandboxes import SandboxInfo, SandboxStatus

    return SandboxInfo(
        id="sandbox-1",
        status=SandboxStatus(state=state),
        entrypoint=["/bin/sh"],
        created_at=CREATED,
        metadata=metadata
        or {
            "atlas.run_id": "run-1",
            "atlas.task_id": "task-1",
            "atlas.provider": "opensandbox",
        },
    )


class _Manager:
    def __init__(self, info=None, error: Exception | None = None) -> None:
        self.info = info
        self.error = error
        self.closed = False

    async def get_sandbox_info(self, sandbox_id: str):
        del sandbox_id
        if self.error is not None:
            raise self.error
        assert self.info is not None
        return self.info

    async def close(self) -> None:
        self.closed = True


def _patch_manager_create(monkeypatch, manager: _Manager) -> None:
    import opensandbox

    async def fake_create(cls, connection_config=None):  # noqa: ANN001, ARG001
        return manager

    monkeypatch.setattr(opensandbox.SandboxManager, "create", classmethod(fake_create))


def _patch_connect(monkeypatch, function) -> None:
    import opensandbox

    monkeypatch.setattr(opensandbox.Sandbox, "connect", classmethod(function))


async def test_inspect_existing_maps_public_sandbox_info_without_connect(
    monkeypatch,
) -> None:
    manager = _Manager(info=_info())
    _patch_manager_create(monkeypatch, manager)

    async def forbidden_connect(cls, *args, **kwargs):  # noqa: ANN001, ARG001
        raise AssertionError("inspection must not connect")

    _patch_connect(monkeypatch, forbidden_connect)
    provider = OpenSandboxProvider(OpenSandboxOptions(endpoint="localhost:8080"))
    observation = await provider.inspect_existing("sandbox-1")
    assert observation.sandbox_id == "sandbox-1"
    assert observation.state == "RUNNING"
    assert observation.created_at == CREATED
    assert observation.metadata["run_id"] == "run-1"
    assert observation.metadata["task_id"] == "task-1"
    assert observation.metadata_digest.startswith("sha256:")
    assert provider._sandboxes == {}
    assert manager.closed is True


async def test_inspect_existing_404_is_not_found(monkeypatch) -> None:
    from opensandbox.exceptions import SandboxApiException

    manager = _Manager(error=SandboxApiException("missing", status_code=404))
    _patch_manager_create(monkeypatch, manager)
    provider = OpenSandboxProvider()
    with pytest.raises(SandboxRemoteNotFound, match="does not exist"):
        await provider.inspect_existing("sandbox-1")
    assert manager.closed is True


async def test_inspect_existing_transient_error_is_unavailable(monkeypatch) -> None:
    from opensandbox.exceptions import SandboxConnectionException

    manager = _Manager(error=SandboxConnectionException("offline"))
    _patch_manager_create(monkeypatch, manager)
    provider = OpenSandboxProvider()
    with pytest.raises(SandboxProviderUnavailable, match="unavailable"):
        await provider.inspect_existing("sandbox-1")


async def test_inspect_existing_500_is_unavailable(monkeypatch) -> None:
    from opensandbox.exceptions import SandboxApiException

    manager = _Manager(error=SandboxApiException("server error", status_code=500))
    _patch_manager_create(monkeypatch, manager)
    provider = OpenSandboxProvider()
    with pytest.raises(SandboxProviderUnavailable, match="could not inspect"):
        await provider.inspect_existing("sandbox-1")


async def test_attach_existing_installs_cache_only_after_connect(monkeypatch) -> None:
    connected = SimpleNamespace(id="sandbox-1", closed=False)
    calls: list[tuple[Any, ...]] = []

    async def fake_connect(cls, sandbox_id, connection_config=None, **kwargs):  # noqa: ANN001
        calls.append((sandbox_id, connection_config, kwargs))
        return connected

    _patch_connect(monkeypatch, fake_connect)
    provider = OpenSandboxProvider()
    await provider.attach_existing("sandbox-1")
    assert calls and calls[0][0] == "sandbox-1"
    assert provider._sandboxes["sandbox-1"] is connected


async def test_attach_existing_failure_leaves_cache_empty(monkeypatch) -> None:
    from opensandbox.exceptions import SandboxConnectionException

    async def fake_connect(cls, sandbox_id, connection_config=None, **kwargs):  # noqa: ANN001
        del sandbox_id, connection_config, kwargs
        raise SandboxConnectionException("connect failed")

    _patch_connect(monkeypatch, fake_connect)
    provider = OpenSandboxProvider()
    with pytest.raises(SandboxAttachFailed, match="connect"):
        await provider.attach_existing("sandbox-1")
    assert provider._sandboxes == {}


async def test_detach_local_closes_only_local_client(monkeypatch) -> None:
    closed = False
    destroyed = False

    class _Client:
        async def close(self) -> None:
            nonlocal closed
            closed = True

        async def destroy(self) -> None:
            nonlocal destroyed
            destroyed = True

    provider = OpenSandboxProvider()
    provider._sandboxes["sandbox-1"] = _Client()
    await provider.detach_local("sandbox-1")
    assert closed is True
    assert destroyed is False
    assert provider._sandboxes == {}


class _PolicySandbox:
    def __init__(self, policy=None, error: Exception | None = None) -> None:
        self.policy = policy
        self.error = error

    async def get_egress_policy(self):
        if self.error is not None:
            raise self.error
        return self.policy


async def test_verify_network_none_accepts_deny_policy() -> None:
    from opensandbox.models.sandboxes import NetworkPolicy, NetworkRule

    provider = OpenSandboxProvider()
    provider._sandboxes["sandbox-1"] = _PolicySandbox(
        NetworkPolicy(
            defaultAction="deny",
            egress=[NetworkRule(action="deny", target="example.com")],
        )
    )
    await provider.verify_network_none("sandbox-1")


async def test_verify_network_none_rejects_allow_rule() -> None:
    from opensandbox.models.sandboxes import NetworkPolicy, NetworkRule

    provider = OpenSandboxProvider()
    provider._sandboxes["sandbox-1"] = _PolicySandbox(
        NetworkPolicy(
            defaultAction="deny",
            egress=[NetworkRule(action="allow", target="example.com")],
        )
    )
    with pytest.raises(SandboxNetworkPolicyViolation, match="non-deny"):
        await provider.verify_network_none("sandbox-1")


async def test_verify_network_none_rejects_default_allow() -> None:
    from opensandbox.models.sandboxes import NetworkPolicy

    provider = OpenSandboxProvider()
    provider._sandboxes["sandbox-1"] = _PolicySandbox(
        NetworkPolicy(defaultAction="allow", egress=[])
    )
    with pytest.raises(SandboxNetworkPolicyViolation, match="default action"):
        await provider.verify_network_none("sandbox-1")


async def test_verify_network_none_read_failure_is_unavailable() -> None:
    provider = OpenSandboxProvider()
    provider._sandboxes["sandbox-1"] = _PolicySandbox(error=RuntimeError("policy endpoint down"))
    with pytest.raises(SandboxNetworkPolicyUnavailable, match="cannot read"):
        await provider.verify_network_none("sandbox-1")


def test_control_plane_digest_excludes_credentials_and_client_timeouts() -> None:
    first = OpenSandboxProvider(
        OpenSandboxOptions(endpoint="LOCALHOST:8080", protocol="http", use_server_proxy=True),
        api_key="super-secret",
    )
    second = OpenSandboxProvider(
        OpenSandboxOptions(
            endpoint="localhost:8080",
            protocol="http",
            use_server_proxy=True,
            request_timeout_seconds=1,
        ),
        api_key="different-secret",
    )
    different = OpenSandboxProvider(
        OpenSandboxOptions(endpoint="localhost:9090", protocol="http", use_server_proxy=True)
    )
    assert first.continuity_identity_digest() == second.continuity_identity_digest()
    assert first.continuity_identity_digest() != different.continuity_identity_digest()
    assert "super-secret" not in first.continuity_identity_digest()
