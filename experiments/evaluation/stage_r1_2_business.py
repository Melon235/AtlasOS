"""Synthetic supplier-review deliverable for the business verification track.

All data is fabricated. The point is to show that the verification contract is not tied to Python
deliverables: the same :class:`VerificationSpec` / finding interface validates a structured business
artifact, its arithmetic, its hard constraints and its evidence references.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from atlasos.verification.models import CheckKind, CheckSpec, VerificationSpec

ARTIFACT = "reviews/supplier_review.json"

BUDGET = 800_000
DELIVERY_LIMIT_DAYS = 90
MAX_LEAD_TIME_DAYS = 180

#: Registered source documents. A citation must name one of these ids.
SUPPLIER_SOURCES: dict[str, dict[str, Any]] = {
    "quote_a": {
        "supplier_id": "supplier_a",
        "quoted_price": 650_000,
        "shipping_cost": 12_000,
        "delivery_days": 75,
        "technical_qualified": True,
        "notes": "synthetic quotation; quantity 500 units, FOB",
    },
    "spec_a": {
        "supplier_id": "supplier_a",
        "min_tolerance_mm": 0.05,
        "max_operating_temp_c": 85,
        "certifications": ["ISO9001"],
    },
    "spec_b": {
        "supplier_id": "supplier_b",
        "min_tolerance_mm": 0.2,
        "max_operating_temp_c": 70,
        "certifications": [],
    },
}

REQUIRED_FIELDS = {
    "supplier_id": "string",
    "technical_qualified": "boolean",
    "quoted_price": "integer",
    "shipping_cost": "integer",
    "total_cost": "integer",
    "delivery_days": "integer",
    "risk_flags": "array",
    "evidence_refs": "array",
}


def example_artifact() -> dict[str, Any]:
    return {
        "supplier_id": "supplier_a",
        "technical_qualified": True,
        "quoted_price": 650_000,
        "shipping_cost": 12_000,
        "total_cost": 662_000,
        "delivery_days": 75,
        "risk_flags": [],
        "evidence_refs": ["quote_a:price", "spec_a:performance"],
    }


def business_spec() -> VerificationSpec:
    checks = [
        CheckSpec("biz.present", "PRESENCE", CheckKind.FILE_PRESENT, ARTIFACT),
        CheckSpec("biz.readable", "PRESENCE", CheckKind.FILE_READABLE, ARTIFACT, {"min_chars": 20}),
        CheckSpec(
            "biz.schema",
            "STRUCTURAL",
            CheckKind.JSON_SCHEMA,
            ARTIFACT,
            {"required": REQUIRED_FIELDS},
        ),
        CheckSpec(
            "biz.total_cost",
            "PUBLIC_CONTRACT",
            CheckKind.ARITHMETIC,
            ARTIFACT,
            {"target": "total_cost", "sum_of": ["quoted_price", "shipping_cost"]},
        ),
        CheckSpec(
            "biz.ranges",
            "PUBLIC_CONTRACT",
            CheckKind.NUMERIC_RANGE,
            ARTIFACT,
            {
                "ranges": {
                    "quoted_price": [0, 5_000_000],
                    "shipping_cost": [0, 500_000],
                    "delivery_days": [1, MAX_LEAD_TIME_DAYS],
                }
            },
        ),
        CheckSpec(
            "biz.budget",
            "PUBLIC_CONTRACT",
            CheckKind.CONSTRAINT,
            ARTIFACT,
            {"field": "total_cost", "relation": "le", "limit": BUDGET},
        ),
        CheckSpec(
            "biz.delivery",
            "PUBLIC_CONTRACT",
            CheckKind.CONSTRAINT,
            ARTIFACT,
            {"field": "delivery_days", "relation": "le", "limit": DELIVERY_LIMIT_DAYS},
        ),
        CheckSpec(
            "biz.evidence_refs",
            "PUBLIC_CONTRACT",
            CheckKind.EVIDENCE_REFS,
            ARTIFACT,
            {"field": "evidence_refs", "registry": sorted(SUPPLIER_SOURCES), "min_refs": 1},
        ),
        CheckSpec(
            "biz.evidence_semantics",
            "PUBLIC_CONTRACT",
            CheckKind.EVIDENCE_REFS,
            ARTIFACT,
            {"field": "evidence_refs", "registry": [], "min_refs": 1},
            authoritative=False,
        ),
    ]
    return VerificationSpec(
        spec_id="stage-r1-2/business-supplier-review",
        version="1.0.0",
        deliverable_type="structured_business_artifact",
        required_artifacts=(ARTIFACT,),
        checks=tuple(checks),
        provenance="trusted_host_declaration",
        notes="Synthetic procurement review; schema, arithmetic and hard limits are decidable.",
    ).with_declared_digest()


def write_artifact(snapshot: Path, payload: dict[str, Any] | None = None) -> Path:
    target = snapshot / ARTIFACT
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload or example_artifact(), indent=2), encoding="utf-8")
    return target


__all__ = [
    "ARTIFACT",
    "BUDGET",
    "DELIVERY_LIMIT_DAYS",
    "REQUIRED_FIELDS",
    "SUPPLIER_SOURCES",
    "business_spec",
    "example_artifact",
    "write_artifact",
]
