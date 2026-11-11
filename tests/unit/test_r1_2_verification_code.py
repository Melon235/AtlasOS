"""R1-2A: structural and public-contract checks for Python deliverables."""

from __future__ import annotations

from pathlib import Path

import pytest
from experiments.evaluation.stage_r1_2_contracts import p2_spec, p3_spec

from atlasos.verification import models
from atlasos.verification.code_checks import _signature
from atlasos.verification.dynamic import (
    DynamicOutcome,
    parse_pytest_failure,
    render_public_tests,
)
from atlasos.verification.engine import VerificationEngine
from atlasos.verification.models import CheckKind, CheckSpec, VerificationSpec

GOOD_MONEY = '''"""Money."""


def format_cents(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    dollars, rest = divmod(abs(cents), 100)
    return f"{sign}${dollars:,}.{rest:02d}"


def parse_cents(text: str) -> int:
    raise ValueError("not implemented in this fixture")


def split_evenly(total: int, parts: int) -> list[int]:
    return [total // parts] * parts
'''


def _write(root: Path, relative: str, text: str) -> None:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _spec(artifact: str, *checks: CheckSpec) -> VerificationSpec:
    return VerificationSpec(
        spec_id="test/code",
        version="1.0.0",
        deliverable_type="python_modules",
        required_artifacts=(artifact,),
        checks=checks,
    ).with_declared_digest()


def test_missing_function_fails(tmp_path: Path) -> None:
    _write(tmp_path, "ledger/money.py", "def other() -> None:\n    return None\n")
    spec = _spec(
        "ledger/money.py",
        CheckSpec(
            "functions",
            "STRUCTURAL",
            CheckKind.FUNCTION_PRESENT,
            "ledger/money.py",
            {"functions": ["format_cents"]},
        ),
    )
    result = VerificationEngine().verify(spec, tmp_path)
    assert result.status_for("STRUCTURAL") == "FAIL"
    assert result.findings[0].reason_code == models.REASON_MISSING_FUNCTION


def test_syntax_error_fails(tmp_path: Path) -> None:
    _write(tmp_path, "module.py", "def broken(:\n")
    spec = _spec(
        "module.py",
        CheckSpec("parses", "STRUCTURAL", CheckKind.PYTHON_PARSES, "module.py"),
    )
    result = VerificationEngine().verify(spec, tmp_path)
    assert result.findings[0].reason_code == models.REASON_PARSE_ERROR


def test_signature_mismatch_fails(tmp_path: Path) -> None:
    _write(tmp_path, "module.py", "def f(a, b=2, *, c=3):\n    return a\n")
    spec = _spec(
        "module.py",
        CheckSpec(
            "sig",
            "STRUCTURAL",
            CheckKind.SIGNATURE_OK,
            "module.py",
            {"signatures": {"f": "f(a, b=1, *, c=3)"}},
        ),
    )
    result = VerificationEngine().verify(spec, tmp_path)
    assert result.status_for("STRUCTURAL") == "FAIL"
    assert result.findings[0].reason_code == models.REASON_SIGNATURE_MISMATCH


def test_signature_reader_ignores_annotations() -> None:
    import ast

    tree = ast.parse("def g(cents: int, *, flag: bool = False) -> str: ...")
    node = tree.body[0]
    assert isinstance(node, ast.FunctionDef)
    assert _signature(node) == "g(cents, *, flag=False)"


def test_not_implemented_stub_is_detected(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "module.py",
        "def f():\n    raise NotImplementedError('f')\n\ndef g():\n    ...\n",
    )
    spec = _spec(
        "module.py",
        CheckSpec(
            "stubs",
            "STRUCTURAL",
            CheckKind.NO_NOT_IMPLEMENTED,
            "module.py",
            {"functions": ["f", "g"]},
        ),
    )
    result = VerificationEngine().verify(spec, tmp_path)
    assert result.status_for("STRUCTURAL") == "FAIL"
    assert result.findings[0].reason_code == models.REASON_NOT_IMPLEMENTED


def test_forbidden_import_is_detected(tmp_path: Path) -> None:
    _write(tmp_path, "ledger/entries.py", "from ledger.money import format_cents\n")
    spec = _spec(
        "ledger/entries.py",
        CheckSpec(
            "independence",
            "STRUCTURAL",
            CheckKind.NO_FORBIDDEN_IMPORT,
            "ledger/entries.py",
            {"modules": ["ledger.money"]},
        ),
    )
    result = VerificationEngine().verify(spec, tmp_path)
    assert result.findings[0].reason_code == models.REASON_FORBIDDEN_IMPORT


def test_import_binding_reports_missing_names(tmp_path: Path) -> None:
    _write(tmp_path, "consumers/report.py", "from providers.clock import to_epoch\n")
    spec = _spec(
        "consumers/report.py",
        CheckSpec(
            "binding",
            "INTEGRATION",
            CheckKind.IMPORT_BINDING,
            "consumers/report.py",
            {"module": "providers.clock", "names": ["to_epoch", "from_epoch"]},
        ),
    )
    result = VerificationEngine().verify(spec, tmp_path)
    assert result.status_for("INTEGRATION") == "FAIL"
    assert result.findings[0].reason_code == models.REASON_BINDING_MISMATCH


