"""Deterministic verification engine.

The engine is pure data-in/data-out: a :class:`VerificationSpec`, an exported workspace snapshot
and an optional sandbox runner. It never mutates the workspace, never touches the hidden oracle and
never changes the run's own success classification.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from atlasos.verification import code_checks, file_checks, models, structured_checks, text_checks
from atlasos.verification.dynamic import DynamicOutcome
from atlasos.verification.files import snapshot_digest

_STATIC = {
    **file_checks.CHECKS,
    **code_checks.CHECKS,
    **text_checks.CHECKS,
    **structured_checks.CHECKS,
}


class DynamicContractRunner(Protocol):
    async def run(
        self, *, snapshot: Path, cases: Sequence[Mapping[str, Any]]
    ) -> DynamicOutcome: ...


class VerificationEngine:
    def __init__(self, dynamic: DynamicContractRunner | None = None) -> None:
        self.dynamic = dynamic

    # -- public API ------------------------------------------------------
    def verify(
        self,
        spec: models.VerificationSpec,
        snapshot: Path,
        *,
        baseline_digests: Mapping[str, str] | None = None,
        recorded_digests: Mapping[str, str] | None = None,
    ) -> models.VerificationResult:
        return asyncio.run(
            self.verify_async(
                spec,
                snapshot,
                baseline_digests=baseline_digests,
                recorded_digests=recorded_digests,
            )
        )

    async def verify_async(
        self,
        spec: models.VerificationSpec,
        snapshot: Path,
        *,
        baseline_digests: Mapping[str, str] | None = None,
        recorded_digests: Mapping[str, str] | None = None,
    ) -> models.VerificationResult:
        started = time.perf_counter()
        cpu_started = time.process_time()
        artifact_digest = await asyncio.to_thread(_digest_or_empty, snapshot)
        result = models.VerificationResult(
            spec_id=spec.spec_id,
            spec_version=spec.version,
            spec_digest=spec.declared_digest or spec.digest,
            deliverable_type=spec.deliverable_type,
            artifact_digest=artifact_digest,
            dynamic_mode="sandbox" if self.dynamic is not None else "unavailable",
        )
        if not spec.is_intact():
            result.error = "verification spec digest does not match its content"
            for category in models.CATEGORIES:
                result.findings.append(
                    models.VerificationFinding(
                        check_id=f"{spec.spec_id}:integrity",
                        category=category,
                        status="FAIL",
                        reason_code=models.REASON_SPEC_TAMPERED,
                        detail="the acceptance contract was modified after it was frozen",
                    )
                )
            result.verification_wall_ms = _elapsed(started)
            result.verification_cpu_ms = _cpu_elapsed(cpu_started)
            return result
        if not artifact_digest:
            result.findings.append(
                models.VerificationFinding(
                    check_id=f"{spec.spec_id}:snapshot",
                    category="PRESENCE",
                    status="FAIL",
                    reason_code=models.REASON_MISSING,
                    detail="no workspace snapshot was exported for verification",
                )
            )
            result.verification_wall_ms = _elapsed(started)
            result.verification_cpu_ms = _cpu_elapsed(cpu_started)
            return result

        extra = {
            "baseline_digests": dict(baseline_digests or {}),
            "recorded_digests": dict(recorded_digests or {}),
        }
        for check in spec.checks:
            if check.kind in _STATIC:
                finding = self._run_static(check, snapshot, extra)
            elif check.kind in models.DYNAMIC_KINDS:
                finding = await self._run_dynamic(check, spec, snapshot)
            else:
                finding = _unknown(check, models.REASON_UNSUPPORTED, f"unknown kind {check.kind}")
            result.findings.append(finding)
        result.verification_wall_ms = _elapsed(started)
        result.verification_cpu_ms = _cpu_elapsed(cpu_started)
        return result

    # -- internals -------------------------------------------------------
    def _run_static(
        self, check: models.CheckSpec, snapshot: Path, extra: Mapping[str, Any]
    ) -> models.VerificationFinding:
        if not check.authoritative:
            return _unknown(
                check,
                models.REASON_NON_AUTHORITATIVE,
                "this rule cannot be decided deterministically from a public source",
            )
        function = _STATIC[check.kind]
        try:
            return function(check, snapshot, **extra)
        except Exception as error:  # noqa: BLE001 - a broken check is UNKNOWN, never PASS
            return _unknown(check, models.REASON_UNSUPPORTED, f"{type(error).__name__}: {error}")

    async def _run_dynamic(
        self, check: models.CheckSpec, spec: models.VerificationSpec, snapshot: Path
    ) -> models.VerificationFinding:
        if self.dynamic is None:
            return _unknown(
                check,
                models.REASON_DYNAMIC_UNAVAILABLE,
                "no sandbox runner was configured; dynamic public checks were not executed",
            )
        cases = list(check.params.get("cases") or [])
        outcome = await self.dynamic.run(snapshot=snapshot, cases=cases)
        return models.VerificationFinding(
            check_id=check.check_id,
            category=check.category,
            status=outcome.status,
            reason_code=outcome.reason_code,
            artifact=check.artifact,
            detail=outcome.detail,
            evidence_digest=models.canonical_digest(
                {"spec": spec.digest, "cases": cases, "sandbox": outcome.sandbox_id}
            ),
        )


def _unknown(check: models.CheckSpec, reason: str, detail: str) -> models.VerificationFinding:
    return models.VerificationFinding(
        check_id=check.check_id,
        category=check.category,
        status="UNKNOWN",
        reason_code=reason,
        artifact=check.artifact,
        detail=detail,
    )


def _elapsed(started: float) -> float:
    return round((time.perf_counter() - started) * 1000.0, 3)


def _cpu_elapsed(started: float) -> float:
    """Process CPU time, measured on the CPU clock rather than the wall clock."""

    return round((time.process_time() - started) * 1000.0, 3)


def _digest_or_empty(snapshot: Path) -> str:
    return snapshot_digest(snapshot) if snapshot.is_dir() else ""


def verify_spec(
    spec: models.VerificationSpec,
    snapshot: Path,
    *,
    baseline_digests: Mapping[str, str] | None = None,
    recorded_digests: Mapping[str, str] | None = None,
    dynamic: DynamicContractRunner | None = None,
) -> models.VerificationResult:
    """Convenience wrapper for callers that are not already inside an event loop."""

    return VerificationEngine(dynamic).verify(
        spec,
        snapshot,
        baseline_digests=baseline_digests,
        recorded_digests=recorded_digests,
    )


__all__ = ["DynamicContractRunner", "VerificationEngine", "verify_spec"]
