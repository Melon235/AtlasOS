"""Trusted verification contracts for the R1-2A deliverables.

Every rule below is traceable to something the worker could read:

* the visible fixture stub and its docstrings (P2),
* the frozen interface document staged in the workspace (P3),
* the published report line format in the worker prompt (P1).

Nothing here is derived from ``hidden_files``. The hidden oracle stays in
``experiments/evaluation/oracle.py`` and is never imported by this module.
"""

from __future__ import annotations

from atlasos.verification.models import CATEGORIES, CheckKind, CheckSpec, VerificationSpec

_ENTRY_FUNCTIONS = ("parse_entry", "total_for", "top_memos")
_MONEY_FUNCTIONS = ("format_cents", "parse_cents", "split_evenly")

ENTRIES_SIGNATURES = {
    "parse_entry": "parse_entry(line)",
    "total_for": "total_for(entries, memo)",
    "top_memos": "top_memos(entries, limit)",
}
MONEY_SIGNATURES = {
    "format_cents": "format_cents(cents)",
    "parse_cents": "parse_cents(text)",
    "split_evenly": "split_evenly(total, parts)",
}

#: Published examples. ``format_cents``/``parse_cents``/``split_evenly`` values are quoted in the
#: stub docstring; the remaining ones follow the documented rule (two decimals, strip, ties A-Z).
_ENTRIES_CASES = (
    {
        "case_id": "parse_entry.shape",
        "module": "ledger.entries",
        "imports": ["parse_entry"],
        "code": 'parse_entry("2021-07-04,42,rent")',
        "expected_literal": ("2021-07-04", 42, "rent"),
    },
    {
        "case_id": "parse_entry.strips_fields",
        "module": "ledger.entries",
        "imports": ["parse_entry"],
        "code": 'parse_entry(" 2021-07-04 , -3 , gas ")',
        "expected_literal": ("2021-07-04", -3, "gas"),
    },
    {
        "case_id": "parse_entry.rejects_two_fields",
        "module": "ledger.entries",
        "imports": ["parse_entry"],
        "code": 'parse_entry("2021-07-04,42")',
        "raises": "ValueError",
    },
    {
        "case_id": "parse_entry.rejects_short_date",
        "module": "ledger.entries",
        "imports": ["parse_entry"],
        "code": 'parse_entry("2021-7-04,42,x")',
        "raises": "ValueError",
    },
    {
        "case_id": "parse_entry.rejects_float_amount",
        "module": "ledger.entries",
        "imports": ["parse_entry"],
        "code": 'parse_entry("2021-07-04,4.2,x")',
        "raises": "ValueError",
    },
    {
        "case_id": "total_for.sums_matching_memo",
        "module": "ledger.entries",
        "imports": ["total_for"],
        "code": 'total_for([("d", 1, "a"), ("d", 2, "a"), ("d", 5, "b")], "a")',
        "expected_literal": 3,
    },
    {
        "case_id": "top_memos.orders_and_limits",
        "module": "ledger.entries",
        "imports": ["top_memos"],
        "code": ('top_memos([("d1", 3, "a"), ("d2", 3, "b"), ("d3", 9, "c")], 2)'),
        "expected_literal": [("c", 9), ("a", 3)],
    },
    {
        "case_id": "top_memos.empty_input",
        "module": "ledger.entries",
        "imports": ["top_memos"],
        "code": "top_memos([], 3)",
        "expected_literal": [],
    },
)

_MONEY_CASES = (
    {
        "case_id": "format_cents.grouped",
        "module": "ledger.money",
        "imports": ["format_cents"],
        "code": "format_cents(123450)",
        "expected_literal": "$1,234.50",
    },
    {
        "case_id": "format_cents.negative",
        "module": "ledger.money",
        "imports": ["format_cents"],
        "code": "format_cents(-5)",
        "expected_literal": "-$0.05",
    },
    {
        "case_id": "format_cents.zero",
        "module": "ledger.money",
        "imports": ["format_cents"],
        "code": "format_cents(0)",
        "expected_literal": "$0.00",
    },
    {
        "case_id": "parse_cents.grouped",
        "module": "ledger.money",
        "imports": ["parse_cents"],
        "code": 'parse_cents("$1,234.50")',
        "expected_literal": 123450,
    },
    {
        "case_id": "parse_cents.bare_integer",
        "module": "ledger.money",
        "imports": ["parse_cents"],
        "code": 'parse_cents("7")',
        "expected_literal": 700,
    },
    {
        "case_id": "parse_cents.rejects_three_decimals",
        "module": "ledger.money",
        "imports": ["parse_cents"],
        "code": 'parse_cents("1.234")',
        "raises": "ValueError",
    },
    {
        "case_id": "split_evenly.positive",
        "module": "ledger.money",
        "imports": ["split_evenly"],
        "code": "split_evenly(10, 3)",
        "expected_literal": [4, 3, 3],
    },
    {
        "case_id": "split_evenly.negative",
        "module": "ledger.money",
        "imports": ["split_evenly"],
        "code": "split_evenly(-10, 3)",
        "expected_literal": [-4, -3, -3],
    },
    {
        "case_id": "split_evenly.rejects_zero_parts",
        "module": "ledger.money",
        "imports": ["split_evenly"],
        "code": "split_evenly(10, 0)",
        "raises": "ValueError",
    },
)

