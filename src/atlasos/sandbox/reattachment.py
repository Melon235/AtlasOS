"""Explicit M5 verified-reattachment seam.

Inspection must be possible without connecting. The provider returns a small normalized
observation; M5 decides whether the durable binding and the observed remote resource are the same
sandbox. ``Sandbox.connect`` is issued only after that decision.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from atlasos.sandbox.models import SandboxBindingError

REMOTE_RUNNING = "RUNNING"
REMOTE_TERMINAL = frozenset({"TERMINATED"})


class SandboxReattachmentObservation(BaseModel):
    """Provider-specific inspection translated into M5-safe fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sandbox_id: str
    state: str
    created_at: datetime
    metadata: dict[str, str]
    metadata_digest: str


class SandboxReattachmentError(SandboxBindingError):
    pass


class SandboxReattachmentUnsupported(SandboxReattachmentError):
    pass


class SandboxProviderUnavailable(SandboxReattachmentError):
    """The control plane could not give a trustworthy answer; durable state is unchanged."""


class SandboxRemoteNotFound(SandboxReattachmentError):
    """The trusted control plane definitively reports that the sandbox no longer exists."""


class SandboxRemoteTerminal(SandboxReattachmentError):
    """The trusted control plane reports a definitive terminal provider state."""


class SandboxRemoteNotAttachable(SandboxReattachmentError):
    """The sandbox exists but is not currently safe to attach; durable state is unchanged."""


class SandboxLegacyContinuityUnavailable(SandboxReattachmentError):
    """A Stage A record lacks provider-side continuity evidence."""


class SandboxContinuityMismatch(SandboxReattachmentError):
    """Provider/config evidence does not match the durable binding; do not connect."""


class SandboxConfigMismatch(SandboxContinuityMismatch):
    pass


class SandboxCreatedAtMismatch(SandboxContinuityMismatch):
    pass


class SandboxMetadataMismatch(SandboxContinuityMismatch):
    pass


class SandboxAttachFailed(SandboxReattachmentError):
    """Verification passed, but the provider could not establish a local connection."""


class SandboxNetworkPolicyError(SandboxBindingError):
    pass


class SandboxNetworkPolicyUnavailable(SandboxNetworkPolicyError):
    """The effective network policy could not be read or proved."""


class SandboxNetworkPolicyViolation(SandboxNetworkPolicyError):
    """The effective network policy does not satisfy Atlas network=none."""


@runtime_checkable
class SandboxReattachmentProvider(Protocol):
    def continuity_identity_digest(self) -> str: ...

    async def inspect_existing(self, sandbox_id: str) -> SandboxReattachmentObservation: ...

    async def attach_existing(self, sandbox_id: str) -> None: ...

    async def detach_local(self, sandbox_id: str) -> None: ...

    async def verify_network_none(self, sandbox_id: str) -> None: ...


def normalize_remote_state(value: str) -> str:
    return str(value).strip().upper()


def normalize_provider_created_at(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def normalize_atlas_metadata(metadata: Mapping[str, str] | None) -> dict[str, str]:
    """Project provider metadata onto the Atlas correlation fields used by M5."""

    values = dict(metadata or {})
    aliases = {
        "run_id": ("atlas.run_id", "atlas_run_id"),
        "task_id": ("atlas.task_id", "atlas_task_id"),
        "worker_id": ("atlas.worker_id", "atlas_worker_id"),
        "provider": ("atlas.provider",),
        "correlation_id": ("atlas.correlation_id", "atlas_correlation_id"),
    }
    normalized: dict[str, str] = {}
    for field, keys in aliases.items():
        for key in keys:
            value = values.get(key)
            if value is not None:
                normalized[field] = str(value)
                break
    return normalized


def atlas_metadata_digest(metadata: Mapping[str, str] | None) -> str:
    relevant = {
        str(key): str(value)
        for key, value in (metadata or {}).items()
        if str(key).startswith("atlas")
    }
    encoded = json.dumps(
        relevant,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def compute_control_plane_digest(
    *, provider_name: str, base_url: str, use_server_proxy: bool
) -> str:
    payload = {
        "provider_name": provider_name,
        "base_url": base_url.rstrip("/").lower(),
        "use_server_proxy": bool(use_server_proxy),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


__all__ = [
    "REMOTE_RUNNING",
    "REMOTE_TERMINAL",
    "SandboxAttachFailed",
    "SandboxConfigMismatch",
    "SandboxContinuityMismatch",
    "SandboxCreatedAtMismatch",
    "SandboxLegacyContinuityUnavailable",
    "SandboxMetadataMismatch",
    "SandboxNetworkPolicyError",
    "SandboxNetworkPolicyUnavailable",
    "SandboxNetworkPolicyViolation",
    "SandboxProviderUnavailable",
    "SandboxReattachmentError",
    "SandboxReattachmentObservation",
    "SandboxReattachmentProvider",
    "SandboxReattachmentUnsupported",
    "SandboxRemoteNotAttachable",
    "SandboxRemoteNotFound",
    "SandboxRemoteTerminal",
    "atlas_metadata_digest",
    "compute_control_plane_digest",
    "normalize_atlas_metadata",
    "normalize_provider_created_at",
    "normalize_remote_state",
]
