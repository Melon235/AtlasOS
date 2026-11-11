"""R1-2A: structured business deliverables and Markdown report deliverables."""

from __future__ import annotations

import json
from pathlib import Path

from experiments.evaluation import stage_r1_2_business as business
from experiments.evaluation.stage_r1_2_contracts import p1_spec

from atlasos.verification import models
from atlasos.verification.engine import VerificationEngine
from atlasos.verification.models import CheckKind, CheckSpec, VerificationSpec

GOOD_PRICING_REPORT = """# Pricing

tier_rate: KeyError | example: tier_rate("pro") -> 0.8
final_price: KeyError | example: final_price(250, "pro") -> 200
bulk_discount: ValueError | example: bulk_discount(120) -> 0.15
cheapest: ValueError | example: cheapest([7, 3, 9]) -> 3

## Constants
TIERS = {"basic": 1.0, "pro": 0.8, "enterprise": 0.6}
DEFAULT_TIER = "basic"
EXPEDITE_SURCHARGE = 0.05
"""

GOOD_INVENTORY_REPORT = """# Inventory

available: none | example: available("nut") -> False
reserve: ValueError, LookupError, RuntimeError | example: reserve("bolt", 5) -> 7
low_stock: none | example: low_stock(4) -> ["gear"]
total_units: none | example: total_units() -> 22

## Constants
STOCK = {"bolt": 12, "nut": 0, "washer": 7, "gear": 3}
LOW_STOCK_THRESHOLD = 5
"""


def _write(root: Path, relative: str, text: str) -> None:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _artifact(root: Path, payload: dict) -> None:
    target = root / business.ARTIFACT
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload), encoding="utf-8")


def _verify_business(root: Path):
    return VerificationEngine(None).verify(business.business_spec(), root)


def test_business_artifact_passes_schema_and_limits(tmp_path: Path) -> None:
    _artifact(tmp_path, business.example_artifact())
    result = _verify_business(tmp_path)
    statuses = result.category_statuses()
    assert statuses["deliverable_presence"] == "PASS"
    assert statuses["structural_validity"] == "PASS"
    # the evidence-semantics rule is deliberately UNKNOWN, everything decidable passed
    assert statuses["public_contract_status"] == "UNKNOWN"
    assert not result.failures()


def test_business_schema_violation_fails(tmp_path: Path) -> None:
    payload = business.example_artifact()
    payload.pop("shipping_cost")
    payload["delivery_days"] = "75"
    _artifact(tmp_path, payload)
    result = _verify_business(tmp_path)
    assert result.status_for("STRUCTURAL") == "FAIL"
    assert result.findings[-1].reason_code == models.REASON_PARSE_ERROR or any(
        item.reason_code == models.REASON_SCHEMA_VIOLATION for item in result.findings
    )


def test_business_total_cost_must_add_up(tmp_path: Path) -> None:
    payload = business.example_artifact()
    payload["total_cost"] = 900_000
    _artifact(tmp_path, payload)
    result = _verify_business(tmp_path)
    codes = {item.reason_code for item in result.failures()}
    assert models.REASON_ARITHMETIC_MISMATCH in codes
    assert models.REASON_CONSTRAINT_VIOLATED in codes


def test_business_delivery_limit_is_enforced(tmp_path: Path) -> None:
    payload = business.example_artifact()
    payload["delivery_days"] = 120
    _artifact(tmp_path, payload)
    result = _verify_business(tmp_path)
    failed = {item.check_id for item in result.failures()}
    assert "biz.delivery" in failed
    assert "biz.budget" not in failed


def test_business_unregistered_evidence_ref_fails(tmp_path: Path) -> None:
    payload = business.example_artifact()
    payload["evidence_refs"] = ["quote_a:price", "spec_z:performance"]
    _artifact(tmp_path, payload)
    result = _verify_business(tmp_path)
    codes = {item.reason_code for item in result.failures()}
    assert models.REASON_EVIDENCE_MISSING_REF in codes


def test_business_evidence_semantics_stay_unknown(tmp_path: Path) -> None:
    _artifact(tmp_path, business.example_artifact())
    result = _verify_business(tmp_path)
    semantics = [item for item in result.findings if item.check_id == "biz.evidence_semantics"]
    assert semantics and semantics[0].status == "UNKNOWN"
    assert semantics[0].reason_code == models.REASON_NON_AUTHORITATIVE


def test_business_numeric_range_violation_fails(tmp_path: Path) -> None:
    payload = business.example_artifact()
    payload["quoted_price"] = 9_000_000
    payload["total_cost"] = 9_012_000
    _artifact(tmp_path, payload)
    result = _verify_business(tmp_path)
    codes = {item.reason_code for item in result.failures()}
    assert models.REASON_OUT_OF_RANGE in codes


def test_business_non_json_artifact_fails(tmp_path: Path) -> None:
    _write(tmp_path, business.ARTIFACT, "{not json")
    result = _verify_business(tmp_path)
    assert result.status_for("PRESENCE") == "FAIL"


def test_p1_reports_pass_when_the_published_shape_is_followed(tmp_path: Path) -> None:
    _write(tmp_path, "reports/pricing.md", GOOD_PRICING_REPORT)
    _write(tmp_path, "reports/inventory.md", GOOD_INVENTORY_REPORT)
    result = VerificationEngine(None).verify(p1_spec(), tmp_path)
    statuses = result.category_statuses()
    assert statuses["deliverable_presence"] == "PASS"
    assert statuses["structural_validity"] == "PASS"
    assert statuses["public_contract_status"] == "UNKNOWN"  # prose semantics only
    assert not result.failures()


def test_p1_reports_fail_on_a_wrong_reported_value(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "reports/pricing.md",
        GOOD_PRICING_REPORT.replace("-> 200", "-> 250"),
    )
    _write(tmp_path, "reports/inventory.md", GOOD_INVENTORY_REPORT)
    result = VerificationEngine(None).verify(p1_spec(), tmp_path)
    assert result.status_for("PUBLIC_CONTRACT") == "FAIL"
    assert any("final_price" in item.detail for item in result.failures())


def test_p1_missing_constants_section_fails(tmp_path: Path) -> None:
    _write(tmp_path, "reports/pricing.md", GOOD_PRICING_REPORT.split("## Constants")[0])
    _write(tmp_path, "reports/inventory.md", GOOD_INVENTORY_REPORT)
    result = VerificationEngine(None).verify(p1_spec(), tmp_path)
    assert result.status_for("STRUCTURAL") == "FAIL"


def test_text_check_needs_a_real_artifact(tmp_path: Path) -> None:
    spec = VerificationSpec(
        spec_id="test/text",
        version="1.0.0",
        deliverable_type="markdown_analysis",
        required_artifacts=("reports/x.md",),
        checks=(
            CheckSpec(
                "section",
                "STRUCTURAL",
                CheckKind.TEXT_SECTION,
                "reports/x.md",
                {"heading": "## Constants"},
            ),
        ),
    ).with_declared_digest()
    result = VerificationEngine().verify(spec, tmp_path)
    assert result.findings[0].reason_code == models.REASON_MISSING
