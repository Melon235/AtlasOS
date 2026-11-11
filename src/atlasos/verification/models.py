"""Declarative verification contract and result models.

The verification layer answers one question only: *is this deliverable complete, structurally
valid and consistent with the contracts the worker was allowed to see?* It is not an oracle, it
never executes untrusted code on the host, and it never feeds back into the run's own success
judgement in ``observe`` mode.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

#: Check categories. They are reported separately so that a structural defect is never averaged
#: into a public-contract score.
Category = Literal["PRESENCE", "STRUCTURAL", "PUBLIC_CONTRACT", "INTEGRATION"]
CATEGORIES: tuple[Category, ...] = ("PRESENCE", "STRUCTURAL", "PUBLIC_CONTRACT", "INTEGRATION")

Status = Literal["PASS", "FAIL", "UNKNOWN", "NOT_APPLICABLE"]
_STATUS_ORDER = {"FAIL": 0, "UNKNOWN": 1, "PASS": 2, "NOT_APPLICABLE": 3}

Verdict = Literal["PASS", "FAIL", "PARTIAL", "INCONCLUSIVE"]

#: Where a contract rule came from. Only the first two may authorize a FAIL.
Provenance = Literal[
    "trusted_host_declaration",
    "public_contract_document",
    "visible_deliverable_docstring",
]


class CheckKind(str):
    """String constants for the concrete checks the engine can run."""

    FILE_PRESENT = "file.present"
    FILE_READABLE = "file.readable"
    FILE_UNCHANGED = "file.unchanged"
    ARTIFACT_RECORDED = "artifact.recorded"

    PYTHON_PARSES = "python.parses"
    FUNCTION_PRESENT = "python.function_present"
    SIGNATURE_OK = "python.signature"
    NO_NOT_IMPLEMENTED = "python.no_not_implemented"
    NO_FORBIDDEN_IMPORT = "python.no_forbidden_import"

    TEXT_SECTION = "text.section"
    TEXT_LINES = "text.lines"

    JSON_SCHEMA = "structure.json_schema"
    ARITHMETIC = "structure.arithmetic"
    NUMERIC_RANGE = "structure.numeric_range"
    CONSTRAINT = "structure.constraint"
    EVIDENCE_REFS = "structure.evidence_refs"

    IMPORT_BINDING = "integration.import_binding"
    PUBLIC_EXAMPLE = "integration.public_example"


STATIC_KINDS: frozenset[str] = frozenset(
    {
        CheckKind.FILE_PRESENT,
        CheckKind.FILE_READABLE,
        CheckKind.FILE_UNCHANGED,
        CheckKind.ARTIFACT_RECORDED,
        CheckKind.PYTHON_PARSES,
        CheckKind.FUNCTION_PRESENT,
        CheckKind.SIGNATURE_OK,
        CheckKind.NO_NOT_IMPLEMENTED,
        CheckKind.NO_FORBIDDEN_IMPORT,
        CheckKind.TEXT_SECTION,
        CheckKind.TEXT_LINES,
        CheckKind.JSON_SCHEMA,
        CheckKind.ARITHMETIC,
        CheckKind.NUMERIC_RANGE,
        CheckKind.CONSTRAINT,
        CheckKind.EVIDENCE_REFS,
        CheckKind.IMPORT_BINDING,
    }
)

#: Kinds that need the artifact to be imported/called inside an isolated sandbox.
DYNAMIC_KINDS: frozenset[str] = frozenset({CheckKind.PUBLIC_EXAMPLE})


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def canonical_digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CheckSpec:
    """One declarative check inside a :class:`VerificationSpec`."""

    check_id: str
    category: Category
    kind: str
    artifact: str | None = None
    params: Mapping[str, Any] = field(default_factory=dict)
    #: ``False`` for a check whose rule cannot be justified from a trusted or public source; the
    #: engine reports those as UNKNOWN instead of failing the deliverable.
    authoritative: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "category": self.category,
            "kind": self.kind,
            "artifact": self.artifact,
            "params": dict(self.params),
            "authoritative": self.authoritative,
        }


@dataclass(frozen=True)
class VerificationSpec:
    """Host-owned acceptance contract for one deliverable type.

    The spec is data. It is constructed from a trusted task definition, is digested, and is
    validated against that digest before every run so a worker (or a corrupted record) cannot
    silently change its own acceptance criteria.
    """

    spec_id: str
    version: str
    deliverable_type: str
    required_artifacts: tuple[str, ...]
    checks: tuple[CheckSpec, ...]
    provenance: Provenance = "trusted_host_declaration"
    public_contract_digest: str | None = None
    notes: str = ""
    #: Set by the builder; recomputed and compared before use.
    declared_digest: str = ""

    def payload(self) -> dict[str, Any]:
        return {
            "spec_id": self.spec_id,
            "version": self.version,
            "deliverable_type": self.deliverable_type,
            "required_artifacts": list(self.required_artifacts),
            "checks": [check.as_dict() for check in self.checks],
            "provenance": self.provenance,
            "public_contract_digest": self.public_contract_digest,
        }

    @property
    def digest(self) -> str:
        return canonical_digest(self.payload())

    def is_intact(self) -> bool:
        return bool(self.declared_digest) and self.declared_digest == self.digest

    def with_declared_digest(self) -> VerificationSpec:
        return VerificationSpec(
            spec_id=self.spec_id,
            version=self.version,
            deliverable_type=self.deliverable_type,
            required_artifacts=self.required_artifacts,
            checks=self.checks,
            provenance=self.provenance,
            public_contract_digest=self.public_contract_digest,
            notes=self.notes,
            declared_digest=self.digest,
        )


@dataclass(frozen=True)
class VerificationFinding:
    check_id: str
    category: Category
    status: Status
    reason_code: str
    artifact: str | None = None
    detail: str = ""
    evidence_digest: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "category": self.category,
            "status": self.status,
            "reason_code": self.reason_code,
            "artifact": self.artifact,
            "detail": self.detail[:2000],
            "evidence_digest": self.evidence_digest,
        }


@dataclass
class VerificationResult:
    spec_id: str
    spec_version: str
    spec_digest: str
    deliverable_type: str
    artifact_digest: str
    findings: list[VerificationFinding] = field(default_factory=list)
    dynamic_mode: str = "unavailable"
    verification_wall_ms: float | None = None
    verification_cpu_ms: float | None = None
    sandbox_cost_usd: float | None = None
    error: str | None = None

    # -- aggregation -----------------------------------------------------
    def status_for(self, category: Category) -> Status:
        statuses = [finding.status for finding in self.findings if finding.category == category]
        if not statuses:
            return "NOT_APPLICABLE"
        if "FAIL" in statuses:
            return "FAIL"
        if "UNKNOWN" in statuses:
            return "UNKNOWN"
        if "PASS" in statuses:
            return "PASS"
        return "NOT_APPLICABLE"

    def category_statuses(self) -> dict[str, Status]:
        return {
            "deliverable_presence": self.status_for("PRESENCE"),
            "structural_validity": self.status_for("STRUCTURAL"),
            "public_contract_status": self.status_for("PUBLIC_CONTRACT"),
            "integration_status": self.status_for("INTEGRATION"),
        }

    @property
    def overall(self) -> Status:
        statuses = list(self.category_statuses().values())
        if "FAIL" in statuses:
            return "FAIL"
        if "UNKNOWN" in statuses:
            return "UNKNOWN"
        if "PASS" in statuses:
            return "PASS"
        return "NOT_APPLICABLE"

    def failures(self) -> list[VerificationFinding]:
        return [finding for finding in self.findings if finding.status == "FAIL"]

    def unknowns(self) -> list[VerificationFinding]:
        return [finding for finding in self.findings if finding.status == "UNKNOWN"]

    def as_dict(self) -> dict[str, Any]:
        return {
            "spec_id": self.spec_id,
            "spec_version": self.spec_version,
            "spec_digest": self.spec_digest,
            "deliverable_type": self.deliverable_type,
            "artifact_digest": self.artifact_digest,
            "dynamic_mode": self.dynamic_mode,
            "verification_wall_ms": self.verification_wall_ms,
            "verification_cpu_ms": self.verification_cpu_ms,
            "sandbox_cost_usd": self.sandbox_cost_usd,
            "error": self.error,
            **self.category_statuses(),
            "verification_overall": self.overall,
            "findings": [finding.as_dict() for finding in self.findings],
        }


def merge_status(statuses: list[Status]) -> Status:
    """Worst-status-wins aggregation used for a single check group."""

    if not statuses:
        return "NOT_APPLICABLE"
    return sorted(statuses, key=lambda item: _STATUS_ORDER[item])[0]


REASON_OK = "OK"
REASON_MISSING = "MISSING"
REASON_EMPTY = "EMPTY"
REASON_UNREADABLE = "UNREADABLE"
REASON_WRONG_TYPE = "WRONG_TYPE"
REASON_UNEXPECTED_CHANGE = "UNEXPECTED_CHANGE"
REASON_NOT_RECORDED = "NOT_RECORDED"
REASON_PARSE_ERROR = "PARSE_ERROR"
REASON_MISSING_FUNCTION = "MISSING_FUNCTION"
REASON_SIGNATURE_MISMATCH = "SIGNATURE_MISMATCH"
REASON_NOT_IMPLEMENTED = "NOT_IMPLEMENTED"
REASON_FORBIDDEN_IMPORT = "FORBIDDEN_IMPORT"
REASON_SECTION_MISSING = "SECTION_MISSING"
REASON_SHAPE_MISMATCH = "SHAPE_MISMATCH"
REASON_SCHEMA_VIOLATION = "SCHEMA_VIOLATION"
REASON_ARITHMETIC_MISMATCH = "ARITHMETIC_MISMATCH"
REASON_OUT_OF_RANGE = "OUT_OF_RANGE"
REASON_CONSTRAINT_VIOLATED = "CONSTRAINT_VIOLATED"
REASON_EVIDENCE_MISSING_REF = "EVIDENCE_MISSING_REF"
REASON_EVIDENCE_UNCHECKED = "EVIDENCE_SEMANTICS_UNVERIFIED"
REASON_BINDING_MISMATCH = "BINDING_MISMATCH"
REASON_SPEC_TAMPERED = "SPEC_DIGEST_MISMATCH"
REASON_ARTIFACT_MISMATCH = "ARTIFACT_DIGEST_MISMATCH"
REASON_DYNAMIC_UNAVAILABLE = "DYNAMIC_VERIFICATION_UNAVAILABLE"
REASON_DYNAMIC_ERROR = "DYNAMIC_VERIFICATION_ERROR"
REASON_DYNAMIC_TIMEOUT = "DYNAMIC_VERIFICATION_TIMEOUT"
REASON_UNSUPPORTED = "UNSUPPORTED_CHECK"
REASON_NON_AUTHORITATIVE = "NON_AUTHORITATIVE_RULE"
