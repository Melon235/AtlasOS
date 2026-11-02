"""Independent acceptance checks for the UAT CSV fixture.

This file lives outside ``uat_repo`` and is not mounted into the sandbox. The UAT harness
imports the agent-modified ``csv_parser`` from the sandbox copy and checks behavior the agent
was not given as a visible test.
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AcceptanceResult:
    passed: bool
    failures: tuple[str, ...]


def _load_parser(repo: Path):
    module_path = repo / "csv_parser.py"
    spec = importlib.util.spec_from_file_location("uat_csv_parser_under_test", module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load parser from {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.parse_csv


def validate(repo: Path) -> AcceptanceResult:
    failures: list[str] = []
    try:
        parse_csv = _load_parser(repo)
    except Exception as error:  # noqa: BLE001 - acceptance reports the failure
        return AcceptanceResult(False, (f"import failed: {type(error).__name__}: {error}",))

    cases = (
        (
            "plain rows",
            "name,city\nAlice,Tokyo\n",
            [["name", "city"], ["Alice", "Tokyo"]],
        ),
        (
            "quoted comma",
            'name,city\n"Alice","Tokyo, Japan"\n',
            [["name", "city"], ["Alice", "Tokyo, Japan"]],
        ),
        (
            "escaped quote",
            'name,note\n"Alice","He said ""hi"""\n',
            [["name", "note"], ["Alice", 'He said "hi"']],
        ),
        (
            "unknown acceptance row",
            'x\n"a,b","c""d"\n',
            [["x"], ["a,b", 'c"d']],
        ),
    )
    for label, text, expected in cases:
        try:
            actual = parse_csv(text)
        except Exception as error:  # noqa: BLE001 - acceptance reports the failure
            failures.append(f"{label}: raised {type(error).__name__}: {error}")
            continue
        if actual != expected:
            failures.append(f"{label}: expected {expected!r}, got {actual!r}")
    return AcceptanceResult(not failures, tuple(failures))
