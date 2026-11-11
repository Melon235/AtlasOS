"""Filesystem helpers shared by the verification checks.

Every path coming from a spec is untrusted data: it is normalized, refused when absolute or when it
escapes the snapshot, and never followed through a symlink.
"""

from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath


class UnsafeArtifactPath(ValueError):
    """The spec named a path that cannot be resolved inside the snapshot."""


def safe_relative(raw: str) -> str:
    candidate = PurePosixPath(str(raw).replace("\\", "/"))
    if candidate.is_absolute() or ".." in candidate.parts or not candidate.parts:
        raise UnsafeArtifactPath(f"artifact path must be a safe relative path: {raw!r}")
    return candidate.as_posix()


def resolve(snapshot: Path, raw: str) -> Path:
    return snapshot.joinpath(*safe_relative(raw).split("/"))


def read_text(snapshot: Path, raw: str) -> str:
    """Read an artifact as UTF-8 text; raises ``FileNotFoundError``/``UnicodeDecodeError``."""

    path = resolve(snapshot, raw)
    if path.is_symlink():
        raise UnsafeArtifactPath(f"artifact is a symlink: {raw!r}")
    if not path.is_file():
        raise FileNotFoundError(raw)
    return path.read_text(encoding="utf-8")


def file_digest(snapshot: Path, raw: str) -> str:
    path = resolve(snapshot, raw)
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot_digest(snapshot: Path) -> str:
    """Stable digest over the whole exported workspace (paths + contents)."""

    digest = hashlib.sha256()
    if not snapshot.is_dir():
        return ""
    for item in sorted(snapshot.rglob("*")):
        if not item.is_file() or item.is_symlink():
            continue
        relative = item.relative_to(snapshot).as_posix()
        if "__pycache__" in relative or relative.startswith(".git/"):
            continue
        digest.update(relative.encode("utf-8"))
        digest.update(hashlib.sha256(item.read_bytes()).hexdigest().encode("ascii"))
    return "sha256:" + digest.hexdigest()
