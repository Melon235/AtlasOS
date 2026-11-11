"""Stage R0.5: pre-registered positive and negative delegation benchmark families.

Stage R0 proved that the router refuses unsafe plans. It could not prove that the router
*admits* a plan that deserves delegation, because none of the Stage E1 plans passed the gates and
none of them was designed to. This module adds the missing control group:

* ``P1`` two independent read-only analyses that write disjoint reports,
* ``P2`` two independent module implementations with disjoint public contracts,
* ``P3`` two modules implemented against one frozen, public interface document,
* ``N1`` two writers of the same file (shared write),
* ``N2`` a consumer of an interface that was never frozen,
* ``N3`` a pure linear dependency chain.

Every family is a normal :class:`BenchmarkTask`, so A0 (forced single), A1 (forced candidate DAG)
and A2 (selective routing) run through the unchanged evaluation runner. The families differ only in
fixture, prompts and worker graph; the router sees the same evidence kinds it sees for Stage E1.

Oracle disclosure
-----------------
This repository is public, so these oracles are public too. ``hidden_files`` are never staged into
the worker sandbox (Stage G3-4a isolation additionally proves that no private artifact name is
reachable from inside it), but a worker with outbound network access could in principle read this
file. Runs are therefore checked for network-looking tool calls, and this family must not be
presented as a held-out benchmark after publication.

Nothing in this module inspects a sample id to decide a route; the id is only a label.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from experiments.evaluation.models import BenchmarkTask, WorkerStep

__all__ = [
    "BENCHMARK_VERSION",
    "DEFINITIONS",
    "ENV",
    "EXPERIMENT_MATRIX",
    "NEGATIVE_IDS",
    "POSITIVE_IDS",
    "R05Definition",
    "benchmark_digest",
    "definition_by_id",
    "definition_payload",
    "preregistration",
    "variants",
]

#: Benchmark version, with the two design defects found before any result was used.
#:
#: * ``r0.5.0`` — the P1 oracle demanded the literal ``none`` for functions that raise nothing in
#:   their contract (a correct analysis that also listed ``TypeError`` failed) and required an
#:   unquoted constant value even though the module quotes it. Fixed in ``r0.5.1``.
#: * ``r0.5.1`` — the P2 contract did not say that fields are stripped, while the P2 oracle expected
#:   ``parse_entry(" 2024-01-05 , -9 , rent ")`` to succeed: the oracle contradicted the published
#:   contract. Fixed in ``r0.5.2`` by stating the stripping rule in the contract.
#:
#: Both defects are recorded in ``docs/stage-r0-5-positive-delegation.md`` together with the
#: invalidated runs. P1/P3/N* definitions are byte-identical across ``r0.5.1`` and ``r0.5.2``.
BENCHMARK_VERSION = "r0.5.2"

#: Environment sentence. It contains no path, so it can never be read as a write claim.
ENV = "Work in /workspace/project. Only the files this task names may change; never modify tests/."


def _module(body: str) -> str:
    return "from __future__ import annotations\n\n" + body


@dataclass(frozen=True)
class R05Definition:
    """One benchmark family member with its expected, frozen routing outcome."""

    sample_id: str
    family: str
    expected_route: str
    expected_reason: str
    overall_prompt: str
    visible_files: dict[str, str]
    hidden_files: dict[str, str]
    allowed_paths: tuple[str, ...]
    required_artifacts: tuple[str, ...]
    workers: tuple[WorkerStep, ...]
    frozen_contracts: dict[str, tuple[str, ...]] | None = None
    max_turns: int = 14

    def __post_init__(self) -> None:
        if self.frozen_contracts is None:
            object.__setattr__(self, "frozen_contracts", {})


# --------------------------------------------------------------------------- P1 fixtures

_PRICING = _module(
    '''"""Pricing rules. Read-only reference module: analyse it, never modify it."""

TIERS: dict[str, float] = {"basic": 1.0, "pro": 0.8, "enterprise": 0.6}
DEFAULT_TIER = "basic"
EXPEDITE_SURCHARGE = 0.05


def tier_rate(tier: str) -> float:
    """Return the rate multiplier for ``tier``."""
    return TIERS[tier]


def final_price(amount_cents: int, tier: str = DEFAULT_TIER, *, expedited: bool = False) -> int:
    """Return the final price in integer cents."""
    price = amount_cents * tier_rate(tier)
    if expedited:
        price = price * (1 + EXPEDITE_SURCHARGE)
    return int(round(price))


def bulk_discount(quantity: int) -> float:
    """Return the discount for a line quantity."""
    if quantity < 0:
        raise ValueError("quantity must be non-negative")
    if quantity >= 100:
        return 0.15
    if quantity >= 50:
        return 0.10
    return 0.0


def cheapest(amounts: list[int]) -> int:
    """Return the smallest amount."""
    if not amounts:
        raise ValueError("amounts must not be empty")
    return min(amounts)
'''
)

_INVENTORY = _module(
    '''"""Stock rules. Read-only reference module: analyse it, never modify it."""

STOCK: dict[str, int] = {"bolt": 12, "nut": 0, "washer": 7, "gear": 3}
LOW_STOCK_THRESHOLD = 5


def available(item: str) -> bool:
    """Return True when the item has a positive quantity."""
    return STOCK.get(item, 0) > 0


def reserve(item: str, count: int) -> int:
    """Return the remaining quantity after reserving ``count`` units."""
    if count <= 0:
        raise ValueError("count must be positive")
    if item not in STOCK:
        raise LookupError(f"unknown item: {item}")
    if STOCK[item] < count:
        raise RuntimeError("insufficient stock")
    return STOCK[item] - count


def low_stock(threshold: int = LOW_STOCK_THRESHOLD) -> list[str]:
    """Return the sorted names whose quantity is positive but below ``threshold``."""
    return sorted(name for name, qty in STOCK.items() if 0 < qty < threshold)


def total_units() -> int:
    """Return the sum of every quantity."""
    return sum(STOCK.values())
'''
)

_REPORT_RULES = (
    "Write one line per public function in exactly this shape:\n"
    "`<function name>: <exception types it can raise, comma separated, or none> | "
    "example: <call> -> <exact returned value>`\n"
    "Use exactly the example calls listed here, list every exception type the function can raise "
    "for well-typed inputs (including exceptions it propagates), and write `none` when it raises "
    "nothing. Exceptions that only appear when a caller passes the wrong type do not count.\n"
    "After the function lines add a section `## Constants` with one line per module-level "
    "constant in the shape `<CONSTANT_NAME> = <value>`; quote a string value exactly as the module "
    "quotes it."
)

_P1_PRICING_CALLS = (
    'Example calls for this module: tier_rate("pro"), final_price(250, "pro"), '
    "bulk_discount(120), cheapest([7, 3, 9])."
)

_P1_INVENTORY_CALLS = (
    'Example calls for this module: available("nut"), reserve("bolt", 5), '
    "low_stock(4), total_units()."
)

_P1_OVERALL = (
    "Analyse two independent reference modules of this repository, one report for each.\n\n"
    "Module 1: /workspace/project/catalog/pricing.py\n"
    'Calls: tier_rate("pro"), final_price(250, "pro"), bulk_discount(120), cheapest([7, 3, 9]).\n\n'
    "Module 2: /workspace/project/catalog/inventory.py\n"
    'Calls: available("nut"), reserve("bolt", 5), low_stock(4), total_units().\n\n'
    f"{_REPORT_RULES}\n\n"
    "Write the Module 1 report to /workspace/project/reports/pricing.md.\n"
    "Write the Module 2 report to /workspace/project/reports/inventory.md.\n"
    f"{ENV}"
)

# --------------------------------------------------------------------------- P2 fixtures

_ENTRIES_STUB = _module(
    '''"""Ledger entry parsing. Public contract below; implement it exactly."""


def parse_entry(line: str) -> tuple[str, int, str]:
    """Parse ``"<date>,<amount>,<memo>"`` into ``(date, amount, memo)``.

    The line is split on commas and must yield exactly three fields, otherwise ValueError is
    raised. Each field is stripped of surrounding whitespace before it is validated. The date must
    then be exactly 10 characters in YYYY-MM-DD shape; calendar validity is not checked. The amount
    must be a base-10 integer and may carry a leading '-'; anything else raises ValueError. The
    memo may be empty.
    """
    raise NotImplementedError("parse_entry")


def total_for(entries: list[tuple[str, int, str]], memo: str) -> int:
    """Return the sum of the amounts of the entries whose memo matches exactly."""
    raise NotImplementedError("total_for")


def top_memos(entries: list[tuple[str, int, str]], limit: int) -> list[tuple[str, int]]:
    """Return at most ``limit`` (memo, total) pairs, highest total first, ties by memo A-Z.

    An empty list of entries or ``limit <= 0`` returns an empty list.
    """
    raise NotImplementedError("top_memos")
'''
)

_MONEY_STUB = _module(
    '''"""Money formatting helpers. Public contract below; implement it exactly."""


def format_cents(cents: int) -> str:
    """Format integer cents as dollars with two decimals and comma group separators.

    123450 -> "$1,234.50"; -5 -> "-$0.05"; 0 -> "$0.00".
    """
    raise NotImplementedError("format_cents")


def parse_cents(text: str) -> int:
    """Return integer cents parsed from a formatted amount.

    "$1,234.50" -> 123450; "-$0.05" -> -5; "7" -> 700. A leading '$' is optional, comma group
    separators are only allowed in the integer part, and more than two decimals raises ValueError.
    """
    raise NotImplementedError("parse_cents")


def split_evenly(total: int, parts: int) -> list[int]:
    """Split ``total`` into ``parts`` integers without losing a cent.

    The first ``total % parts`` shares receive one extra cent. ``parts <= 0`` raises ValueError.
    split_evenly(10, 3) -> [4, 3, 3]; split_evenly(-10, 3) -> [-4, -3, -3].
    """
    raise NotImplementedError("split_evenly")
'''
)

_ENTRIES_HIDDEN = _module(
    """import pytest

