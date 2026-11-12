"""Pinned upstream checkout: task loading and digests.

The checkout is never vendored into this repository. ``CAB_UPSTREAM`` points at a local copy of the
pinned commit; every load verifies the commit and recomputes the per-task digests so an accidental
upstream drift fails loudly instead of silently changing the benchmark.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PINNED_COMMIT = "d38bd1711aa3926653452458a1be02c1efd12930"
PINNED_RELEASE = "v1.3.1"
PINNED_IMAGE = (
    "acciolyk/accio_bench@sha256:1e9cf5c72a56794175b7d06ece036b92e296e6b7e9e9a7fa244026f6acea3859"
)

#: The five native text-only tasks selected for B0, mapped to their upstream directories.
TASK_PATHS: dict[str, str] = {
    "api-slack-supplier-quote-thread-consolidation": (
        "api/slack/supplier-quote-thread-consolidation"
    ),
    "api-gmail-invoice-hold-mcp": "api/gmail/invoice-hold-mcp",
    "cli-stripe-invoice-reconciliation": "cli/stripe/invoice-reconciliation",
    "cli-xfer-customs-freight-anomaly-trace": "cli/xfer/customs-freight-anomaly-trace",
    "cli-xfer-customs-procurement-control-tower": "cli/xfer/customs-procurement-control-tower",
}


class UpstreamError(RuntimeError):
    """The pinned upstream checkout is missing or does not match the pinned commit."""


@dataclass(frozen=True)
class NativeTask:
    task_id: str
    root: Path
    definition: dict[str, Any]
    prompt: str
    rubric: dict[str, Any]
    image: str
    workspace: Path
    grader_script: Path
    private_dir: Path

    @property
    def agent_timeout_seconds(self) -> int:
        return int(self.definition.get("agent", {}).get("timeout_sec", 1800))

    @property
    def max_actions(self) -> int:
        return int(self.definition.get("agent", {}).get("max_actions", 60))

    @property
    def verifier_timeout_seconds(self) -> int:
        return int(self.definition.get("verifier", {}).get("timeout_sec", 180))

    @property
    def environment(self) -> dict[str, Any]:
        return dict(self.definition.get("environment", {}))

    @property
    def host_published_port(self) -> int | None:
        value = self.environment.get("host_published_port")
        return int(value) if value is not None else None

    @property
    def runtime_mocks(self) -> dict[str, Any]:
        return dict(self.environment.get("runtime_mocks") or {})


def upstream_root(explicit: str | Path | None = None) -> Path:
    root = explicit or os.environ.get("CAB_UPSTREAM")
    if not root:
        raise UpstreamError(
            "CAB_UPSTREAM is not set; point it at a checkout of "
            f"Accio-org/CommerceAgentBench@{PINNED_COMMIT}"
        )
    path = Path(root).resolve()
    if not (path / "datasets_domain_v1").is_dir():
        raise UpstreamError(f"{path} does not look like a Commerce Agent Bench checkout")
    return path


def verify_commit(root: Path) -> str:
    """Refuse to run against anything but the pinned commit.

    Two accepted forms: a Git checkout at the pinned commit, or a content-verified export carrying
    ``UPSTREAM_COMMIT`` plus the per-task digests in ``manifest.json``. A marker alone is not
    trusted: :func:`verify_task_integrity` must agree on the task bytes.
    """

    try:
        commit = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except OSError as error:  # pragma: no cover - only on a broken host
        raise UpstreamError(f"cannot read the upstream commit: {error}") from error
    actual = commit.stdout.strip() if commit.returncode == 0 else ""
    if actual:
        if actual != PINNED_COMMIT:
            raise UpstreamError(
                f"upstream commit {actual} does not match the pinned {PINNED_COMMIT}"
            )
        return actual
    marker = root / "UPSTREAM_COMMIT"
    if not marker.is_file():
        raise UpstreamError(
            f"{root} is neither a Git checkout nor a content-verified export (no UPSTREAM_COMMIT)"
        )
    declared = marker.read_text(encoding="utf-8").strip()
    if declared != PINNED_COMMIT:
        raise UpstreamError(f"export declares commit {declared}, pinned is {PINNED_COMMIT}")
    return declared


def recorded_digests() -> dict[str, dict[str, str]]:
    path = Path(__file__).with_name("manifest.json")
    if not path.is_file():
        raise UpstreamError("manifest.json is missing; the task digests are not pinned")
    payload = json.loads(path.read_text(encoding="utf-8"))
    return dict(payload.get("task_digests") or {})


def verify_task_integrity(task: NativeTask) -> None:
    """Fail loudly when the checkout drifts from the audited task bytes."""

    expected = recorded_digests().get(task.task_id)
    if not expected:
        raise UpstreamError(f"{task.task_id}: no pinned digests in manifest.json")
    actual = task_digests(task).as_dict()
    drift = {
        key: (expected.get(key), value)
        for key, value in actual.items()
        if expected.get(key) != value
    }
    if drift:
        raise UpstreamError(f"{task.task_id}: upstream drift detected: {drift}")


def _digest_files(root: Path, relatives: list[str]) -> str:
    digest = hashlib.sha256()
    for relative in relatives:
        path = root / relative
        digest.update(relative.encode("utf-8"))
        digest.update(hashlib.sha256(path.read_bytes()).hexdigest().encode("ascii"))
    return "sha256:" + digest.hexdigest()


def _list_files(root: Path, *, skip: tuple[str, ...] = ()) -> list[str]:
    if not root.is_dir():
        return []
    out: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root).as_posix()
        if any(relative.startswith(prefix) for prefix in skip):
            continue
        out.append(relative)
    return out


@dataclass(frozen=True)
class TaskDigests:
    fixture_digest: str
    task_definition_digest: str
    grader_digest: str
    private_digest: str

    def as_dict(self) -> dict[str, str]:
        return {
            "fixture_digest": self.fixture_digest,
            "task_definition_digest": self.task_definition_digest,
            "grader_digest": self.grader_digest,
            "private_digest": self.private_digest,
        }


def task_digests(task: NativeTask) -> TaskDigests:
    workspace = _list_files(task.workspace)
    grader = _list_files(task.root / "grader")
    private = _list_files(task.private_dir)
    definition = [
        item
        for item in _list_files(task.root, skip=("private/", "grader/"))
        if item.split("/")[0] in {"task.md", "task.toml", "rubric.json"}
    ]
    return TaskDigests(
        fixture_digest=_digest_files(task.workspace, workspace) if workspace else "sha256:empty",
        task_definition_digest=_digest_files(task.root, definition),
        grader_digest=_digest_files(task.root / "grader", grader) if grader else "sha256:empty",
        private_digest=_digest_files(task.private_dir, private) if private else "sha256:empty",
    )


def load_task(task_id: str, *, root: Path | None = None) -> NativeTask:
    base = root or upstream_root()
    relative = TASK_PATHS.get(task_id)
    if relative is None:
        raise UpstreamError(f"unknown Commerce Agent Bench task id: {task_id}")
    task_root = base / "datasets_domain_v1" / relative
    if not task_root.is_dir():
        raise UpstreamError(f"task directory is missing from the checkout: {task_root}")
    definition = tomllib.loads((task_root / "task.toml").read_text(encoding="utf-8"))
    rubric = json.loads((task_root / "rubric.json").read_text(encoding="utf-8"))
    images = definition.get("environment", {}).get("base_images") or []
    if len(images) != 1:
        raise UpstreamError(f"{task_id}: expected exactly one pinned base image, got {images}")
    if images[0] != PINNED_IMAGE:
        raise UpstreamError(f"{task_id}: image {images[0]} is not the pinned image")
    prompt = (task_root / "task.md").read_text(encoding="utf-8")
    task = NativeTask(
        task_id=task_id,
        root=task_root,
        definition=definition,
        prompt=prompt,
        rubric=rubric,
        image=images[0],
        workspace=task_root / "workspace",
        grader_script=task_root / "grader" / "run.sh",
        private_dir=task_root / "private",
    )
    verify_task_integrity(task)
    return task


def text_only_collection(root: Path | None = None) -> list[str]:
    base = root or upstream_root()
    payload = json.loads(
        (base / "datasets_domain_v1" / "domain_v1_text_only.collection.json").read_text(
            encoding="utf-8"
        )
    )
    return list(payload.get("task_ids") or [])


def manifest_payload() -> dict[str, Any]:
    """Stable description of what B0 pins, written next to the evidence."""

    return {
        "upstream_repository": "https://github.com/Accio-org/CommerceAgentBench",
        "upstream_commit": PINNED_COMMIT,
        "upstream_release": PINNED_RELEASE,
        "container_image": PINNED_IMAGE,
        "task_collection": "domain_v1_text_only",
        "tasks": {task_id: TASK_PATHS[task_id] for task_id in TASK_PATHS},
        "license": {
            "code": "Apache-2.0",
            "task_data": "CC BY 4.0 (LICENSE-DATA)",
        },
    }


__all__ = [
    "PINNED_COMMIT",
    "PINNED_IMAGE",
    "PINNED_RELEASE",
    "TASK_PATHS",
    "NativeTask",
    "TaskDigests",
    "UpstreamError",
    "load_task",
    "manifest_payload",
    "task_digests",
    "text_only_collection",
    "upstream_root",
    "verify_commit",
]
