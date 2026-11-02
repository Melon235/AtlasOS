"""Trusted construction and capability checks for Atlas Sandbox providers.

Atlas M5 consumes the AnyCode ``SandboxProvider`` protocol directly. This module owns only the
deployment-side selection of that provider and the fail-closed checks applied before a project
workload is allowed to create a sandbox. It is not a second sandbox abstraction and it never
imports a provider SDK directly.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from typing import Any

from anycode.sandbox import SandboxCapabilities, SandboxProvider, create_sandbox_provider

from atlasos.sandbox.models import SandboxCapabilityError, SandboxCredentialError

# A real Atlas Sandbox must be remotely isolated. The fake provider used by deterministic
# regression tests is deliberately excluded from this set.
REAL_SANDBOX_PROVIDERS: frozenset[str] = frozenset({"daytona"})

# AnyCode reports ``allowlist`` for providers that can enforce both a CIDR/domain allowlist and a
# complete block. ``none`` is the subset Atlas M5 requires; ``unrestricted`` cannot satisfy it.
_NETWORK_NONE_CAPABLE = frozenset({"none", "allowlist"})
_ISOLATED = frozenset({"container", "microvm", "vm", "remote"})


def build_sandbox_provider(
    name: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> SandboxProvider:
    """Construct a provider selected by trusted host configuration.

    Credentials stay in the SDK's environment/configuration. They are checked for presence only;
    their values are never read into Atlas state, labels, commands, or evidence.
    """

    if name not in REAL_SANDBOX_PROVIDERS:
        raise SandboxCredentialError(
            f"sandbox provider {name!r} is not a configured real Atlas Sandbox provider; "
            f"supported providers: {sorted(REAL_SANDBOX_PROVIDERS)}"
        )
    env = os.environ if environ is None else environ
    api_key = env.get("DAYTONA_API_KEY")
    jwt_token = env.get("DAYTONA_JWT_TOKEN")
    organization_id = env.get("DAYTONA_ORGANIZATION_ID")
    if not api_key and not (jwt_token and organization_id):
        raise SandboxCredentialError(
            "Daytona credentials are not configured in the host environment; set "
            "DAYTONA_API_KEY (or DAYTONA_JWT_TOKEN and DAYTONA_ORGANIZATION_ID). "
            "Atlas will not fall back to a fake provider."
        )
    return create_sandbox_provider(name)


def is_real_sandbox_provider(
    provider: SandboxProvider, *, expected_name: str | None = None
) -> bool:
    """Return whether the provider is a declared remote Atlas Sandbox provider."""

    capabilities = provider.capabilities()
    name = capabilities.provider
    if expected_name is not None and name != expected_name:
        return False
    return name in REAL_SANDBOX_PROVIDERS and capabilities.isolation in _ISOLATED


def require_real_sandbox_provider(
    provider: SandboxProvider, *, expected_name: str | None = None
) -> None:
    capabilities = provider.capabilities()
    if not is_real_sandbox_provider(provider, expected_name=expected_name):
        expected = expected_name or "a real remote provider"
        raise SandboxCapabilityError(
            "real sandbox provider is required; "
            f"selected provider={capabilities.provider!r} isolation={capabilities.isolation!r}, "
            f"expected {expected}"
        )


def capability_summary(capabilities: SandboxCapabilities) -> dict[str, Any]:
    """Safe, secret-free capability projection for metrics and M7 evidence."""

    return {
        "provider": capabilities.provider,
        "contract_version": capabilities.contract_version,
        "isolation": capabilities.isolation,
        "networking": capabilities.networking,
        "persistent_filesystem": capabilities.persistent_filesystem,
        "snapshots": capabilities.snapshots,
        "command_streaming": capabilities.command_streaming,
        "cancellation": capabilities.cancellation,
        "file_transfer": capabilities.file_transfer,
        "evidence": capabilities.evidence,
        "limitations": list(capabilities.limitations),
    }


def capability_digest(capabilities: SandboxCapabilities) -> str:
    payload = json.dumps(
        capability_summary(capabilities), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def capability_failures(
    capabilities: SandboxCapabilities, *, require_network_none: bool = True
) -> tuple[str, ...]:
    """List requirements that prevent an Atlas project workload from starting."""

    failures: list[str] = []
    if capabilities.isolation not in _ISOLATED:
        failures.append(f"isolation={capabilities.isolation!r} is not remote/isolated")
    if require_network_none and capabilities.networking not in _NETWORK_NONE_CAPABLE:
        failures.append(f"networking={capabilities.networking!r} cannot enforce network='none'")
    if not capabilities.persistent_filesystem:
        failures.append("persistent_filesystem is required")
    if not capabilities.file_transfer:
        failures.append("file_transfer is required")
    if capabilities.contract_version != "1.0":
        failures.append(f"unsupported sandbox contract {capabilities.contract_version!r}")
    return tuple(failures)


def require_capabilities(capabilities: SandboxCapabilities) -> None:
    failures = capability_failures(capabilities)
    if failures:
        raise SandboxCapabilityError(
            "sandbox provider cannot satisfy Atlas M5 requirements: " + "; ".join(failures)
        )
