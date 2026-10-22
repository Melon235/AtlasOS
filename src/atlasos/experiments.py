"""Experiment persistence and reproducibility metadata.

Every run is appended as one JSON line so repeated baselines stay machine-readable without a
database. Metadata is stored beside the run, never injected into a model prompt.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
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