from ledger.entries import parse_entry, top_memos, total_for


def test_parse_entry() -> None:
    assert parse_entry("2024-01-05,1250,coffee") == ("2024-01-05", 1250, "coffee")
    assert parse_entry("2024-13-99,7,") == ("2024-13-99", 7, "")
    assert parse_entry(" 2024-01-05 , -9 , rent ") == ("2024-01-05", -9, "rent")
    with pytest.raises(ValueError):
        parse_entry("2024-01-05,12")
    with pytest.raises(ValueError):
        parse_entry("2024-01-05,12,x,extra")
    with pytest.raises(ValueError):
        parse_entry("2024-1-05,12,x")
    with pytest.raises(ValueError):
        parse_entry("2024-01-05,1.5,x")


def test_totals() -> None:
    rows = [
        ("2024-01-05", 1250, "coffee"),
        ("2024-01-06", 100, "coffee"),
        ("2024-01-07", 900, "rent"),
        ("2024-01-08", 50, "tea"),
    ]
    assert total_for(rows, "coffee") == 1350
    assert total_for(rows, "nothing") == 0
    assert top_memos(rows, 2) == [("coffee", 1350), ("rent", 900)]
    assert top_memos(rows, 0) == []
    assert top_memos([], 3) == []
    assert top_memos(rows, 10) == [("coffee", 1350), ("rent", 900), ("tea", 50)]
