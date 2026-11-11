"""Stage R0.5: oracle self-test for the positive families.

A benchmark whose oracle can never pass proves nothing, and one that passes without work proves
even less. For every positive family this test materialises the *real* fixture, runs the *real*
hidden tests, and checks both directions:

* the shipped stub fails,
* a reference implementation passes.

The reference implementations live in this file, are never part of ``visible_files``, and are never
written into a worker sandbox. They exist so a reviewer can see that the oracle is satisfiable and
what the task actually asks for.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from experiments.evaluation.fixtures import _write_files
from experiments.evaluation.stage_r05_tasks import definition_by_id, variants

P1_PRICING_REPORT = """tier_rate: KeyError | example: tier_rate("pro") -> 0.8
final_price: KeyError | example: final_price(250, "pro") -> 200
bulk_discount: ValueError | example: bulk_discount(120) -> 0.15
cheapest: ValueError | example: cheapest([7, 3, 9]) -> 3

## Constants
DEFAULT_TIER = "basic"
EXPEDITE_SURCHARGE = 0.05
TIERS = {"basic": 1.0, "pro": 0.8, "enterprise": 0.6}
"""

P1_INVENTORY_REPORT = """available: none | example: available("nut") -> False
reserve: ValueError, LookupError, RuntimeError | example: reserve("bolt", 5) -> 7
low_stock: none | example: low_stock(4) -> ["gear"]
total_units: none | example: total_units() -> 22

## Constants
LOW_STOCK_THRESHOLD = 5
STOCK = {"bolt": 12, "nut": 0, "washer": 7, "gear": 3}
"""

P2_ENTRIES = """from __future__ import annotations


def parse_entry(line: str) -> tuple[str, int, str]:
    fields = line.split(",")
    if len(fields) != 3:
        raise ValueError("expected three comma separated fields")
    raw_date, raw_amount, raw_memo = fields
    date = raw_date.strip()
    if len(date) != 10 or date[4] != "-" or date[7] != "-":
        raise ValueError("date must be YYYY-MM-DD")
    if not (date[0:4] + date[5:7] + date[8:10]).isdigit():
        raise ValueError("date must be YYYY-MM-DD")
    try:
        amount = int(raw_amount.strip())
    except ValueError as error:
        raise ValueError("amount must be an integer") from error
    return (date, amount, raw_memo.strip())


def total_for(entries: list[tuple[str, int, str]], memo: str) -> int:
    return sum(amount for _date, amount, entry_memo in entries if entry_memo == memo)


def top_memos(entries: list[tuple[str, int, str]], limit: int) -> list[tuple[str, int]]:
    if limit <= 0:
        return []
    totals: dict[str, int] = {}
    for _date, amount, memo in entries:
        totals[memo] = totals.get(memo, 0) + amount
    ordered = sorted(totals.items(), key=lambda item: (-item[1], item[0]))
    return ordered[:limit]
"""

P2_MONEY = """from __future__ import annotations


