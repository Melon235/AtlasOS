"""Capability scope syntax, canonicalization, effective scopes, and context hashing.

Capabilities are atomic ``<domain>.<action>`` strings. No wildcard, resource qualifier, or
inheritance exists in M2.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from datetime import UTC, datetime

from anycode.identity.context import ExecutionContext

_SCOPE = re.compile(r"^[a-z][a-z0-9_-]*\.[a-z][a-z0-9_-]*$")
_AGENT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


class InvalidScope(ValueError):
    """A capability does not match the M2 atomic scope syntax."""


class InvalidActor(ValueError):
    """An actor/delegatee cannot be canonicalized."""


def canonicalize_scopes(scopes: Iterable[str]) -> tuple[str, ...]:
    """Validate, deduplicate, and stable-sort scopes."""

    normalized: list[str] = []
    for raw in scopes:
        if not isinstance(raw, str):
            raise InvalidScope(f"scope must be a string, got {raw!r}")
        scope = raw.strip().lower()
        if not _SCOPE.fullmatch(scope):
            raise InvalidScope(
                f"invalid scope {raw!r}; expected '<domain>.<action>' with no wildcard/resource"
            )
        normalized.append(scope)
    return tuple(sorted(set(normalized)))


def canonical_actor(name: str) -> str:
    """Canonical ``agent:<name>`` identity used in delegation grants."""

    if not isinstance(name, str) or not name.strip():
        raise InvalidActor("agent name must be a non-empty string")
    value = name.strip()
    if value.startswith("agent:"):
        value = value[len("agent:") :]
    elif ":" in value:
        raise InvalidActor(f"only agent identities may be delegated to: {name!r}")
    if not _AGENT_NAME.fullmatch(value):
        raise InvalidActor(f"invalid agent name: {name!r}")
    return f"agent:{value}"


def effective_scopes(context: ExecutionContext) -> frozenset[str]:
    """Intersection of every grant's scopes; empty when there is no delegation."""

    grants = context.delegation
    if not grants:
        return frozenset()
    scopes = [frozenset(grant.scopes) for grant in grants]
    return frozenset.intersection(*scopes)


def derive_child_expiry(context: ExecutionContext) -> datetime | None:
    """Child expiry can never outlive the earliest non-null ancestor expiry."""

    expiries = [grant.expires_at for grant in context.delegation if grant.expires_at is not None]
    return min(expiries) if expiries else None


def expired_grants(
    context: ExecutionContext,
    *,
    now: datetime | None = None,
) -> tuple[int, ...]:
    current = now or datetime.now(UTC)
    return tuple(
        index
        for index, grant in enumerate(context.delegation)
        if grant.expires_at is not None and grant.expires_at <= current
    )


def context_hash(context: ExecutionContext) -> str:
    """Stable hash of the whole trusted execution context, including its chain."""

    payload = json.dumps(
        context.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