"""
)

_MONEY_HIDDEN = _module(
    """import pytest

from ledger.money import format_cents, parse_cents, split_evenly


def test_format_and_parse() -> None:
    assert format_cents(123450) == "$1,234.50"
    assert format_cents(-5) == "-$0.05"
    assert format_cents(0) == "$0.00"
    assert format_cents(999) == "$9.99"
    assert format_cents(100000000) == "$1,000,000.00"
    assert parse_cents("$1,234.50") == 123450
    assert parse_cents("-$0.05") == -5
    assert parse_cents("7") == 700
    assert parse_cents("0.00") == 0
    for bad in ("1,23.4", "$", "1.234", "abc", "-"):
        with pytest.raises(ValueError):
            parse_cents(bad)
    for cents in (0, 5, 999, 123450, -5, 100000000):
        assert parse_cents(format_cents(cents)) == cents


def test_split_evenly() -> None:
    assert split_evenly(10, 3) == [4, 3, 3]
    assert split_evenly(-10, 3) == [-4, -3, -3]
    assert split_evenly(0, 4) == [0, 0, 0, 0]
    assert sum(split_evenly(1234, 7)) == 1234
    with pytest.raises(ValueError):
        split_evenly(10, 0)
"""
)

_P2_OVERALL = (
    "Implement two independent modules of the ledger package. Their contracts are frozen and "
    "independent: neither module imports the other.\n\n"
    "Module A: /workspace/project/ledger/entries.py — parse_entry, total_for, top_memos.\n"
    "Module B: /workspace/project/ledger/money.py — format_cents, parse_cents, split_evenly.\n\n"
    "Each file already contains the public signatures and their docstrings; keep those signatures "
    "and implement the documented behaviour exactly, including every error case. Do not change the "
    "docstrings' promises and do not add imports between the two modules.\n\n"
    f"{ENV}"
)

_ENTRIES_PROMPT = (
    "Implement the frozen public contract of /workspace/project/ledger/entries.py: parse_entry, "
    "total_for and top_memos, exactly as the docstrings in that file describe. Keep the existing "
    "signatures. Every documented error case must raise the documented exception. Do not import or "
    "modify the other module of this package, and do not add any other module.\n\n"
    f"{ENV}"
)

_MONEY_PROMPT = (
    "Implement the frozen public contract of /workspace/project/ledger/money.py: format_cents, "
    "parse_cents and split_evenly, exactly as the docstrings in that file describe. Keep the "
    "existing signatures, handle negative values and every rejected input, and make "
    "parse_cents(format_cents(x)) == x for every integer x. Do not import or modify the other "
    "module of this package, and do not add any other module.\n\n"
    f"{ENV}"
)

# --------------------------------------------------------------------------- P3 fixtures

_CLOCK_STUB = _module(
    '''"""Frozen provider module. Implement it against docs/INTERFACE.md."""


def to_epoch(text: str) -> int:
    """Return the number of days between 1970-01-01 and the given 'YYYY-MM-DD' date."""
    raise NotImplementedError("to_epoch")


def from_epoch(value: int) -> str:
    """Return the canonical 'YYYY-MM-DD' string for a day offset from 1970-01-01."""
    raise NotImplementedError("from_epoch")
'''
)

_REPORT_STUB = _module(
    '''"""Frozen consumer module. Implement it against docs/INTERFACE.md."""


def summarize(rows: list[tuple[str, int]]) -> str:
    """Return '<earliest>|<latest>|<total>' for the given (date, amount) rows."""
    raise NotImplementedError("summarize")
'''
)

_INTERFACE_DOC = """# Frozen interface