def test_frozen_interface_change_is_detected(tmp_path: Path) -> None:
    _write(tmp_path, "docs/INTERFACE.md", "# changed\n")
    spec = _spec(
        "docs/INTERFACE.md",
        CheckSpec("frozen", "STRUCTURAL", CheckKind.FILE_UNCHANGED, "docs/INTERFACE.md"),
    )
    result = VerificationEngine().verify(
        spec, tmp_path, baseline_digests={"docs/INTERFACE.md": "0" * 64}
    )
    assert result.status_for("STRUCTURAL") == "FAIL"
    assert result.findings[0].reason_code == models.REASON_UNEXPECTED_CHANGE


def test_rendered_tests_name_each_case() -> None:
    module, names = render_public_tests(
        [
            {"case_id": "a", "module": "m", "imports": ["f"], "code": "f()", "expected_literal": 1},
            {
                "case_id": "b",
                "module": "m",
                "imports": ["g"],
                "code": "g()",
                "raises": "ValueError",
            },
        ]
    )
    assert "def test_case_00()" in module and "def test_case_01()" in module
    assert "with pytest.raises(ValueError)" in module
    assert names["test_case_00"] == "a"


def test_pytest_failure_names_are_mapped() -> None:
    names = {"test_case_00": "a", "test_case_01": "b"}
    stdout = "FAILED test_public_contract.py::test_case_01 - AssertionError\n"
    assert parse_pytest_failure(stdout, names) == "b"


def test_pytest_failure_markers_survive_a_single_line_transport() -> None:
    """The sandbox transport strips newlines, so markers must be found anywhere in the text."""

    names = {"test_case_02": "format_cents.zero", "test_case_07": "split_evenly.negative"}
    stdout = (
        "F..F.. [100%]=== short test summary info ==="
        "FAILED test_public_contract.py::test_case_02 - AssertionError"
        "FAILED test_public_contract.py::test_case_07 - AssertionError"
    )
    assert parse_pytest_failure(stdout, names) == "format_cents.zero, split_evenly.negative"


class _RecordingDynamic:
    def __init__(self, outcome: DynamicOutcome) -> None:
        self.outcome = outcome
        self.cases = None

    async def run(self, *, snapshot: Path, cases):  # noqa: ANN001, ANN201
        self.cases = list(cases)
        return self.outcome


def test_p2_contract_fails_a_stub_module(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "ledger/entries.py",
        'def parse_entry(line: str):\n    raise NotImplementedError("parse_entry")\n',
    )
    _write(tmp_path, "ledger/money.py", GOOD_MONEY)
    result = VerificationEngine(None).verify(p2_spec(), tmp_path)
    assert result.category_statuses()["structural_validity"] == "FAIL"
    assert result.overall == "FAIL"


def test_p2_contract_passes_structure_but_is_unknown_without_sandbox(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "ledger/entries.py",
        "def parse_entry(line):\n    return (line, 0, '')\n\n"
        "def total_for(entries, memo):\n    return 0\n\n"
        "def top_memos(entries, limit):\n    return []\n",
    )
    _write(tmp_path, "ledger/money.py", GOOD_MONEY)
    result = VerificationEngine(None).verify(p2_spec(), tmp_path)
    statuses = result.category_statuses()
    assert statuses["deliverable_presence"] == "PASS"
    assert statuses["structural_validity"] == "PASS"
    assert statuses["public_contract_status"] == "UNKNOWN"
    assert result.overall == "UNKNOWN"


def test_p2_dynamic_failure_fails_public_contract(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "ledger/entries.py",
        "def parse_entry(line):\n    return (line, 0, '')\n\n"
        "def total_for(entries, memo):\n    return 0\n\n"
        "def top_memos(entries, limit):\n    return []\n",
    )
    _write(tmp_path, "ledger/money.py", GOOD_MONEY)
    runner = _RecordingDynamic(
        DynamicOutcome("FAIL", models.REASON_SHAPE_MISMATCH, "format_cents.zero")
    )
    result = VerificationEngine(runner).verify(p2_spec(), tmp_path)
    assert result.category_statuses()["public_contract_status"] == "FAIL"
    assert runner.cases is not None and len(runner.cases) >= 10


def test_p3_contract_flags_a_changed_interface(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "providers/clock.py",
        "def to_epoch(text):\n    return 0\n\ndef from_epoch(value):\n    return '1970-01-01'\n",
    )
    _write(
        tmp_path,
        "consumers/report.py",
        "from providers.clock import to_epoch, from_epoch\n\ndef summarize(rows):\n    return ''\n",
    )
    _write(tmp_path, "docs/INTERFACE.md", "# edited\n")
    spec = p3_spec()
    result = VerificationEngine(None).verify(
        spec, tmp_path, baseline_digests={"docs/INTERFACE.md": "0" * 64}
    )
    assert result.category_statuses()["structural_validity"] == "FAIL"
    frozen = [item for item in result.findings if item.check_id == "p3.interface.frozen"]
    assert frozen and frozen[0].reason_code == models.REASON_UNEXPECTED_CHANGE


@pytest.mark.parametrize(
    "payload",
    ["def f(\n", "class X:\n", ""],
)
def test_unparseable_or_empty_modules_never_pass(tmp_path: Path, payload: str) -> None:
    _write(tmp_path, "module.py", payload)
    spec = _spec(
        "module.py",
        CheckSpec("parses", "STRUCTURAL", CheckKind.PYTHON_PARSES, "module.py"),
    )
    result = VerificationEngine().verify(spec, tmp_path)
    assert result.status_for("STRUCTURAL") == "FAIL"
