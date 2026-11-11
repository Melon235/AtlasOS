"""PRESENCE-level checks: does the deliverable exist, is it readable and unchanged when frozen."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from atlasos.verification import models
from atlasos.verification.files import (
    UnsafeArtifactPath,
    file_digest,
    read_text,
    resolve,
)


def _finding(
    check: models.CheckSpec,
    status: models.Status,
    reason: str,
    detail: str = "",
    evidence: str | None = None,
) -> models.VerificationFinding:
    return models.VerificationFinding(
        check_id=check.check_id,
        category=check.category,
        status=status,
        reason_code=reason,
        artifact=check.artifact,
        detail=detail,
        evidence_digest=evidence,
    )


def check_present(check: models.CheckSpec, snapshot: Path, **_: Any) -> models.VerificationFinding:
    artifact = check.artifact or ""
    try:
        path = resolve(snapshot, artifact)
    except UnsafeArtifactPath as error:
        return _finding(check, "FAIL", models.REASON_WRONG_TYPE, str(error))
    if check.params.get("expect") == "directory":
        if path.is_dir() and not path.is_symlink():
            return _finding(check, "PASS", models.REASON_OK)
        return _finding(check, "FAIL", models.REASON_MISSING, f"{artifact} is not a directory")
    if not path.exists() or path.is_symlink():
        return _finding(check, "FAIL", models.REASON_MISSING, f"{artifact} does not exist")
    if not path.is_file():
        return _finding(check, "FAIL", models.REASON_WRONG_TYPE, f"{artifact} is not a file")
    return _finding(check, "PASS", models.REASON_OK, evidence=file_digest(snapshot, artifact))


def check_readable(check: models.CheckSpec, snapshot: Path, **_: Any) -> models.VerificationFinding:
    artifact = check.artifact or ""
    min_chars = int(check.params.get("min_chars", 1))
    try:
        text = read_text(snapshot, artifact)
    except FileNotFoundError:
        return _finding(check, "FAIL", models.REASON_MISSING)
    except (UnsafeArtifactPath, UnicodeDecodeError) as error:
        return _finding(check, "FAIL", models.REASON_UNREADABLE, str(error))
    if len(text.strip()) < min_chars:
        return _finding(
            check, "FAIL", models.REASON_EMPTY, f"fewer than {min_chars} non-space char(s)"
        )
    return _finding(check, "PASS", models.REASON_OK, evidence=file_digest(snapshot, artifact))


def check_unchanged(
    check: models.CheckSpec,
    snapshot: Path,
    *,
    baseline_digests: Mapping[str, str] | None = None,
    **_,
) -> models.VerificationFinding:
    """A frozen contract file must keep its baseline digest."""

    artifact = check.artifact or ""
    baseline: Mapping[str, str] = baseline_digests or {}
    expected = baseline.get(artifact)
    if expected is None:
        return _finding(
            check,
            "UNKNOWN",
            models.REASON_NON_AUTHORITATIVE,
            "no baseline digest was recorded for this frozen artifact",
        )
    try:
        actual = file_digest(snapshot, artifact).removeprefix("sha256:")
    except FileNotFoundError:
        return _finding(check, "FAIL", models.REASON_MISSING, "frozen artifact is gone")
    if actual != expected:
        return _finding(
            check,
            "FAIL",
            models.REASON_UNEXPECTED_CHANGE,
            "frozen artifact content changed",
            evidence="sha256:" + actual,
        )
    return _finding(check, "PASS", models.REASON_OK, evidence="sha256:" + actual)


def check_artifact_recorded(
    check: models.CheckSpec,
    snapshot: Path,
    *,
    recorded_digests: Mapping[str, str] | None = None,
    **_,
) -> models.VerificationFinding:
    """The artifact must match the digest the worker's own tool calls produced (anti-tamper)."""

    artifact = check.artifact or ""
    recorded: Mapping[str, str] = recorded_digests or {}
    expected = recorded.get(artifact)
    if expected is None:
        return _finding(
            check,
            "UNKNOWN",
            models.REASON_NOT_RECORDED,
            "the run recorded no digest for this artifact",
        )
    try:
        actual = file_digest(snapshot, artifact)
    except (FileNotFoundError, UnsafeArtifactPath) as error:
        return _finding(check, "FAIL", models.REASON_MISSING, str(error))
    if _normalize(actual) != _normalize(expected):
        return _finding(
            check,
            "FAIL",
            models.REASON_UNEXPECTED_CHANGE,
            "artifact content does not match the digest recorded during execution",
            evidence=actual,
        )
    return _finding(check, "PASS", models.REASON_OK, evidence=actual)


def _normalize(digest: str) -> str:
    return str(digest).removeprefix("sha256:").lower()


CHECKS = {
    models.CheckKind.FILE_PRESENT: check_present,
    models.CheckKind.FILE_READABLE: check_readable,
    models.CheckKind.FILE_UNCHANGED: check_unchanged,
    models.CheckKind.ARTIFACT_RECORDED: check_artifact_recorded,
}

__all__ = ["CHECKS"]
