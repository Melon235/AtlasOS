"""Trusted manifest construction.

The builder accepts *declarations* that a Host already considers trustworthy and refuses anything
else. It never reads model output, never infers ownership from a task name, and never widens a
declaration when two workers disagree.

Three provenance levels exist in AtlasRuntime:

``TRUSTED_HOST_DECLARATION``
    Operator configuration. Highest level.
``VERIFIED_TASK_METADATA``
    A benchmark or control-plane field the Host authored directly.
``PLANNER_CLAIM``
    Model- or planner-produced text. Explicitly rejected: routing a task to a worker is not a
    grant to write a file.
"""

from __future__ import annotations

from collections.abc import Sequence

from atlasos.ownership.models import (
    OWNERSHIP_POLICY_VERSION,
    PROVENANCE_HOST,
    PROVENANCE_VERIFIED_TASK,
    OwnedPathDeclaration,
    WorkspaceOwnershipManifest,
)
from atlasos.ownership.paths import OwnershipPathError, normalize_declared_path

TRUSTED_PROVENANCE = frozenset({PROVENANCE_HOST, PROVENANCE_VERIFIED_TASK})


class OwnershipManifestError(ValueError):
    """A declaration set the Host must fix rather than silently weaken."""


def build_manifest(
    *,
    run_id: str,
    workspace_root: str,
    declarations: Sequence[OwnedPathDeclaration],
    global_paths: Sequence[str] = (),
    policy_version: str = OWNERSHIP_POLICY_VERSION,
) -> WorkspaceOwnershipManifest:
    """Build the immutable authorization table for one run."""

    if not run_id:
        raise OwnershipManifestError("run_id is required")
    if not workspace_root:
        raise OwnershipManifestError("workspace_root is required")

    ownership: dict[str, list[str]] = {}
    tasks: dict[str, str] = {}
    excluded: dict[str, list[str]] = {}
    provenance: dict[str, str] = {}
    seen_workers: set[str] = set()

    for declaration in declarations:
        worker_id = declaration.worker_id.strip()
        if not worker_id:
            raise OwnershipManifestError("declaration without a worker id")
        if declaration.provenance not in TRUSTED_PROVENANCE:
            raise OwnershipManifestError(
                f"worker {worker_id!r}: untrusted provenance {declaration.provenance!r} cannot "
                "grant write ownership"
            )
        if worker_id in seen_workers and tasks.get(worker_id) != declaration.task_id:
            raise OwnershipManifestError(f"worker {worker_id!r} is declared for more than one task")
        seen_workers.add(worker_id)
        tasks[worker_id] = declaration.task_id
        provenance[worker_id] = declaration.provenance
        bucket = ownership.setdefault(worker_id, [])
        for raw in declaration.paths:
            normalized = _normalize(raw, workspace_root, worker_id)
            if normalized not in bucket:
                bucket.append(normalized)
        excluded_bucket = excluded.setdefault(worker_id, [])
        for raw in declaration.excluded:
            normalized = _normalize(raw, workspace_root, worker_id)
            if normalized not in excluded_bucket:
                excluded_bucket.append(normalized)

    _reject_conflicts(ownership)

    manifest = WorkspaceOwnershipManifest(
        run_id=run_id,
        workspace_root=workspace_root.rstrip("/") or "/",
        policy_version=policy_version,
        worker_ownership={worker: tuple(sorted(paths)) for worker, paths in ownership.items()},
        task_by_worker={worker: tasks[worker] for worker in ownership},
        excluded_by_worker={worker: tuple(sorted(paths)) for worker, paths in excluded.items()},
        global_paths=tuple(
            sorted(
                _normalize(raw, workspace_root, "<global>") for raw in global_paths if raw.strip()
            )
        ),
        provenance={worker: provenance[worker] for worker in ownership},
        digest="",
    )
    return WorkspaceOwnershipManifest(
        run_id=manifest.run_id,
        workspace_root=manifest.workspace_root,
        policy_version=manifest.policy_version,
        worker_ownership=manifest.worker_ownership,
        task_by_worker=manifest.task_by_worker,
        excluded_by_worker=manifest.excluded_by_worker,
        global_paths=manifest.global_paths,
        provenance=manifest.provenance,
        digest=manifest.compute_digest(),
    )


def _normalize(raw: str, workspace_root: str, worker_id: str) -> str:
    try:
        return normalize_declared_path(raw, workspace_root=workspace_root)
    except OwnershipPathError as error:
        raise OwnershipManifestError(f"worker {worker_id!r}: {error}") from error


def _reject_conflicts(ownership: dict[str, list[str]]) -> None:
    claimed: dict[str, str] = {}
    for worker_id, paths in ownership.items():
        for path in paths:
            previous = claimed.get(path)
            if previous is not None and previous != worker_id:
                raise OwnershipManifestError(
                    f"path {path!r} is declared by both {previous!r} and {worker_id!r}"
                )
            claimed[path] = worker_id


__all__ = [
    "OwnershipManifestError",
    "TRUSTED_PROVENANCE",
    "build_manifest",
]
