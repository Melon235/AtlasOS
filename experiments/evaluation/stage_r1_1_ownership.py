"""Stage R1-1 evaluation bridge: benchmark task metadata -> trusted ownership declarations.

The benchmark dataset is authored by the Host, so a worker step prompt and a frozen contract are
``VERIFIED_TASK_METADATA``. A planner-produced path list would not be, and
:func:`atlasos.ownership.build_manifest` rejects it.

Nothing here changes the fixture, the oracle, the worker prompt, the DAG, the budget or the
scheduler. It only states, before the run, which files each worker is allowed to write.
"""

from __future__ import annotations

from experiments.evaluation.models import BenchmarkTask

from atlasos.ownership import (
    OWNERSHIP_COVERAGE_PARTIAL,
    OwnedPathDeclaration,
    WorkspaceOwnershipManifest,
    build_manifest,
)
from atlasos.workers.routing import extract_declared_paths

OWNERSHIP_EVIDENCE_VERSION = "r1.1-ownership-v1"

#: R1-1 does not claim OS-level isolation; keep the honest coverage label in one place.
OWNERSHIP_COVERAGE = OWNERSHIP_COVERAGE_PARTIAL


def worker_id_for(run_id: str, task_id: str) -> str:
    """The single convention the runner uses for worker identity."""

    return f"{run_id}-{task_id}"


def declarations_for_task(task: BenchmarkTask, *, run_id: str) -> tuple[OwnedPathDeclaration, ...]:
    """Derive one declaration per worker from benchmark metadata only."""

    declarations: list[OwnedPathDeclaration] = []
    for step in task.workers:
        # ``frozen_contracts`` names read-only interface files a worker must respect, not files it
        # may write. Treating a contract as an owned path would grant both sides of a frozen
        # interface write access, which is exactly the authorization the stage forbids.
        frozen = tuple(task.frozen_contracts.get(step.task_id, ()))
        owned, excluded = extract_declared_paths(step.prompt)
        declarations.append(
            OwnedPathDeclaration(
                worker_id=worker_id_for(run_id, step.task_id),
                task_id=worker_id_for(run_id, step.task_id),
                paths=tuple(item for item in owned if item not in frozen),
                excluded=(*excluded, *frozen, *task.forbidden_paths),
                provenance="verified_task_metadata",
            )
        )
    return tuple(declarations)


def manifest_for_task(
    task: BenchmarkTask, *, run_id: str, workspace_root: str
) -> WorkspaceOwnershipManifest:
    return build_manifest(
        run_id=run_id,
        workspace_root=workspace_root,
        declarations=declarations_for_task(task, run_id=run_id),
    )


def manifest_public_summary(manifest: WorkspaceOwnershipManifest) -> dict[str, object]:
    """Public metadata only: no workspace paths beyond the declared relative files."""

    return {
        "run_id": manifest.run_id,
        "policy_version": manifest.policy_version,
        "digest": manifest.digest,
        "coverage": OWNERSHIP_COVERAGE,
        "workers": {worker: list(manifest.declared_paths(worker)) for worker in manifest.workers()},
        "task_by_worker": {worker: manifest.task_of(worker) for worker in manifest.workers()},
    }


__all__ = [
    "OWNERSHIP_COVERAGE",
    "OWNERSHIP_EVIDENCE_VERSION",
    "declarations_for_task",
    "manifest_for_task",
    "manifest_public_summary",
    "worker_id_for",
]
