"""Fixture materialization, digests, and workspace comparison for evaluation."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path, PurePosixPath

from experiments.evaluation.models import BenchmarkTask

_IGNORED_PARTS = frozenset(
    {".git", "__pycache__", ".pytest_cache", ".venv", "node_modules", "dist", "build"}
)


def _validate_relative(path: str) -> str:
    candidate = PurePosixPath(path.replace("\\", "/"))
    if candidate.is_absolute() or ".." in candidate.parts or not candidate.parts:
        raise ValueError(f"fixture path must be a safe relative path: {path!r}")
    return candidate.as_posix()


def _write_files(root: Path, files: dict[str, str]) -> None:
    for relative, content in sorted(files.items()):
        safe = _validate_relative(relative)
        target = root / safe
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")


def materialize_fixture(task: BenchmarkTask, destination: Path) -> Path:
    """Create the visible fixture. Hidden oracle files are never written here."""

    destination.mkdir(parents=True, exist_ok=True)
    _write_files(destination, task.visible_files)
    return destination


def materialize_oracle(task: BenchmarkTask, destination: Path) -> Path:
    """Create the independent oracle workspace from the exported agent workspace."""

    destination.mkdir(parents=True, exist_ok=True)
    _write_files(destination, task.hidden_files)
    return destination


def stage_git_repo(destination: Path, *, label: str = "Atlas evaluation fixture") -> str:
    commands = (
        ["git", "init", "-q"],
        ["git", "config", "core.autocrlf", "false"],
        ["git", "config", "core.safecrlf", "false"],
        ["git", "add", "-A"],
        [
            "git",
            "-c",
            "user.email=eval@example.test",
            "-c",
            "user.name=AtlasEval",
            "commit",
            "-qm",
            label,
        ],
        ["git", "rev-parse", "HEAD"],
    )
    for command in commands:
        result = subprocess.run(
            command,
            cwd=destination,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"cannot stage fixture repository: {result.stderr.strip() or result.stdout.strip()}"
            )
    return result.stdout.strip()


def workspace_hashes(root: Path) -> dict[str, str]:
    hashes: dict[str, str] = {}
    if not root.is_dir():
        return hashes
    for item in sorted(root.rglob("*")):
        if not item.is_file() or _IGNORED_PARTS.intersection(item.relative_to(root).parts):
            continue
        hashes[item.relative_to(root).as_posix()] = hashlib.sha256(item.read_bytes()).hexdigest()
    return hashes


def workspace_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for relative, value in sorted(workspace_hashes(root).items()):
        digest.update(relative.encode("utf-8"))
        digest.update(value.encode("ascii"))
    return digest.hexdigest()


def copy_workspace(source: Path, destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(
        source,
        destination,
        ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", ".git"),
    )


def changes_against_baseline(
    baseline: dict[str, str], current: dict[str, str]
) -> dict[str, object]:
    changed = sorted(
        path for path in baseline.keys() & current.keys() if baseline[path] != current[path]
    )
    added = sorted(current.keys() - baseline.keys())
    removed = sorted(baseline.keys() - current.keys())
    return {"changed": changed, "added": added, "removed": removed}


def path_matches(path: str, patterns: tuple[str, ...]) -> bool:
    """Match with pathlib semantics using ``**`` globs over POSIX relative paths."""

    if not patterns:
        return False
    return any(PurePosixPath(path).match(pattern) for pattern in patterns)


def host_fixture_unchanged(before: dict[str, str], after: dict[str, str]) -> bool:
    return before == after


def ensure_python_import_path() -> str:
    root = Path(__file__).resolve().parents[2]
    return os.pathsep.join([str(root / "src"), str(root)])
