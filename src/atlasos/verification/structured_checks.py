"""Checks for structured business deliverables (JSON business artifacts).

This is the part of the verifier that is not about code at all: it validates a documented business
artifact against a Host-owned schema and the arithmetic and hard constraints the task published.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from atlasos.verification import models
from atlasos.verification.files import UnsafeArtifactPath, read_text

_TYPE_CHECKS = {
    "string": lambda value: isinstance(value, str),
    "boolean": lambda value: isinstance(value, bool),
    "integer": lambda value: isinstance(value, int) and not isinstance(value, bool),
    "number": lambda value: isinstance(value, int | float) and not isinstance(value, bool),
    "array": lambda value: isinstance(value, list),
    "object": lambda value: isinstance(value, dict),
}


def _finding(
    check: models.CheckSpec,
    status: models.Status,
    reason: str,
    detail: str = "",
) -> models.VerificationFinding:
    return models.VerificationFinding(
        check_id=check.check_id,
        category=check.category,
        status=status,
        reason_code=reason,
        artifact=check.artifact,
        detail=detail,
    )


def _load(
    check: models.CheckSpec, snapshot: Path
) -> tuple[dict[str, Any] | None, models.VerificationFinding | None]:
    artifact = check.artifact or ""
    try:
        payload = json.loads(read_text(snapshot, artifact))
    except FileNotFoundError:
        return None, _finding(check, "FAIL", models.REASON_MISSING)
    except (UnsafeArtifactPath, UnicodeDecodeError) as error:
        return None, _finding(check, "FAIL", models.REASON_UNREADABLE, str(error))
    except json.JSONDecodeError as error:
        return None, _finding(
            check, "FAIL", models.REASON_PARSE_ERROR, f"{error.msg} at line {error.lineno}"
        )
    if not isinstance(payload, dict):
        return None, _finding(
            check, "FAIL", models.REASON_SCHEMA_VIOLATION, "expected a JSON object"
        )
    return payload, None


def check_json_schema(
    check: models.CheckSpec, snapshot: Path, **_: Any
) -> models.VerificationFinding:
    payload, failure = _load(check, snapshot)
    if failure is not None:
        return failure
    assert payload is not None
    problems: list[str] = []
    for field_name, type_name in (check.params.get("required") or {}).items():
        if field_name not in payload:
            problems.append(f"{field_name}: missing")
            continue
        predicate = _TYPE_CHECKS.get(str(type_name))
        if predicate is None:
            problems.append(f"{field_name}: unsupported type {type_name!r}")
        elif not predicate(payload[field_name]):
            problems.append(f"{field_name}: expected {type_name}")
    for field_name in check.params.get("forbidden") or []:
        if field_name in payload:
            problems.append(f"{field_name}: must not be present")
    if problems:
        return _finding(check, "FAIL", models.REASON_SCHEMA_VIOLATION, "; ".join(problems))
    return _finding(check, "PASS", models.REASON_OK)


def check_arithmetic(
    check: models.CheckSpec, snapshot: Path, **_: Any
) -> models.VerificationFinding:
    payload, failure = _load(check, snapshot)
    if failure is not None:
        return failure
    assert payload is not None
    target = str(check.params.get("target") or "")
    parts = [str(item) for item in check.params.get("sum_of") or []]
    values: list[float] = []
    for name in [target, *parts]:
        value = payload.get(name)
        if not isinstance(value, int | float) or isinstance(value, bool):
            return _finding(
                check,
                "UNKNOWN",
                models.REASON_SCHEMA_VIOLATION,
                f"{name} is not numeric, arithmetic cannot be evaluated",
            )
        values.append(float(value))
    expected = round(sum(values[1:]), 6)
    actual = round(values[0], 6)
    if actual != expected:
        return _finding(
            check,
            "FAIL",
            models.REASON_ARITHMETIC_MISMATCH,
            f"{target}={actual} but {'+'.join(parts)}={expected}",
        )
    return _finding(check, "PASS", models.REASON_OK, detail=f"{target}={actual}")


def check_numeric_range(
    check: models.CheckSpec, snapshot: Path, **_: Any
) -> models.VerificationFinding:
    payload, failure = _load(check, snapshot)
    if failure is not None:
        return failure
    assert payload is not None
    problems: list[str] = []
    for field_name, bounds in (check.params.get("ranges") or {}).items():
        value = payload.get(field_name)
        low, high = bounds
        if not isinstance(value, int | float) or isinstance(value, bool):
            problems.append(f"{field_name}: not numeric")
            continue
        if (low is not None and value < low) or (high is not None and value > high):
            problems.append(f"{field_name}={value} outside [{low}, {high}]")
    if problems:
        return _finding(check, "FAIL", models.REASON_OUT_OF_RANGE, "; ".join(problems))
    return _finding(check, "PASS", models.REASON_OK)


def check_constraint(
    check: models.CheckSpec, snapshot: Path, **_: Any
) -> models.VerificationFinding:
    """A published hard limit: ``field <= limit`` (or ``>=``)."""

    payload, failure = _load(check, snapshot)
    if failure is not None:
        return failure
    assert payload is not None
    field_name = str(check.params.get("field") or "")
    relation = str(check.params.get("relation") or "le")
    limit = check.params.get("limit")
    value = payload.get(field_name)
    if not isinstance(value, int | float) or isinstance(value, bool):
        return _finding(
            check, "UNKNOWN", models.REASON_SCHEMA_VIOLATION, f"{field_name} is not numeric"
        )
    if not isinstance(limit, int | float) or isinstance(limit, bool):
        return _unknown_limit(check, field_name)
    ok = value <= limit if relation == "le" else value >= limit
    if not ok:
        return _finding(
            check,
            "FAIL",
            models.REASON_CONSTRAINT_VIOLATED,
            f"{field_name}={value} violates {field_name} {relation} {limit}",
        )
    return _finding(check, "PASS", models.REASON_OK, detail=f"{field_name}={value}")


def _unknown_limit(check: models.CheckSpec, field_name: str) -> models.VerificationFinding:
    return _finding(
        check,
        "UNKNOWN",
        models.REASON_NON_AUTHORITATIVE,
        f"{field_name}: the published limit is not numeric",
    )


def check_evidence_refs(
    check: models.CheckSpec, snapshot: Path, **_: Any
) -> models.VerificationFinding:
    """Every cited reference must point at a registered source document."""

    payload, failure = _load(check, snapshot)
    if failure is not None:
        return failure
    assert payload is not None
    field_name = str(check.params.get("field") or "evidence_refs")
    registry = {str(item) for item in check.params.get("registry") or []}
    refs = payload.get(field_name)
    if not isinstance(refs, list) or not all(isinstance(item, str) for item in refs):
        return _finding(
            check, "FAIL", models.REASON_SCHEMA_VIOLATION, f"{field_name} must be a list of strings"
        )
    minimum = int(check.params.get("min_refs", 1))
    if len(refs) < minimum:
        return _finding(
            check, "FAIL", models.REASON_EVIDENCE_MISSING_REF, f"fewer than {minimum} reference(s)"
        )
    if not registry:
        return _finding(
            check,
            "UNKNOWN",
            models.REASON_NON_AUTHORITATIVE,
            "no evidence registry was published for this task",
        )
    unknown = sorted({ref.split(":", 1)[0] for ref in refs} - registry)
    if unknown:
        return _finding(
            check, "FAIL", models.REASON_EVIDENCE_MISSING_REF, f"unregistered sources: {unknown}"
        )
    return _finding(
        check, "PASS", models.REASON_OK, detail=f"{len(refs)} ref(s) resolve to a known source"
    )


CHECKS = {
    models.CheckKind.JSON_SCHEMA: check_json_schema,
    models.CheckKind.ARITHMETIC: check_arithmetic,
    models.CheckKind.NUMERIC_RANGE: check_numeric_range,
    models.CheckKind.CONSTRAINT: check_constraint,
    models.CheckKind.EVIDENCE_REFS: check_evidence_refs,
}

__all__ = ["CHECKS"]
