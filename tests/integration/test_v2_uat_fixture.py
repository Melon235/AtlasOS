"""The UAT fixture must fail its external acceptance as shipped and pass when fixed."""

from __future__ import annotations

import hashlib
import importlib.util
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE = REPO_ROOT / "experiments" / "fixtures" / "uat_repo"
ACCEPTANCE = REPO_ROOT / "experiments" / "fixtures" / "uat_acceptance.py"
CORRECT_PARSER = '''"""CSV parser."""

from __future__ import annotations

import csv
from io import StringIO


def parse_csv(text: str) -> list[list[str]]:
    return [row for row in csv.reader(StringIO(text)) if row]
'''


def _acceptance_module():
    spec = importlib.util.spec_from_file_location("uat_acceptance_test", ACCEPTANCE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    for item in sorted(path.rglob("*")):
        if item.is_file() and "__pycache__" not in item.parts:
            digest.update(item.relative_to(path).as_posix().encode())
            digest.update(item.read_bytes())
    return digest.hexdigest()


def test_uat_fixture_fails_before_fix_and_passes_after(tmp_path: Path) -> None:
    module = _acceptance_module()
    before = _digest(FIXTURE)
    assert module.validate(FIXTURE).passed is False
    copy = tmp_path / "uat_repo"
    shutil.copytree(FIXTURE, copy, ignore=shutil.ignore_patterns("__pycache__"))
    (copy / "csv_parser.py").write_text(CORRECT_PARSER, encoding="utf-8")
    result = module.validate(copy)
    assert result.passed is True, result.failures
    assert _digest(FIXTURE) == before
