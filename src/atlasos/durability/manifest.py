"""Atlas durable-session manifest (sidecar metadata only).

AnyCode checkpoints own execution truth. This manifest owns Atlas identity and semantic
configuration so a resume can be validated before touching upstream state.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from atlasos.workflow import AtlasWorkflow

SCHEMA_VERSION = 2


class ManifestMismatch(RuntimeError):
    """Raised when a resume invocation does not match the frozen session."""


class AtlasSessionManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = SCHEMA_VERSION
    run_id: str
    created_at: datetime

    git_commit: str | None
    git_dirty: bool

    # Durable session identity: the workflow text, its semantic configuration, and the
    # deterministic placed assignment. Operational settings (paths, logging, fault injection,
    # timestamps, secrets) are deliberately absent.
    workflow_hash: str
    workflow_input: str = ""
    semantic_config_hash: str
    placed_assignment_hash: str

    anycode_version: str | None = None
    contextpilot_version: str | None = None
    model: str

    placement_policy: str
    placement_weights: dict[str, float]
    context_policy: str
    renderer_policy: str
    context_transform: str

    checkpoint_location: str
    status: str = "running"


def workflow_hash(workflow: AtlasWorkflow) -> str:
    payload = [
        {
            "task_id": task.task_id,
            "title": task.title,
            "description": task.description,
            "assignee": task.assignee,
            "depends_on": list(task.depends_on),
            "required_capabilities": sorted(task.required_capabilities),
            "required_context": [
                {
                    "logical_key": requirement.logical_key,
                    "source_task_id": requirement.source_task_id,
                    "required_version": requirement.required_version,
                }
                for requirement in task.required_context
            ],
        }
        for task in sorted(workflow.tasks, key=lambda task: task.task_id)
    ]
    agents = sorted(agent.name for agent in workflow.agents)
    canonical = json.dumps(
        {"tasks": payload, "agents": agents, "input": workflow.input},
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def write_manifest(path: Path, manifest: AtlasSessionManifest) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(manifest.model_dump_json(), encoding="utf-8")
    os.replace(temporary, path)


def read_manifest(path: Path) -> AtlasSessionManifest:
    manifest = AtlasSessionManifest.model_validate_json(path.read_text(encoding="utf-8"))
    if manifest.schema_version != SCHEMA_VERSION:
        raise ManifestMismatch(
            f"unsupported manifest schema {manifest.schema_version}; expected {SCHEMA_VERSION}"
        )
    return manifest


def verify_compatible(
    manifest: AtlasSessionManifest,
    *,
    workflow: AtlasWorkflow,
    semantic_config_hash: str,
    placed_assignment_hash: str,
    placement_policy: str,
    context_policy: str,
    renderer_policy: str,
    context_transform: str,
) -> None:
    """Validate everything a resume must preserve before upstream state is touched."""

    checks = {
        "workflow_hash": (manifest.workflow_hash, workflow_hash(workflow)),
        "semantic_config_hash": (manifest.semantic_config_hash, semantic_config_hash),
        "placed_assignment_hash": (manifest.placed_assignment_hash, placed_assignment_hash),
        "placement_policy": (manifest.placement_policy, placement_policy),
        "context_policy": (manifest.context_policy, context_policy),
        "renderer_policy": (manifest.renderer_policy, renderer_policy),
        "context_transform": (manifest.context_transform, context_transform),
    }
    mismatched = [
        f"{name}: stored={stored!r} current={current!r}"
        for name, (stored, current) in checks.items()
        if stored != current
    ]
    if mismatched:
        raise ManifestMismatch("resume rejected: " + "; ".join(mismatched))


def manifest_payload(manifest: AtlasSessionManifest) -> dict[str, Any]:
    return manifest.model_dump(mode="json")


def utc_now() -> datetime:
    return datetime.now(UTC)
