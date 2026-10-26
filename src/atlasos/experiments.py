"""Experiment persistence and reproducibility metadata.

Every run is appended as one JSON line so repeated baselines stay machine-readable without a
database. Metadata is stored beside the run, never injected into a model prompt.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from importlib import metadata
from pathlib import Path
from typing import Any

from atlasos.config import AtlasConfig


def configuration_hash(config: AtlasConfig) -> str:
    """Stable hash of the effective experiment configuration."""

    payload = config.model_dump(mode="json")
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def git_commit(repo_root: Path, *, timeout_s: float = 5.0) -> str | None:
    """Current HEAD commit, or ``None`` outside a repository / without git."""

    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    commit = completed.stdout.strip()
    return commit or None


def git_dirty(repo_root: Path, *, timeout_s: float = 5.0) -> bool:
    """True when the worktree has uncommitted changes or its state cannot be determined."""

    try:
        completed = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return True
    if completed.returncode != 0:
        return True
    return bool(completed.stdout.strip())


def package_version(distribution: str) -> str | None:
    """Installed version of a distribution, or ``None`` when it is absent."""

    try:
        return metadata.version(distribution)
    except metadata.PackageNotFoundError:
        return None


class ExperimentStore:
    """Append-only JSONL store for normalized run results."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def append(self, record: Any) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = record.model_dump_json()
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line + "\n")

    def read_all(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        rows: list[dict[str, Any]] = []
        with self.path.open(encoding="utf-8") as handle:
            for line in handle:
                stripped = line.strip()
                if stripped:
                    rows.append(json.loads(stripped))
        return rows


def semantic_config_hash(config: AtlasConfig) -> str:
    """Hash only settings that change execution semantics.

    Operational fields (run id, resume marker, fault injection, paths, logging, secrets,
    timestamps) are intentionally absent so resuming cannot invalidate its own config.
    """

    payload = {
        "provider": {
            "name": config.provider.name,
            "model": config.provider.model,
            "base_url": config.provider.base_url,
        },
        "context": {
            "policy": config.context.policy,
            "renderer": config.context.renderer,
            "transform": config.context.transform.name,
        },
        "scheduler": config.scheduler.policy,
        "tools": {
            "mode": config.tools.mode,
            "search_backend": config.tools.search_backend,
            "embedding_model": config.tools.embedding_model,
            "max_results": config.tools.max_results,
        },
        "placement": {
            "policy": config.placement.policy,
            "weights": {
                "locality": config.placement.weights.locality,
                "load": config.placement.weights.load,
            },
        },
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def placed_assignment_hash(workflow) -> str:
    """Canonical task_id -> agent_id identity for deterministic pre-placement."""

    mapping = {task.task_id: task.assignee for task in workflow.tasks}
    canonical = json.dumps(dict(sorted(mapping.items())), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