_CLOCK_CASES = (
    {
        "case_id": "to_epoch.epoch_origin",
        "module": "providers.clock",
        "imports": ["to_epoch"],
        "code": 'to_epoch("1970-01-01")',
        "expected_literal": 0,
    },
    {
        "case_id": "to_epoch.rejects_impossible_date",
        "module": "providers.clock",
        "imports": ["to_epoch"],
        "code": 'to_epoch("2024-02-30")',
        "raises": "ValueError",
    },
    {
        "case_id": "to_epoch.rejects_unpadded_date",
        "module": "providers.clock",
        "imports": ["to_epoch"],
        "code": 'to_epoch("2024-1-5")',
        "raises": "ValueError",
    },
    {
        "case_id": "from_epoch.epoch_origin",
        "module": "providers.clock",
        "imports": ["from_epoch"],
        "code": "from_epoch(0)",
        "expected_literal": "1970-01-01",
    },
    {
        "case_id": "summarize.published_example",
        "module": "consumers.report",
        "imports": ["summarize"],
        "code": 'summarize([("2024-01-02", 5), ("2023-12-31", 7)])',
        "expected_literal": "2023-12-31|2024-01-02|12",
    },
    {
        "case_id": "summarize.rejects_empty",
        "module": "consumers.report",
        "imports": ["summarize"],
        "code": "summarize([])",
        "raises": "ValueError",
    },
)

_PRICING_FUNCTIONS = {
    "tier_rate": {"call": 'tier_rate("pro")', "value": "0.8"},
    "final_price": {"call": 'final_price(250, "pro")', "value": "200"},
    "bulk_discount": {"call": "bulk_discount(120)", "value": "0.15"},
    "cheapest": {"call": "cheapest([7, 3, 9])", "value": "3"},
}
_INVENTORY_FUNCTIONS = {
    "available": {"call": 'available("nut")', "value": "False"},
    "reserve": {"call": 'reserve("bolt", 5)', "value": "7"},
    "low_stock": {"call": "low_stock(4)", "value": ["gear"]},
    "total_units": {"call": "total_units()", "value": "22"},
}


def _checks(*items: CheckSpec) -> tuple[CheckSpec, ...]:
    seen: set[str] = set()
    for item in items:
        if item.check_id in seen:
            raise ValueError(f"duplicate check id {item.check_id!r}")
        seen.add(item.check_id)
    return tuple(items)


