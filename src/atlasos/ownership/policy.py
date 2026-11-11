"""M3 external-policy adapter that enforces workspace ownership.

The gate already offers one authorization seam (``ActionSafetyGate.external_policy``) and one
failure semantic: any deny becomes ``DENY`` with the adapter's reason codes, which are audited and
recorded in the trajectory. This module uses exactly that seam; it does not wrap the executor, does
not re-implement M3, and cannot upgrade a deny to an allow.

Modes
-----
``off``      never installed.
``audit``    the same analysis runs, findings are collected, execution is unchanged.
``enforce``  unrecognized identities, unverifiable paths, and any write outside the manifest are
             denied before the tool body runs.

Enforcement coverage is ``PARTIAL``: file tools are checked exactly; shell commands are checked by
textual classification, and a command whose write set cannot be proven is refused rather than
trusted.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from anycode.contracts.models import PolicyDecision
from anycode.identity.policy import PolicyEnforcer, PolicyRequest

from atlasos.ownership.models import (
    OWNERSHIP_COVERAGE_PARTIAL,
    OWNERSHIP_DENIED_FOREIGN_PATH,
    OWNERSHIP_DENIED_UNDECLARED_PATH,
    OWNERSHIP_DENIED_WORKSPACE_WIDE,
    OWNERSHIP_INVALID_MANIFEST,
    OWNERSHIP_MODE_ENFORCE,
    OWNERSHIP_MODE_OFF,
    OWNERSHIP_MODES,
    OWNERSHIP_PATH_ESCAPE,
    OWNERSHIP_POLICY_VERSION,
    OWNERSHIP_UNCLASSIFIABLE_SHELL,
    OWNERSHIP_UNKNOWN_WORKER,
    WorkspaceOwnershipManifest,
)
from atlasos.ownership.paths import OwnershipPathError, normalize_workspace_path
from atlasos.ownership.shell import (
    KNOWN_WRITE_TARGETS,
    PROVEN_READ_ONLY,
    WORKSPACE_WIDE_WRITE,
    classify_shell_command,
)

FILE_WRITE_TOOLS = frozenset({"file_write", "file_edit"})
SHELL_TOOLS = frozenset({"bash"})

#: Character devices that are legitimate write sinks and never touch the workspace.
DEVICE_SINKS = frozenset({"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty"})

_EVIDENCE_LIMIT = 200


@dataclass(frozen=True)
class OwnershipFinding:
    """One observed ownership event, kept for audit and evaluation."""

    worker_id: str | None
    task_id: str | None
    tool_name: str
    target: str
    reason_code: str
    decision: str
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "task_id": self.task_id,
            "tool_name": self.tool_name,
            "target": self.target,
            "reason_code": self.reason_code,
            "decision": self.decision,
            "detail": self.detail,
        }


@dataclass
class OwnershipEvidence:
    """Evaluation-only counters. Evidence never changes an authorization decision."""

    mode: str
    manifest_digest: str
    policy_version: str
    coverage: str
    checked_calls: int = 0
    allowed_calls: int = 0
    denied_calls: int = 0
    denials: list[dict[str, Any]] = field(default_factory=list)
    audit_findings: list[dict[str, Any]] = field(default_factory=list)
    truncated: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "manifest_digest": self.manifest_digest,
            "policy_version": self.policy_version,
            "coverage": self.coverage,
            "checked_calls": self.checked_calls,
            "allowed_calls": self.allowed_calls,
            "denied_calls": self.denied_calls,
            "denials": self.denials,
            "audit_findings": self.audit_findings,
            "evidence_truncated": self.truncated,
        }


class WorkspaceOwnershipPolicy:
    """``ExternalPolicyAdapter`` implementation for the M3 gate."""

    def __init__(
        self,
        manifest: WorkspaceOwnershipManifest,
        *,
        mode: str,
        coverage: str = OWNERSHIP_COVERAGE_PARTIAL,
    ) -> None:
        if mode not in OWNERSHIP_MODES:
            raise ValueError(f"unknown ownership mode {mode!r}")
        self.manifest = manifest
        self.mode = mode
        self.coverage = coverage
        self.evidence = OwnershipEvidence(
            mode=mode,
            manifest_digest=manifest.digest,
            policy_version=manifest.policy_version,
            coverage=coverage,
        )

    # -- ExternalPolicyAdapter -------------------------------------------------

    async def decide(self, request: PolicyRequest) -> PolicyDecision:
        if self.mode == OWNERSHIP_MODE_OFF:
            return self._decision(request, allowed=True)

        reason_code, target, detail = self._evaluate(request)
        allowed = reason_code is None
        self.evidence.checked_calls += 1
        if allowed:
            self.evidence.allowed_calls += 1
            return self._decision(request, allowed=True)

        finding = OwnershipFinding(
            worker_id=_attribute(request, "worker_id"),
            task_id=_attribute(request, "task_id"),
            tool_name=_tool_name(request),
            target=target,
            reason_code=reason_code,
            decision="DENY" if self.mode == OWNERSHIP_MODE_ENFORCE else "AUDIT",
            detail=detail,
        )
        if self.mode == OWNERSHIP_MODE_ENFORCE:
            self.evidence.denied_calls += 1
            _bounded_append(self.evidence.denials, finding.as_dict(), self.evidence)
            return self._decision(request, allowed=False, reason_codes=(reason_code,))
        _bounded_append(self.evidence.audit_findings, finding.as_dict(), self.evidence)
        return self._decision(request, allowed=True)

    # -- analysis -------------------------------------------------------------

    def _evaluate(self, request: PolicyRequest) -> tuple[str | None, str, str]:
        """Return ``(reason_code, target, detail)``; ``reason_code`` None means allowed."""

        manifest = self.manifest
        if not manifest.is_valid():
            return OWNERSHIP_INVALID_MANIFEST, "<manifest>", "manifest digest mismatch"
        if not manifest.workspace_root:
            return OWNERSHIP_INVALID_MANIFEST, "<manifest>", "manifest has no workspace root"

        worker_id = _attribute(request, "worker_id")
        task_id = _attribute(request, "task_id")
        if worker_id is None:
            return OWNERSHIP_UNKNOWN_WORKER, "<identity>", "tool call carries no worker identity"
        if worker_id not in manifest.worker_ownership:
            return OWNERSHIP_UNKNOWN_WORKER, worker_id, "worker is not declared in the manifest"
        declared_task = manifest.task_of(worker_id)
        if declared_task is not None and task_id is not None and declared_task != task_id:
            return (
                OWNERSHIP_UNKNOWN_WORKER,
                worker_id,
                f"runtime task {task_id!r} does not match manifest task {declared_task!r}",
            )

        tool_name = _tool_name(request)
        if tool_name in FILE_WRITE_TOOLS:
            return self._check_file_write(request, worker_id, tool_name)
        if tool_name in SHELL_TOOLS:
            return self._check_shell(request, worker_id, tool_name)
        return None, tool_name, "tool does not write the shared workspace"

    def _check_file_write(
        self, request: PolicyRequest, worker_id: str, tool_name: str
    ) -> tuple[str | None, str, str]:
        raw_path = _arguments(request).get("path")
        try:
            target = normalize_workspace_path(raw_path, workspace_root=self.manifest.workspace_root)
        except OwnershipPathError as error:
            return OWNERSHIP_PATH_ESCAPE, str(raw_path), str(error)
        return self._authorize_target(worker_id, target, tool_name, "<file tool>")

    def _check_shell(
        self, request: PolicyRequest, worker_id: str, tool_name: str
    ) -> tuple[str | None, str, str]:
        command = str(_arguments(request).get("command") or "")
        assessment = classify_shell_command(command)
        if assessment.kind == PROVEN_READ_ONLY:
            return None, "<read-only>", assessment.reason
        if assessment.kind in (WORKSPACE_WIDE_WRITE,):
            return (
                OWNERSHIP_DENIED_WORKSPACE_WIDE,
                command,
                assessment.reason,
            )
        if assessment.kind != KNOWN_WRITE_TARGETS:
            return OWNERSHIP_UNCLASSIFIABLE_SHELL, command, assessment.reason
        if not assessment.write_targets:
            return None, "<index-only>", assessment.reason
        for raw_target in assessment.write_targets:
            if raw_target in DEVICE_SINKS:
                continue
            try:
                target = normalize_workspace_path(
                    raw_target, workspace_root=self.manifest.workspace_root
                )
            except OwnershipPathError as error:
                return OWNERSHIP_PATH_ESCAPE, str(raw_target), str(error)
            reason, _, detail = self._authorize_target(
                worker_id,
                target,
                tool_name,
                "shell command",
                allow_directory_creation=assessment.creates_directories,
            )
            if reason is not None:
                return reason, target, detail
        return None, command, assessment.reason

    def _authorize_target(
        self,
        worker_id: str,
        target: str,
        tool_name: str,
        source: str,
        *,
        allow_directory_creation: bool = False,
    ) -> tuple[str | None, str, str]:
        if self.manifest.rejects_for_exclusion(worker_id, target):
            return (
                OWNERSHIP_DENIED_UNDECLARED_PATH,
                target,
                f"{source}: path is explicitly excluded for this worker",
            )
        owner = self.manifest.owner_of(target)
        if owner == worker_id:
            return None, target, "declared owner"
        if owner is not None:
            return (
                OWNERSHIP_DENIED_FOREIGN_PATH,
                target,
                f"{source}: {target} is owned by {owner}",
            )
        if allow_directory_creation and self._contains_declared(worker_id, target):
            return None, target, "directory container of a declared path"
        return (
            OWNERSHIP_DENIED_UNDECLARED_PATH,
            target,
            f"{source}: {target} is not declared for {worker_id}",
        )

    def _contains_declared(self, worker_id: str, directory: str) -> bool:
        prefix = directory.rstrip("/") + "/"
        return any(path.startswith(prefix) for path in self.manifest.declared_paths(worker_id))

    # -- decision construction ------------------------------------------------

    def _decision(
        self,
        request: PolicyRequest,
        *,
        allowed: bool,
        reason_codes: tuple[str, ...] = (),
    ) -> PolicyDecision:
        return PolicyDecision(
            id=uuid.uuid4().hex,
            run_id=request.run_id,
            task_id=request.task_id,
            outcome="allow" if allowed else "deny",
            policy_version=self.manifest.policy_version or OWNERSHIP_POLICY_VERSION,
            reason_codes=reason_codes,
            correlation_id=request.correlation_id,
            causation_id=request.causation_id,
            generation=request.generation,
            attempt=request.attempt,
        )

    # -- host helpers ---------------------------------------------------------

    def evidence_snapshot(self) -> dict[str, Any]:
        return self.evidence.as_dict()


def build_ownership_enforcer(
    manifest: WorkspaceOwnershipManifest,
    *,
    mode: str,
    coverage: str = OWNERSHIP_COVERAGE_PARTIAL,
) -> tuple[PolicyEnforcer | None, WorkspaceOwnershipPolicy | None]:
    """Return the anycode enforcer plus the Atlas adapter, or ``(None, None)`` when off."""

    if mode in (OWNERSHIP_MODE_OFF, ""):
        return None, None
    policy = WorkspaceOwnershipPolicy(manifest, mode=mode, coverage=coverage)
    # ``fail_closed`` keeps provider-side failures denials; the adapter already fails closed for
    # every identity and path it cannot verify, so the wrapper only needs to preserve denies.
    enforcer = PolicyEnforcer(policy, fail_closed=True)
    return enforcer, policy


def _bounded_append(
    bucket: list[dict[str, Any]], item: dict[str, Any], evidence: OwnershipEvidence
) -> None:
    if len(bucket) >= _EVIDENCE_LIMIT:
        evidence.truncated = True
        return
    bucket.append(item)


def _tool_name(request: PolicyRequest) -> str:
    value = request.input.get("tool_name")
    return str(value) if value else "<unknown>"


def _arguments(request: PolicyRequest) -> Mapping[str, Any]:
    value = request.input.get("canonical_arguments")
    return value if isinstance(value, dict) else {}


def _attribute(request: PolicyRequest, key: str) -> str | None:
    attributes = request.context.attributes
    value = attributes.get(key)
    if value is None:
        nested = attributes.get("atlas_worker")
        if isinstance(nested, dict):
            value = nested.get(key)
    return str(value) if value is not None else None


__all__ = [
    "FILE_WRITE_TOOLS",
    "SHELL_TOOLS",
    "OwnershipEvidence",
    "OwnershipFinding",
    "WorkspaceOwnershipPolicy",
    "build_ownership_enforcer",
]
