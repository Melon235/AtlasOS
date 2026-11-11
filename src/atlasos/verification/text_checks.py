"""STRUCTURAL/PRESENCE checks for Markdown report deliverables.

The report task publishes an exact line shape, so shape and coverage are decidable. Whether a
prose answer is *semantically* right is not decidable here and is reported as UNKNOWN by the spec,
never as a keyword-based PASS.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from atlasos.verification import models
from atlasos.verification.files import UnsafeArtifactPath, read_text


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
) -> tuple[list[str] | None, models.VerificationFinding | None]:
    artifact = check.artifact or ""
    try:
        text = read_text(snapshot, artifact)
    except FileNotFoundError:
        return None, _finding(check, "FAIL", models.REASON_MISSING)
    except (UnsafeArtifactPath, UnicodeDecodeError) as error:
        return None, _finding(check, "FAIL", models.REASON_UNREADABLE, str(error))
    return text.splitlines(), None


def check_section(check: models.CheckSpec, snapshot: Path, **_: Any) -> models.VerificationFinding:
    lines, failure = _load(check, snapshot)
    if failure is not None:
        return failure
    assert lines is not None
    heading = str(check.params.get("heading") or "").strip()
    present = any(line.strip() == heading for line in lines)
    if not present:
        return _finding(check, "FAIL", models.REASON_SECTION_MISSING, f"missing {heading!r}")
    return _finding(check, "PASS", models.REASON_OK)


def _section_body(lines: list[str], heading: str | None) -> list[str]:
    if not heading:
        return [line for line in lines if line.strip()]
    body: list[str] = []
    inside = False
    for line in lines:
        stripped = line.strip()
        if stripped == heading:
            inside = True
            continue
        if inside and stripped.startswith("## "):
            break
        if inside and stripped:
            body.append(stripped)
    return body


def _normalize(text: str) -> str:
    return " ".join(str(text).replace("'", '"').split())


def check_lines(check: models.CheckSpec, snapshot: Path, **_: Any) -> models.VerificationFinding:
    lines, failure = _load(check, snapshot)
    if failure is not None:
        return failure
    assert lines is not None
    section = check.params.get("section")
    body = _section_body(lines, str(section) if section else None)
    labels = [str(item) for item in check.params.get("labels") or []]
    required = [str(item) for item in check.params.get("required_substrings") or []]
    constants = dict(check.params.get("constants") or {})
    examples = dict(check.params.get("examples") or {})

    problems: list[str] = []
    for label in labels:
        matches = [
            line for line in body if line.startswith(f"{label}:") or line.startswith(label + " ")
        ]
        if not matches:
            problems.append(f"{label}: no line")
            continue
        if len(matches) > 1:
            problems.append(f"{label}: duplicated")
        for substring in required:
            if substring not in matches[0]:
                problems.append(f"{label}: missing {substring!r}")
        expected_example = examples.get(label)
        if expected_example:
            actual_line = _normalize(matches[0])
            call = _normalize(str(expected_example.get("call") or ""))
            value = _normalize(str(expected_example.get("value") or ""))
            if call and call not in actual_line:
                problems.append(f"{label}: example call {call!r} not reported")
            if value and not actual_line.rstrip().endswith("-> " + value):
                problems.append(f"{label}: reported value is not {value!r}")
    for name, value in constants.items():
        expected = _normalize(f"{name} = {value}")
        if not any(_normalize(line) == expected for line in body):
            problems.append(f"constant {name}: expected {expected!r}")
    if problems:
        return _finding(check, "FAIL", models.REASON_SHAPE_MISMATCH, "; ".join(problems))
    return _finding(
        check,
        "PASS",
        models.REASON_OK,
        detail=f"{len(labels)} labelled line(s), {len(constants)} constant(s)",
    )


CHECKS = {
    models.CheckKind.TEXT_SECTION: check_section,
    models.CheckKind.TEXT_LINES: check_lines,
}

__all__ = ["CHECKS"]