def format_cents(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    dollars, remainder = divmod(abs(cents), 100)
    return f"{sign}${dollars:,}.{remainder:02d}"


def _ungroup(whole: str) -> str:
    if "," not in whole:
        return whole
    head, _, last = whole.rpartition(",")
    groups = head.split(",")
    if not all(group.isdigit() for group in groups):
        raise ValueError("group separators require digits")
    if any(len(group) != 3 for group in groups[1:]) or not 1 <= len(groups[0]) <= 3:
        raise ValueError("misplaced group separator")
    if len(last) != 3 or not last.isdigit():
        raise ValueError("misplaced group separator")
    return "".join(groups) + last


def parse_cents(text: str) -> int:
    raw = text.strip()
    sign = 1
    if raw.startswith("-"):
        sign = -1
        raw = raw[1:]
    if raw.startswith("$"):
        raw = raw[1:]
    if not raw:
        raise ValueError("empty amount")
    if "." in raw:
        whole, _, fraction = raw.partition(".")
        if len(fraction) != 2 or not fraction.isdigit():
            raise ValueError("two decimals are required")
    else:
        whole, fraction = raw, "00"
    digits = _ungroup(whole)
    if not digits.isdigit():
        raise ValueError("amount must be numeric")
    return sign * (int(digits) * 100 + int(fraction))


def split_evenly(total: int, parts: int) -> list[int]:
    if parts <= 0:
        raise ValueError("parts must be positive")
    sign = -1 if total < 0 else 1
    base, extra = divmod(abs(total), parts)
    return [sign * (base + (1 if index < extra else 0)) for index in range(parts)]
"""

P3_CLOCK = """from __future__ import annotations

import re
from datetime import date, timedelta

_EPOCH = date(1970, 1, 1)
_PATTERN = re.compile(r"^\\d{4}-\\d{2}-\\d{2}$")


def to_epoch(text: str) -> int:
    if not _PATTERN.match(text):
        raise ValueError("expected YYYY-MM-DD")
    try:
        parsed = date(int(text[0:4]), int(text[5:7]), int(text[8:10]))
    except ValueError as error:
        raise ValueError("not a calendar date") from error
    return (parsed - _EPOCH).days


def from_epoch(value: int) -> str:
    return (_EPOCH + timedelta(days=value)).isoformat()
"""

P3_REPORT = """from __future__ import annotations

from providers.clock import from_epoch, to_epoch


def summarize(rows: list[tuple[str, int]]) -> str:
    if not rows:
        raise ValueError("rows must not be empty")
    pairs = [(to_epoch(text), amount) for text, amount in rows]
    earliest = min(pair[0] for pair in pairs)
    latest = max(pair[0] for pair in pairs)
    total = sum(pair[1] for pair in pairs)
    return f"{from_epoch(earliest)}|{from_epoch(latest)}|{total}"
"""

REFERENCE = {
    "r05-p1-analysis": {
        "reports/pricing.md": P1_PRICING_REPORT,
        "reports/inventory.md": P1_INVENTORY_REPORT,
    },
    "r05-p2-modules": {
        "ledger/entries.py": P2_ENTRIES,
        "ledger/money.py": P2_MONEY,
    },
    "r05-p3-frozen-interface": {
        "providers/clock.py": P3_CLOCK,
        "consumers/report.py": P3_REPORT,
    },
}


def _run_hidden_tests(workspace: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
        cwd=workspace,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        check=False,
        env={
            **os.environ,
            "PYTHONPATH": str(workspace),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
    )


@pytest.mark.parametrize("sample_id", sorted(REFERENCE))
def test_reference_implementation_passes_the_oracle(sample_id: str, tmp_path: Path) -> None:
    task = variants(definition_by_id(sample_id))[1]
    workspace = tmp_path / "oracle"
    _write_files(workspace, task.visible_files)
    _write_files(workspace, REFERENCE[sample_id])
    _write_files(workspace, task.hidden_files)
    result = _run_hidden_tests(workspace)
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-2000:]


@pytest.mark.parametrize("sample_id", sorted(REFERENCE))
def test_shipped_stub_fails_the_oracle(sample_id: str, tmp_path: Path) -> None:
    task = variants(definition_by_id(sample_id))[1]
    workspace = tmp_path / "stub"
    _write_files(workspace, task.visible_files)
    _write_files(workspace, task.hidden_files)
    result = _run_hidden_tests(workspace)
    assert result.returncode != 0


def test_the_solution_is_never_shipped_in_a_fixture() -> None:
    """The reference implementation must not appear in visible or hidden files."""

    for sample_id, files in REFERENCE.items():
        task = variants(definition_by_id(sample_id))[1]
        for path, content in files.items():
            shipped = task.visible_files.get(path)
            if shipped is not None:
                assert shipped != content
                assert "NotImplementedError" in shipped
            assert content not in task.hidden_files.values()
