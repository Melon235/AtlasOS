"""Deterministic deliverable verification for AtlasRuntime.

Public surface:

* :class:`~atlasos.verification.models.VerificationSpec` — a Host-owned, digested acceptance
  contract built from trusted task definitions.
* :class:`~atlasos.verification.engine.VerificationEngine` — runs the contract over an exported
  workspace snapshot without executing untrusted code on the host.
* :class:`~atlasos.verification.dynamic.SandboxContractRunner` — optional, sandbox-only execution
  of published examples.
"""

from __future__ import annotations

from atlasos.verification.dynamic import DynamicOutcome, SandboxContractRunner, render_public_tests
from atlasos.verification.engine import DynamicContractRunner, VerificationEngine, verify_spec
from atlasos.verification.models import (
    CATEGORIES,
    CheckKind,
    CheckSpec,
    VerificationFinding,
    VerificationResult,
    VerificationSpec,
    canonical_digest,
)

__all__ = [
    "CATEGORIES",
    "CheckKind",
    "CheckSpec",
    "DynamicContractRunner",
    "DynamicOutcome",
    "SandboxContractRunner",
    "VerificationEngine",
    "VerificationFinding",
    "VerificationResult",
    "VerificationSpec",
    "canonical_digest",
    "render_public_tests",
    "verify_spec",
]
