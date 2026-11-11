"""Stage R1-1 workspace ownership model.

Ownership is an execution boundary owned by the Host. A worker may read every file in the shared
workspace, but may only write the paths the Host assigned to it. The manifest is constructed from
trusted task metadata; model output, planner claims, and tool input can never add an authorization.

This module holds data only. It performs no I/O and imports no runtime component.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from atlasos.trajectory.models import canonical_digest

OWNERSHIP_MODE_OFF = "off"
OWNERSHIP_MODE_AUDIT = "audit"
OWNERSHIP_MODE_ENFORCE = "enforce"
OwnershipMode = Literal["off", "audit", "enforce"]
OWNERSHIP_MODES: tuple[str, ...] = (
    OWNERSHIP_MODE_OFF,
    OWNERSHIP_MODE_AUDIT,
    OWNERSHIP_MODE_ENFORCE,
)

OWNERSHIP_POLICY_VERSION = "r1.1.0"

#: R1-1 does not claim OS-level isolation. Shell classification is textual, so a command this
#: module cannot prove read-only is refused in ``enforce`` rather than trusted.
OWNERSHIP_COVERAGE_PARTIAL = "PARTIAL"
OWNERSHIP_COVERAGE_FULL = "FULL"

ProvenanceKind = Literal["trusted_host_declaration", "verified_task_metadata"]
PROVENANCE_HOST = "trusted_host_declaration"
PROVENANCE_VERIFIED_TASK = "verified_task_metadata"

OWNERSHIP_DENIED_FOREIGN_PATH = "OWNERSHIP_DENIED_FOREIGN_PATH"
OWNERSHIP_DENIED_UNDECLARED_PATH = "OWNERSHIP_DENIED_UNDECLARED_PATH"
OWNERSHIP_DENIED_WORKSPACE_WIDE = "OWNERSHIP_DENIED_WORKSPACE_WIDE"
OWNERSHIP_UNKNOWN_WORKER = "OWNERSHIP_UNKNOWN_WORKER"
OWNERSHIP_INVALID_MANIFEST = "OWNERSHIP_INVALID_MANIFEST"
OWNERSHIP_PATH_ESCAPE = "OWNERSHIP_PATH_ESCAPE"
OWNERSHIP_SYMLINK_UNVERIFIED = "OWNERSHIP_SYMLINK_UNVERIFIED"
OWNERSHIP_UNCLASSIFIABLE_SHELL = "OWNERSHIP_UNCLASSIFIABLE_SHELL"

OWNERSHIP_REASON_CODES: tuple[str, ...] = (
    OWNERSHIP_DENIED_FOREIGN_PATH,
    OWNERSHIP_DENIED_UNDECLARED_PATH,
    OWNERSHIP_DENIED_WORKSPACE_WIDE,
    OWNERSHIP_UNKNOWN_WORKER,
    OWNERSHIP_INVALID_MANIFEST,
    OWNERSHIP_PATH_ESCAPE,
    OWNERSHIP_SYMLINK_UNVERIFIED,
    OWNERSHIP_UNCLASSIFIABLE_SHELL,
)


@dataclass(frozen=True)
class OwnedPathDeclaration:
    """One trusted statement that ``worker_id`` may write ``paths``.

    ``provenance`` records *why* the Host believes the claim:

    * ``trusted_host_declaration`` - an operator-supplied configuration.
    * ``verified_task_metadata`` - a benchmark/plan field the Host itself authored.

    A planner-generated path list is neither, and must be rejected by the builder.
    """

    worker_id: str
    task_id: str
    paths: tuple[str, ...]
    excluded: tuple[str, ...] = ()
    provenance: ProvenanceKind = PROVENANCE_VERIFIED_TASK


@dataclass(frozen=True)
class WorkspaceOwnershipManifest:
    """Immutable Host-owned authorization table for one run."""

    run_id: str
    workspace_root: str
    policy_version: str
    worker_ownership: Mapping[str, tuple[str, ...]]
    task_by_worker: Mapping[str, str]
    excluded_by_worker: Mapping[str, tuple[str, ...]]
    global_paths: tuple[str, ...]
    provenance: Mapping[str, str]
    digest: str

    def declared_paths(self, worker_id: str) -> tuple[str, ...]:
        return self.worker_ownership.get(worker_id, ())

    def task_of(self, worker_id: str) -> str | None:
        return self.task_by_worker.get(worker_id)

    def excluded_paths(self, worker_id: str) -> tuple[str, ...]:
        return self.excluded_by_worker.get(worker_id, ())

    def workers(self) -> tuple[str, ...]:
        return tuple(sorted(self.worker_ownership))

    def owner_of(self, target: str) -> str | None:
        """Return the single worker that declared ``target``, or None.

        Two workers declaring the same path is a Host configuration error, and the builder
        refuses to produce such a manifest.
        """

        for worker_id in self.workers():
            for declared in self.declared_paths(worker_id):
                if path_is_within(declared, target):
                    return worker_id
        return None

    def rejects_for_exclusion(self, worker_id: str, target: str) -> bool:
        return any(path_is_within(entry, target) for entry in self.excluded_paths(worker_id))

    def compute_digest(self) -> str:
        return canonical_digest(
            {
                "run_id": self.run_id,
                "workspace_root": self.workspace_root,
                "policy_version": self.policy_version,
                "ownership": {
                    worker: list(paths) for worker, paths in sorted(self.worker_ownership.items())
                },
                "tasks": {worker: task for worker, task in sorted(self.task_by_worker.items())},
                "excluded": {
                    worker: list(paths) for worker, paths in sorted(self.excluded_by_worker.items())
                },
                "global_paths": list(self.global_paths),
                "provenance": {worker: kind for worker, kind in sorted(self.provenance.items())},
            }
        )

    def is_valid(self) -> bool:
        """True only when the manifest still matches its own digest."""

        return bool(self.digest) and self.compute_digest() == self.digest


def path_is_within(declared: str, target: str) -> bool:
    """Match ``target`` against a declared file or directory pattern.

    A declaration normalized by :func:`atlasos.ownership.paths.normalize_declared_path` either
    names one file (``ledger/money.py``) or a directory with a trailing slash (``ledger/``).
    """

    if not declared or not target:
        return False
    if declared.endswith("/"):
        return target == declared[:-1] or target.startswith(declared)
    return target == declared


__all__ = [
    "OWNERSHIP_COVERAGE_FULL",
    "OWNERSHIP_COVERAGE_PARTIAL",
    "OWNERSHIP_DENIED_FOREIGN_PATH",
    "OWNERSHIP_DENIED_UNDECLARED_PATH",
    "OWNERSHIP_DENIED_WORKSPACE_WIDE",
    "OWNERSHIP_INVALID_MANIFEST",
    "OWNERSHIP_MODE_AUDIT",
    "OWNERSHIP_MODE_ENFORCE",
    "OWNERSHIP_MODE_OFF",
    "OWNERSHIP_MODES",
    "OWNERSHIP_PATH_ESCAPE",
    "OWNERSHIP_POLICY_VERSION",
    "OWNERSHIP_REASON_CODES",
    "OWNERSHIP_SYMLINK_UNVERIFIED",
    "OWNERSHIP_UNCLASSIFIABLE_SHELL",
    "OWNERSHIP_UNKNOWN_WORKER",
    "PROVENANCE_HOST",
    "PROVENANCE_VERIFIED_TASK",
    "OwnedPathDeclaration",
    "OwnershipMode",
    "ProvenanceKind",
    "WorkspaceOwnershipManifest",
    "path_is_within",
]