def p1_spec() -> VerificationSpec:
    """Read-only analysis deliverable: two Markdown reports."""

    pricing = "reports/pricing.md"
    inventory = "reports/inventory.md"
    checks = [
        CheckSpec("p1.pricing.present", "PRESENCE", CheckKind.FILE_PRESENT, pricing),
        CheckSpec("p1.inventory.present", "PRESENCE", CheckKind.FILE_PRESENT, inventory),
        CheckSpec(
            "p1.pricing.readable", "PRESENCE", CheckKind.FILE_READABLE, pricing, {"min_chars": 40}
        ),
        CheckSpec(
            "p1.inventory.readable",
            "PRESENCE",
            CheckKind.FILE_READABLE,
            inventory,
            {"min_chars": 40},
        ),
        CheckSpec(
            "p1.pricing.section",
            "STRUCTURAL",
            CheckKind.TEXT_SECTION,
            pricing,
            {"heading": "## Constants"},
        ),
        CheckSpec(
            "p1.inventory.section",
            "STRUCTURAL",
            CheckKind.TEXT_SECTION,
            inventory,
            {"heading": "## Constants"},
        ),
        CheckSpec(
            "p1.pricing.lines",
            "PUBLIC_CONTRACT",
            CheckKind.TEXT_LINES,
            pricing,
            {
                "labels": list(_PRICING_FUNCTIONS),
                "required_substrings": ["| example:", " -> "],
                "examples": _PRICING_FUNCTIONS,
                "section": None,
            },
        ),
        CheckSpec(
            "p1.inventory.lines",
            "PUBLIC_CONTRACT",
            CheckKind.TEXT_LINES,
            inventory,
            {
                "labels": list(_INVENTORY_FUNCTIONS),
                "required_substrings": ["| example:", " -> "],
                "examples": _INVENTORY_FUNCTIONS,
                "section": None,
            },
        ),
        CheckSpec(
            "p1.pricing.constants",
            "PUBLIC_CONTRACT",
            CheckKind.TEXT_LINES,
            pricing,
            {
                "labels": ["TIERS", "DEFAULT_TIER", "EXPEDITE_SURCHARGE"],
                "constants": {"DEFAULT_TIER": '"basic"', "EXPEDITE_SURCHARGE": "0.05"},
                "section": "## Constants",
            },
        ),
        CheckSpec(
            "p1.inventory.constants",
            "PUBLIC_CONTRACT",
            CheckKind.TEXT_LINES,
            inventory,
            {
                "labels": ["STOCK", "LOW_STOCK_THRESHOLD"],
                "constants": {"LOW_STOCK_THRESHOLD": "5"},
                "section": "## Constants",
            },
        ),
        CheckSpec(
            "p1.pricing.prose_semantics",
            "PUBLIC_CONTRACT",
            CheckKind.TEXT_LINES,
            pricing,
            {"labels": []},
            authoritative=False,
        ),
    ]
    return VerificationSpec(
        spec_id="stage-r1-2/p1-read-only-analysis",
        version="1.0.0",
        deliverable_type="markdown_analysis",
        required_artifacts=(pricing, inventory),
        checks=_checks(*checks),
        provenance="public_contract_document",
        notes="Report format is published in the worker prompt; prose correctness is UNKNOWN.",
    ).with_declared_digest()


def p2_spec() -> VerificationSpec:
    """Two independent modules, both implemented from their own visible docstrings."""

    entries = "ledger/entries.py"
    money = "ledger/money.py"
    checks = [
        CheckSpec("p2.entries.present", "PRESENCE", CheckKind.FILE_PRESENT, entries),
        CheckSpec("p2.money.present", "PRESENCE", CheckKind.FILE_PRESENT, money),
        CheckSpec("p2.entries.parses", "STRUCTURAL", CheckKind.PYTHON_PARSES, entries),
        CheckSpec("p2.money.parses", "STRUCTURAL", CheckKind.PYTHON_PARSES, money),
        CheckSpec(
            "p2.entries.functions",
            "STRUCTURAL",
            CheckKind.FUNCTION_PRESENT,
            entries,
            {"functions": list(_ENTRY_FUNCTIONS)},
        ),
        CheckSpec(
            "p2.money.functions",
            "STRUCTURAL",
            CheckKind.FUNCTION_PRESENT,
            money,
            {"functions": list(_MONEY_FUNCTIONS)},
        ),
        CheckSpec(
            "p2.entries.signatures",
            "STRUCTURAL",
            CheckKind.SIGNATURE_OK,
            entries,
            {"signatures": ENTRIES_SIGNATURES},
        ),
        CheckSpec(
            "p2.money.signatures",
            "STRUCTURAL",
            CheckKind.SIGNATURE_OK,
            money,
            {"signatures": MONEY_SIGNATURES},
        ),
        CheckSpec(
            "p2.entries.implemented",
            "STRUCTURAL",
            CheckKind.NO_NOT_IMPLEMENTED,
            entries,
            {"functions": list(_ENTRY_FUNCTIONS)},
        ),
        CheckSpec(
            "p2.money.implemented",
            "STRUCTURAL",
            CheckKind.NO_NOT_IMPLEMENTED,
            money,
            {"functions": list(_MONEY_FUNCTIONS)},
        ),
        CheckSpec(
            "p2.entries.independence",
            "STRUCTURAL",
            CheckKind.NO_FORBIDDEN_IMPORT,
            entries,
            {"modules": ["ledger.money"]},
        ),
        CheckSpec(
            "p2.money.independence",
            "STRUCTURAL",
            CheckKind.NO_FORBIDDEN_IMPORT,
            money,
            {"modules": ["ledger.entries"]},
        ),
        CheckSpec(
            "p2.public_examples",
            "PUBLIC_CONTRACT",
            CheckKind.PUBLIC_EXAMPLE,
            entries,
            {"cases": [*_ENTRIES_CASES, *_MONEY_CASES]},
        ),
    ]
    return VerificationSpec(
        spec_id="stage-r1-2/p2-independent-modules",
        version="1.0.0",
        deliverable_type="python_modules",
        required_artifacts=(entries, money),
        checks=_checks(*checks),
        provenance="visible_deliverable_docstring",
        notes="Every example is quoted in the visible stub docstrings.",
    ).with_declared_digest()


