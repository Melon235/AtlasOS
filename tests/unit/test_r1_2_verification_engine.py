"""R1-2A: engine contract, spec integrity and dynamic-verification failure semantics."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from atlasos.verification import models
from atlasos.verification.dynamic import DynamicOutcome
from atlasos.verification.engine import VerificationEngine
from atlasos.verification.models import CheckKind, CheckSpec, VerificationSpec


def _spec(*checks: CheckSpec, declared: bool = True) -> VerificationSpec:
    spec = VerificationSpec(
        spec_id="test/spec",
        version="1.0.0",
        deliverable_type="python_modules",
        required_artifacts=("module.py",),
        checks=checks,
    )
    return spec.with_declared_digest() if declared else spec


def _write(root: Path, relative: str, text: str) -> Path:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


def test_present_and_missing_artifact(tmp_path: Path) -> None:
    spec = _spec(CheckSpec("present", "PRESENCE", CheckKind.FILE_PRESENT, "module.py"))
    missing = VerificationEngine().verify(spec, tmp_path)
    assert missing.status_for("PRESENCE") == "FAIL"
    _write(tmp_path, "module.py", "x = 1\n")
    found = VerificationEngine().verify(spec, tmp_path)
    assert found.status_for("PRESENCE") == "PASS"
    assert found.overall == "PASS"


def test_tampered_spec_is_refused(tmp_path: Path) -> None:
    spec = _spec(CheckSpec("present", "PRESENCE", CheckKind.FILE_PRESENT, "module.py"))
    _write(tmp_path, "module.py", "x = 1\n")
    tampered = VerificationSpec(
        spec_id=spec.spec_id,
        version=spec.version,
        deliverable_type=spec.deliverable_type,
        required_artifacts=spec.required_artifacts,
        checks=(*spec.checks, CheckSpec("extra", "PRESENCE", CheckKind.FILE_PRESENT, "other.py")),
        declared_digest=spec.declared_digest,
    )
    result = VerificationEngine().verify(tampered, tmp_path)
    assert result.overall == "FAIL"
    codes = {finding.reason_code for finding in result.findings}
    assert models.REASON_SPEC_TAMPERED in codes


def test_artifact_digest_mismatch_fails(tmp_path: Path) -> None:
    _write(tmp_path, "module.py", "x = 1\n")
    spec = _spec(
        CheckSpec("digest", "PRESENCE", CheckKind.ARTIFACT_RECORDED, "module.py"),
    )
    result = VerificationEngine().verify(
        spec, tmp_path, recorded_digests={"module.py": "sha256:" + "0" * 64}
    )
    assert result.status_for("PRESENCE") == "FAIL"
    assert result.findings[0].reason_code == models.REASON_UNEXPECTED_CHANGE


def test_artifact_digest_match_passes(tmp_path: Path) -> None:
    _write(tmp_path, "module.py", "x = 1\n")
    from atlasos.verification.files import file_digest

    spec = _spec(CheckSpec("digest", "PRESENCE", CheckKind.ARTIFACT_RECORDED, "module.py"))
    result = VerificationEngine().verify(
        spec, tmp_path, recorded_digests={"module.py": file_digest(tmp_path, "module.py")}
    )
    assert result.status_for("PRESENCE") == "PASS"


def test_missing_snapshot_is_reported(tmp_path: Path) -> None:
    spec = _spec(CheckSpec("present", "PRESENCE", CheckKind.FILE_PRESENT, "module.py"))
    result = VerificationEngine().verify(spec, tmp_path / "absent")
    assert result.overall == "FAIL"
    assert result.findings[0].reason_code == models.REASON_MISSING


def test_non_authoritative_check_is_unknown(tmp_path: Path) -> None:
    _write(tmp_path, "module.py", "x = 1\n")
    spec = _spec(
        CheckSpec(
            "semantics",
            "PUBLIC_CONTRACT",
            CheckKind.FILE_PRESENT,
            "module.py",
            authoritative=False,
        )
    )
    result = VerificationEngine().verify(spec, tmp_path)
    assert result.status_for("PUBLIC_CONTRACT") == "UNKNOWN"
    assert result.findings[0].reason_code == models.REASON_NON_AUTHORITATIVE


class _FakeDynamic:
    def __init__(self, outcome: DynamicOutcome) -> None:
        self.outcome = outcome
        self.calls = 0

    async def run(self, *, snapshot: Path, cases):  # noqa: ANN001, ANN201
        self.calls += 1
        return self.outcome


def _dynamic_spec() -> VerificationSpec:
    return _spec(
        CheckSpec(
            "examples",
            "PUBLIC_CONTRACT",
            CheckKind.PUBLIC_EXAMPLE,
            "module.py",
            {"cases": [{"case_id": "c1", "code": "f()", "expected_literal": 1}]},
        )
    )


def test_dynamic_unavailable_is_unknown(tmp_path: Path) -> None:
    _write(tmp_path, "module.py", "def f():\n    return 1\n")
    result = VerificationEngine(None).verify(_dynamic_spec(), tmp_path)
    assert result.status_for("PUBLIC_CONTRACT") == "UNKNOWN"
    assert result.findings[0].reason_code == models.REASON_DYNAMIC_UNAVAILABLE
    assert result.dynamic_mode == "unavailable"


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (DynamicOutcome("PASS", models.REASON_OK), "PASS"),
        (DynamicOutcome("FAIL", models.REASON_SHAPE_MISMATCH, "c1"), "FAIL"),
        (
            DynamicOutcome("UNKNOWN", models.REASON_DYNAMIC_TIMEOUT, "timeout"),
            "UNKNOWN",
        ),
        (
            DynamicOutcome("UNKNOWN", models.REASON_DYNAMIC_ERROR, "sandbox down"),
            "UNKNOWN",
        ),
    ],
)
def test_dynamic_outcomes_map_to_findings(
    tmp_path: Path, outcome: DynamicOutcome, expected: str
) -> None:
    _write(tmp_path, "module.py", "def f():\n    return 1\n")
    runner = _FakeDynamic(outcome)
    result = VerificationEngine(runner).verify(_dynamic_spec(), tmp_path)
    assert result.status_for("PUBLIC_CONTRACT") == expected
    assert runner.calls == 1


def test_engine_records_wall_and_cpu_time(tmp_path: Path) -> None:
    _write(tmp_path, "module.py", "x = 1\n")
    spec = _spec(CheckSpec("present", "PRESENCE", CheckKind.FILE_PRESENT, "module.py"))
    result = VerificationEngine().verify(spec, tmp_path)
    assert result.verification_wall_ms is not None and result.verification_wall_ms >= 0
    assert result.verification_cpu_ms is not None


def test_categories_are_reported_separately(tmp_path: Path) -> None:
    _write(tmp_path, "module.py", "x = 1\n")
    spec = _spec(
        CheckSpec("present", "PRESENCE", CheckKind.FILE_PRESENT, "module.py"),
        CheckSpec("syntax", "STRUCTURAL", CheckKind.PYTHON_PARSES, "module.py"),
    )
    result = VerificationEngine().verify(spec, tmp_path)
    statuses = result.category_statuses()
    assert statuses["deliverable_presence"] == "PASS"
    assert statuses["structural_validity"] == "PASS"
    assert statuses["public_contract_status"] == "NOT_APPLICABLE"
    assert result.overall == "PASS"


def test_verify_is_usable_from_async_context(tmp_path: Path) -> None:
    _write(tmp_path, "module.py", "x = 1\n")
    spec = _spec(CheckSpec("present", "PRESENCE", CheckKind.FILE_PRESENT, "module.py"))
    result = asyncio.run(VerificationEngine().verify_async(spec, tmp_path))
    assert result.overall == "PASS"