This contract is fixed before either module is implemented. Neither implementation may change a
signature or a data shape below.

## providers.clock

`to_epoch(text: str) -> int`

* `text` is a `YYYY-MM-DD` date.
* Returns the number of days since `1970-01-01`, i.e. `to_epoch("1970-01-01") == 0`.
* Raises `ValueError` when `text` is not a real calendar date in that exact shape:
  `"2024-1-5"`, `"2024-02-30"` and `"05/01/2024"` are all invalid.

`from_epoch(value: int) -> str`

* Inverse of `to_epoch`; returns the canonical `YYYY-MM-DD` string, so
  `from_epoch(0) == "1970-01-01"`.

## consumers.report

`summarize(rows: list[tuple[str, int]]) -> str`

* `rows` are `(date_text, amount)` pairs.
* Returns `"<earliest>|<latest>|<total>"`: `earliest` and `latest` are the canonical dates of the
  smallest and largest `to_epoch` value, `total` is the sum of every amount.
* Raises `ValueError` for an empty list.
* `summarize([("2024-01-02", 5), ("2023-12-31", 7)])` returns `"2023-12-31|2024-01-02|12"`.
"""

_P3_OVERALL = (
    "Implement two modules that were specified by a frozen interface document. The contract in "
    "/workspace/project/docs/INTERFACE.md is authoritative and was written before this task "
    "started.\n\n"
    "Module A: /workspace/project/providers/clock.py — to_epoch, from_epoch.\n"
    "Module B: /workspace/project/consumers/report.py — summarize.\n\n"
    "Both modules already contain the public signatures. Implement the documented behaviour "
    "exactly, including rejected inputs. Do not change the interface document and do not add "
    "modules.\n\n"
    f"{ENV}"
)

_P3_CLOCK_PROMPT = (
    "Implement /workspace/project/providers/clock.py: to_epoch and from_epoch. Read "
    "/workspace/project/docs/INTERFACE.md first: it is the frozen contract and it defines the "
    "accepted inputs, the rejected inputs and the return values. Handle leap years and reject "
    "malformed or impossible dates with ValueError. Do not modify the consumer module and do not "
    "change the interface document.\n\n"
    f"{ENV}"
)

_P3_SUMMARY_PROMPT = (
    "Implement /workspace/project/consumers/report.py: summarize. Read "
    "/workspace/project/docs/INTERFACE.md first: it is the frozen contract and it defines the "
    "output shape and the empty-input error. Import to_epoch and from_epoch from providers.clock, "
    "which is being written in parallel against the same frozen contract, so code against the "
    "documented signature only. Do not create the provider module and do not change the interface "
    "document.\n\n"
    f"{ENV}"
)

_CLOCK_HIDDEN = _module(
    """import pytest

from providers.clock import from_epoch, to_epoch


def test_to_epoch() -> None:
    assert to_epoch("1970-01-01") == 0
    assert to_epoch("1970-01-02") == 1
    assert to_epoch("2000-03-01") == 11017
    assert to_epoch("2024-02-29") == 19782
    for bad in ("2024-02-30", "2024-1-5", "05/01/2024", "2023-02-29", ""):
        with pytest.raises(ValueError):
            to_epoch(bad)


def test_from_epoch() -> None:
    assert from_epoch(0) == "1970-01-01"
    assert from_epoch(19782) == "2024-02-29"
    assert from_epoch(to_epoch("2031-07-04")) == "2031-07-04"
"""
)

_SUMMARY_HIDDEN = _module(
    """import pytest

from consumers.report import summarize