def p3_spec() -> VerificationSpec:
    """Provider/consumer pair specified by a frozen interface document."""

    clock = "providers/clock.py"
    report = "consumers/report.py"
    interface = "docs/INTERFACE.md"
    checks = [
        CheckSpec("p3.clock.present", "PRESENCE", CheckKind.FILE_PRESENT, clock),
        CheckSpec("p3.report.present", "PRESENCE", CheckKind.FILE_PRESENT, report),
        CheckSpec("p3.interface.present", "PRESENCE", CheckKind.FILE_PRESENT, interface),
        CheckSpec("p3.clock.parses", "STRUCTURAL", CheckKind.PYTHON_PARSES, clock),
        CheckSpec("p3.report.parses", "STRUCTURAL", CheckKind.PYTHON_PARSES, report),
        CheckSpec(
            "p3.clock.functions",
            "STRUCTURAL",
            CheckKind.FUNCTION_PRESENT,
            clock,
            {"functions": ["to_epoch", "from_epoch"]},
        ),
        CheckSpec(
            "p3.report.functions",
            "STRUCTURAL",
            CheckKind.FUNCTION_PRESENT,
            report,
            {"functions": ["summarize"]},
        ),
        CheckSpec(
            "p3.clock.signatures",
            "STRUCTURAL",
            CheckKind.SIGNATURE_OK,
            clock,
            {"signatures": {"to_epoch": "to_epoch(text)", "from_epoch": "from_epoch(value)"}},
        ),
        CheckSpec(
            "p3.report.signatures",
            "STRUCTURAL",
            CheckKind.SIGNATURE_OK,
            report,
            {"signatures": {"summarize": "summarize(rows)"}},
        ),
        CheckSpec(
            "p3.clock.implemented",
            "STRUCTURAL",
            CheckKind.NO_NOT_IMPLEMENTED,
            clock,
            {"functions": ["to_epoch", "from_epoch"]},
        ),
        CheckSpec(
            "p3.report.implemented",
            "STRUCTURAL",
            CheckKind.NO_NOT_IMPLEMENTED,
            report,
            {"functions": ["summarize"]},
        ),
        CheckSpec(
            "p3.interface.frozen",
            "STRUCTURAL",
            CheckKind.FILE_UNCHANGED,
            interface,
            {"frozen": True},
        ),
        CheckSpec(
            "p3.consumer.binding",
            "INTEGRATION",
            CheckKind.IMPORT_BINDING,
            report,
            {"module": "providers.clock", "names": ["to_epoch", "from_epoch"]},
        ),
        CheckSpec(
            "p3.public_examples",
            "PUBLIC_CONTRACT",
            CheckKind.PUBLIC_EXAMPLE,
            clock,
            {"cases": list(_CLOCK_CASES)},
        ),
    ]
    return VerificationSpec(
        spec_id="stage-r1-2/p3-frozen-interface",
        version="1.0.0",
        deliverable_type="python_modules",
        required_artifacts=(clock, report, interface),
        checks=_checks(*checks),
        provenance="public_contract_document",
        notes="Rules come from docs/INTERFACE.md, which every worker could read.",
    ).with_declared_digest()


#: Registry used by the offline harness and by the evaluation runner.
SPECS_BY_SAMPLE = {
    "r05-p1-dependency-audit": p1_spec,
    "r05-p2-modules": p2_spec,
    "r05-p3-frozen-interface": p3_spec,
}


def spec_for_sample(sample_id: str) -> VerificationSpec | None:
    """Resolve a contract by sample id, tolerating the ``-a1``/``-a2`` fixture variant suffix."""

    factory = SPECS_BY_SAMPLE.get(sample_id)
    if factory is None and "-a" in sample_id:
        base = sample_id.rsplit("-a", 1)[0]
        factory = SPECS_BY_SAMPLE.get(base)
    return factory() if factory else None


def category_count(spec: VerificationSpec) -> dict[str, int]:
    counts = dict.fromkeys(CATEGORIES, 0)
    for check in spec.checks:
        counts[check.category] += 1
    return counts