def test_summarize() -> None:
    assert summarize([("2024-01-02", 5), ("2023-12-31", 7)]) == "2023-12-31|2024-01-02|12"
    assert summarize([("2024-02-29", 1)]) == "2024-02-29|2024-02-29|1"
    assert summarize([("2024-01-01", -5), ("2024-01-03", 5)]) == "2024-01-01|2024-01-03|0"
    with pytest.raises(ValueError):
        summarize([])
"""
)

# --------------------------------------------------------------------------- N1 / N2 / N3

_STATE_STUB = _module(
    '''"""Owner module for the shared-state fixture. Nothing to implement here."""
'''
)

_N1_OVERALL = (
    "Record two independent measurements in /workspace/project/state.json. Each measurement must "
    "appear as its own key.\n\n"
    f"{ENV}"
)

# --------------------------------------------------------------------------- definitions


def _definitions() -> tuple[R05Definition, ...]:
    definitions: list[R05Definition] = []

    # P1: independent read-only analysis, two disjoint reports.
    definitions.append(
        R05Definition(
            sample_id="r05-p1-analysis",
            family="P1",
            expected_route="delegate_parallel",
            expected_reason="DELEGATION_ADMITTED",
            overall_prompt=_P1_OVERALL,
            visible_files={
                "README.md": "# Catalog service\n\nRead-only analysis target.\n",
                "catalog/__init__.py": "",
                "catalog/pricing.py": _PRICING,
                "catalog/inventory.py": _INVENTORY,
            },
            hidden_files={
                "tests/test_hidden_p1.py": _module(
                    """from pathlib import Path


def _lines(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8").lower()
    return [" ".join(line.split()) for line in text.splitlines() if line.strip()]


def _head(line: str) -> str:
    head = line.split(":", 1)[0]
    return head.strip().lstrip("-*+ ").strip("`*_ ")


def _line_for(lines: list[str], name: str) -> str:
    for line in lines:
        if _head(line) == name:
            return line
    raise AssertionError(f"no report line for {name!r}")


def _constant(lines: list[str], name: str) -> str:
    for line in lines:
        if line.startswith(name) or line.startswith(f"- {name}") or line.startswith(f"* {name}"):
            return line
    raise AssertionError(f"no constant line for {name!r}")


# Domain exceptions a function raises itself; wrong-argument-type errors do not count.
DOMAIN = ("keyerror", "valueerror", "lookuperror", "runtimeerror")


def test_pricing_report() -> None:
    lines = _lines(Path("reports/pricing.md"))
    assert "keyerror" in _line_for(lines, "tier_rate")
    assert "0.8" in _line_for(lines, "tier_rate")
    assert "keyerror" in _line_for(lines, "final_price")
    assert "200" in _line_for(lines, "final_price")
    assert "valueerror" in _line_for(lines, "bulk_discount")
    assert "0.15" in _line_for(lines, "bulk_discount")
    assert "valueerror" in _line_for(lines, "cheapest")
    assert "3" in _line_for(lines, "cheapest")
    default = _constant(lines, "default_tier")
    assert "basic" in default
    assert "0.05" in _constant(lines, "expedite_surcharge")


def test_inventory_report() -> None:
    lines = _lines(Path("reports/inventory.md"))
    available = _line_for(lines, "available")
    assert not [name for name in DOMAIN if name in available]
    assert "false" in available
    reserve = _line_for(lines, "reserve")
    assert "valueerror" in reserve
    assert "lookuperror" in reserve
    assert "runtimeerror" in reserve
    assert "7" in reserve
    low_stock = _line_for(lines, "low_stock")
    assert not [name for name in DOMAIN if name in low_stock]
    assert "gear" in low_stock
    total_units = _line_for(lines, "total_units")
    assert not [name for name in DOMAIN if name in total_units]
    assert "22" in total_units
    assert "5" in _constant(lines, "low_stock_threshold")
"""
                )
            },
            allowed_paths=("reports/pricing.md", "reports/inventory.md"),
            required_artifacts=("reports/pricing.md", "reports/inventory.md"),
            workers=(
                WorkerStep(
                    "pricing",
                    "r05-p1-pricing",
                    "Read /workspace/project/catalog/pricing.py and analyse it without changing "
                    f"it. {_P1_PRICING_CALLS}\n\n{_REPORT_RULES}\n\n"
                    "Write the report to /workspace/project/reports/pricing.md.\n"
                    f"{ENV}",
                ),
                WorkerStep(
                    "inventory",
                    "r05-p1-inventory",
                    "Read /workspace/project/catalog/inventory.py and analyse it without changing "
                    f"it. {_P1_INVENTORY_CALLS}\n\n{_REPORT_RULES}\n\n"
                    "Write the report to /workspace/project/reports/inventory.md.\n"
                    f"{ENV}",
                ),
            ),
        )
    )

    # P2: independent module implementation with disjoint public contracts.
    definitions.append(
        R05Definition(
            sample_id="r05-p2-modules",
            family="P2",
            expected_route="delegate_parallel",
            expected_reason="DELEGATION_ADMITTED",
            overall_prompt=_P2_OVERALL,
            visible_files={
                "README.md": "# Ledger package\n\nTwo independent modules with frozen contracts.\n",
                "ledger/__init__.py": "",
                "ledger/entries.py": _ENTRIES_STUB,
                "ledger/money.py": _MONEY_STUB,
            },
            hidden_files={
                "tests/test_hidden_p2_entries.py": _ENTRIES_HIDDEN,
                "tests/test_hidden_p2_money.py": _MONEY_HIDDEN,
            },
            allowed_paths=("ledger/entries.py", "ledger/money.py"),
            required_artifacts=("ledger/entries.py", "ledger/money.py"),
            workers=(
                WorkerStep("entries", "r05-p2-entries", _ENTRIES_PROMPT),
                WorkerStep("money", "r05-p2-money", _MONEY_PROMPT),
            ),
        )
    )

    # P3: two modules implemented against one frozen public interface.
    definitions.append(
        R05Definition(
            sample_id="r05-p3-frozen-interface",
            family="P3",
            expected_route="delegate_parallel",
            expected_reason="DELEGATION_ADMITTED",
            overall_prompt=_P3_OVERALL,
            visible_files={
                "README.md": "# Date reporting\n\nTwo modules, one frozen interface.\n",
                "docs/INTERFACE.md": _INTERFACE_DOC,
                "providers/__init__.py": "",
                "providers/clock.py": _CLOCK_STUB,
                "consumers/__init__.py": "",
                "consumers/report.py": _REPORT_STUB,
            },
            hidden_files={
                "tests/test_hidden_p3_clock.py": _CLOCK_HIDDEN,
                "tests/test_hidden_p3_summary.py": _SUMMARY_HIDDEN,
            },
            allowed_paths=("providers/clock.py", "consumers/report.py"),
            required_artifacts=("providers/clock.py", "consumers/report.py"),
            workers=(
                WorkerStep("clock", "r05-p3-clock", _P3_CLOCK_PROMPT),
                WorkerStep("summary", "r05-p3-summary", _P3_SUMMARY_PROMPT),
            ),
            frozen_contracts={
                "clock": ("docs/INTERFACE.md",),
                "summary": ("docs/INTERFACE.md",),
            },
        )
    )

    # N1: two concurrent writers of the same file.
    definitions.append(
        R05Definition(
            sample_id="r05-n1-shared-write",
            family="N1",
            expected_route="direct",
            expected_reason="SHARED_WRITE_RISK",
            overall_prompt=_N1_OVERALL,
            visible_files={
                "README.md": "# Measurements\n",
                "state.json": "{}\n",
                "writer_a.py": _STATE_STUB,
                "writer_b.py": _STATE_STUB,
            },
            hidden_files={
                "tests/test_hidden_n1.py": _module(
                    """import json
from pathlib import Path


def test_state_has_both_keys() -> None:
    state = json.loads(Path("state.json").read_text(encoding="utf-8"))
    assert set(state) == {"a", "b"}
"""
                )
            },
            allowed_paths=("state.json", "writer_a.py", "writer_b.py"),
            required_artifacts=("state.json",),
            workers=(
                WorkerStep(
                    "a",
                    "r05-n1-a",
                    'Write /workspace/project/state.json so that it contains {"a": 1, "b": 2}. '
                    f"{ENV}",
                ),
                WorkerStep(
                    "b",
                    "r05-n1-b",
                    'Write /workspace/project/state.json so that it contains {"a": 1, "b": 2}. '
                    f"{ENV}",
                ),
            ),
        )
    )

    # N2: consumer of an interface that was never frozen (the contract file is absent).
    definitions.append(
        R05Definition(
            sample_id="r05-n2-unfrozen-interface",
            family="N2",
            expected_route="direct",
            expected_reason="UNSTABLE_INTERFACE",
            overall_prompt=_P3_OVERALL,
            visible_files={
                "README.md": "# Date reporting\n\nTwo modules, no frozen interface yet.\n",
                "providers/__init__.py": "",
                "providers/clock.py": _CLOCK_STUB,
                "consumers/__init__.py": "",
                "consumers/report.py": _REPORT_STUB,
            },
            hidden_files={
                "tests/test_hidden_p3_clock.py": _CLOCK_HIDDEN,
                "tests/test_hidden_p3_summary.py": _SUMMARY_HIDDEN,
            },
            allowed_paths=("providers/clock.py", "consumers/report.py"),
            required_artifacts=("providers/clock.py", "consumers/report.py"),
            workers=(
                WorkerStep("clock", "r05-n2-clock", _P3_CLOCK_PROMPT),
                WorkerStep("summary", "r05-n2-summary", _P3_SUMMARY_PROMPT),
            ),
            # The declaration is deliberately kept: the adapter must verify that the file exists.
            frozen_contracts={
                "clock": ("docs/INTERFACE.md",),
                "summary": ("docs/INTERFACE.md",),
            },
        )
    )

    # N3: pure linear chain.
    definitions.append(
        R05Definition(
            sample_id="r05-n3-linear-chain",
            family="N3",
            expected_route="direct",
            expected_reason="NO_PARALLEL_WAVE",
            overall_prompt=(
                "Implement a two-step calculation: compute the total in calc.py, then use it in "
                "report.py.\n\n" + ENV
            ),
            visible_files={
                "README.md": "# Two step calculation\n",
                "calc.py": _module(
                    'def total(values: list[int]) -> int:\n    raise NotImplementedError("total")\n'
                ),
                "report.py": _module(
                    "from calc import total\n\n\n"
                    "def describe(values: list[int]) -> str:\n"
                    '    raise NotImplementedError("describe")\n'
                ),
            },
            hidden_files={
                "tests/test_hidden_n3.py": _module(
                    """from calc import total
from report import describe


def test_chain() -> None:
    assert total([1, 2, 3]) == 6
    assert describe([1, 2, 3]) == "sum=6"
"""
                )
            },
            allowed_paths=("calc.py", "report.py"),
            required_artifacts=("calc.py", "report.py"),
            workers=(
                WorkerStep(
                    "first",
                    "r05-n3-first",
                    "Implement total(values) in /workspace/project/calc.py so that it returns the "
                    f"sum of the values.\n{ENV}",
                ),
                WorkerStep(
                    "second",
                    "r05-n3-second",
                    "Read /workspace/project/calc.py, then implement describe(values) in "
                    '/workspace/project/report.py so that it returns "sum=<total>".\n' + ENV,
                    depends_on=("first",),
                ),
            ),
        )
    )

    return tuple(definitions)


DEFINITIONS: tuple[R05Definition, ...] = _definitions()
POSITIVE_IDS: tuple[str, ...] = tuple(
    item.sample_id for item in DEFINITIONS if item.family.startswith("P")
)
NEGATIVE_IDS: tuple[str, ...] = tuple(
    item.sample_id for item in DEFINITIONS if item.family.startswith("N")
)

#: Frozen experiment matrix. Arms are A0 (forced single), A1 (forced candidate DAG) and A2
#: (selective routing). Track B reuses the Stage E1 tasks that only fail on the width limit.
EXPERIMENT_MATRIX: dict[str, Any] = {
    "track_a": {
        "families": ["P1", "P2", "P3"],
        "tasks": list(POSITIVE_IDS),
        "arms": ["a0", "a1", "a2"],
        "repetitions": 2,
        "routing_policy_version": "r0.1.0",
        "runs": 18,
    },
    "track_a_negative_audit": {
        "families": ["N1", "N2", "N3"],
        "tasks": list(NEGATIVE_IDS),
        "arms": [],
        "repetitions": 0,
        "routing_policy_version": "r0.1.0",
        "runs": 0,
    },
    "track_b": {
        "tasks": ["e1-01-config", "e1-03-rbac"],
        "arms": ["a0", "a1", "a2"],
        "repetitions": 2,
        "routing_policy_version": "r0.2.0",
        "runs": 12,
    },
    "fixed_conditions": {
        "provider": "deepseek",
        "model": "deepseek-chat",
        "temperature": 0,
        "sandbox": "OpenSandbox",
        "budget_mode": "guarded",
        "tool_error_mode": "corrected",
        "handoff_mode": "dual",
        "compaction_mode": "off",
        "tool_result_mode": "off",
        "worker_contract_mode": "legacy",
        "token_budget": 120_000,
        "max_turns": 14,
        "max_tool_calls": 60,
    },
}


def variants(definition: R05Definition) -> tuple[BenchmarkTask, BenchmarkTask]:
    """Return (A0 forced single, A1 frozen candidate DAG) with identical fixtures and oracle."""

    a0 = BenchmarkTask(
        sample_id=f"{definition.sample_id}-a0",
        category="multi_worker",
        mode="coding",
        prompt=definition.overall_prompt,
        target="hidden oracle passes",
        visible_files=definition.visible_files,
        hidden_files=definition.hidden_files,
        workers=(
            WorkerStep(
                task_id="solo",
                title=f"{definition.sample_id}-solo",
                prompt=definition.overall_prompt,
            ),
        ),
        allowed_paths=definition.allowed_paths,
        forbidden_paths=("tests/**",),
        required_artifacts=definition.required_artifacts,
        max_turns=definition.max_turns,
        tags=("stage_r05", definition.family, "A0"),
        frozen_contracts=dict(definition.frozen_contracts or {}),
    )
    a1 = BenchmarkTask(
        sample_id=f"{definition.sample_id}-a1",
        category="multi_worker",
        mode="multi_worker",
        prompt=definition.overall_prompt,
        target="hidden oracle passes",
        visible_files=definition.visible_files,
        hidden_files=definition.hidden_files,
        workers=definition.workers,
        allowed_paths=definition.allowed_paths,
        forbidden_paths=("tests/**",),
        required_artifacts=definition.required_artifacts,
        tool_expectations={"distinct_workers": len(definition.workers)},
        max_turns=definition.max_turns,
        tags=("stage_r05", definition.family, "A1"),
        frozen_contracts=dict(definition.frozen_contracts or {}),
    )
    return a0, a1


def definition_by_id(sample_id: str) -> R05Definition:
    """Accept a base id or any arm id (``-a0``/``-a1``/``-a2``)."""

    base = sample_id
    for suffix in ("-a0", "-a1", "-a2"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    for item in DEFINITIONS:
        if item.sample_id == base:
            return item
    raise KeyError(sample_id)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def definition_payload(definition: R05Definition) -> dict[str, Any]:
    """Everything that can change a run's task semantics or its oracle."""

    return {
        "sample_id": definition.sample_id,
        "family": definition.family,
        "expected_route": definition.expected_route,
        "expected_reason": definition.expected_reason,
        "overall_prompt": definition.overall_prompt,
        "visible_files": definition.visible_files,
        "hidden_files": definition.hidden_files,
        "allowed_paths": list(definition.allowed_paths),
        "required_artifacts": list(definition.required_artifacts),
        "frozen_contracts": {
            key: list(value) for key, value in sorted((definition.frozen_contracts or {}).items())
        },
        "workers": [
            {
                "task_id": worker.task_id,
                "title": worker.title,
                "prompt": worker.prompt,
                "depends_on": list(worker.depends_on),
                "max_turns": worker.max_turns,
            }
            for worker in definition.workers
        ],
        "max_turns": definition.max_turns,
    }


def benchmark_digest(definition: R05Definition) -> str:
    return hashlib.sha256(_canonical(definition_payload(definition)).encode("utf-8")).hexdigest()


def preregistration() -> dict[str, Any]:
    """Frozen, hash-addressed description of the R0.5 experiment.

    Written before the first live model call and asserted again by every round, so a changed
    fixture, prompt, contract or threshold cannot silently reuse the original round name.
    """

    from experiments.evaluation.stage_r0_routing import routing_policy

    positives = [item for item in DEFINITIONS if item.family.startswith("P")]
    negatives = [item for item in DEFINITIONS if item.family.startswith("N")]
    contracts: dict[str, str] = {}
    for item in DEFINITIONS:
        for task_id, paths in sorted((item.frozen_contracts or {}).items()):
            for path in paths:
                content = item.visible_files.get(path)
                contracts[f"{item.sample_id}:{task_id}:{path}"] = (
                    hashlib.sha256(content.encode("utf-8")).hexdigest()
                    if content is not None
                    else "MISSING"
                )
    oracle = {
        item.sample_id: {
            path: hashlib.sha256(content.encode("utf-8")).hexdigest()
            for path, content in sorted(item.hidden_files.items())
        }
        for item in DEFINITIONS
    }
    narrow = routing_policy("r0.1.0")
    wide = routing_policy("r0.2.0")
    payload: dict[str, Any] = {
        "benchmark_version": BENCHMARK_VERSION,
        "positive_task_definitions": {
            "ids": list(POSITIVE_IDS),
            "digests": {item.sample_id: benchmark_digest(item) for item in positives},
        },
        "negative_task_definitions": {
            "ids": list(NEGATIVE_IDS),
            "digests": {item.sample_id: benchmark_digest(item) for item in negatives},
        },
        "public_contracts": contracts,
        "oracle_definition_digest": hashlib.sha256(_canonical(oracle).encode("utf-8")).hexdigest(),
        "routing_policy_version": narrow.version,
        "routing_policy_digest": narrow.digest(),
        "width_policy_version": wide.version,
        "width_policy_digest": wide.digest(),
        "experiment_matrix": EXPERIMENT_MATRIX,
    }
    payload["preregistration_digest"] = hashlib.sha256(
        _canonical(payload).encode("utf-8")
    ).hexdigest()
    return payload
