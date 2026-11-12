"""Host-owned public input manifest for a native task workspace.

The agent may only read files the Host froze *before* the environment started. Runtime files that
native services create under ``outputs/`` (mock state, mock audit, service logs) are not public
inputs, even though they sit inside the same workspace tree.

Path ownership is decided by canonical resolution plus a set membership test, never by string
prefixes: ``..``, absolute paths, drive letters, UNC paths, symlinks (including symlinked parents),
and case aliases all fail closed.
"""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


class PublicInputError(RuntimeError):
    """A read was refused because the path is not a registered public input."""


def canonical_relative(raw: str) -> str:
    """Normalise a caller-supplied path to a POSIX-style relative form, or refuse it."""

    text = str(raw or "").strip().replace("\\", "/")
    if not text:
        raise PublicInputError("path is empty")
    if text.startswith("/") or text.startswith("//"):
        raise PublicInputError(f"absolute paths are not readable: {raw!r}")
    if len(text) >= 2 and text[1] == ":":
        raise PublicInputError(f"drive-letter paths are not readable: {raw!r}")
    parsed = PurePosixPath(text)
    if any(part in {"..", ""} for part in parsed.parts):
        raise PublicInputError(f"path traversal is not allowed: {raw!r}")
    normalised = "/".join(part for part in parsed.parts if part != ".")
    if not normalised:
        raise PublicInputError("path is empty")
    return normalised.casefold()


def _reject_link_components(root: Path, relative: str) -> Path:
    """Walk each component with lstat; any symlink or reparse point is refused."""

    current = root
    for part in PurePosixPath(relative).parts:
        current = current / part
        try:
            info = os.lstat(current)
        except OSError as error:
            raise PublicInputError(f"cannot inspect {relative!r}: {error}") from error
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise PublicInputError(f"symlinked paths are not readable: {relative!r}")
    return current


def _ownership_check(workspace_root: Path, relative: str) -> Path:
    root = workspace_root.resolve(strict=True)
    candidate = _reject_link_components(root, relative)
    resolved = candidate.resolve(strict=False)
    if os.path.commonpath([str(root), str(resolved)]) != str(root):
        raise PublicInputError(f"path escapes the workspace: {relative!r}")
    return candidate


@dataclass(frozen=True)
class PublicInputManifest:
    """Frozen set of readable public inputs for one run."""

    workspace_root: Path
    digest: str
    #: casefolded relative paths
    entries: frozenset[str]
    #: relpath -> sha256 of the source bytes
    hashes: dict[str, str]

    @classmethod
    def freeze(cls, workspace_root: Path, *, source_root: Path) -> PublicInputManifest:
        """Freeze from the *pristine* task workspace, before the run stages or services write."""

        entries: set[str] = set()
        hashes: dict[str, str] = {}
        for path in sorted(source_root.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(source_root).as_posix()
            entries.add(relative.casefold())
            hashes[relative.casefold()] = hashlib.sha256(path.read_bytes()).hexdigest()
        payload = "".join(f"{key}:{hashes[key]}" for key in sorted(hashes))
        digest = "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()
        return cls(
            workspace_root=workspace_root,
            digest=digest,
            entries=frozenset(entries),
            hashes=hashes,
        )

    def resolve(self, raw: str) -> Path:
        """Return the readable path, or raise :class:`PublicInputError`."""

        relative = canonical_relative(raw)
        if relative not in self.entries:
            raise PublicInputError(
                f"{raw!r} is not a registered public input; registered inputs: {len(self.entries)}"
            )
        target = _ownership_check(self.workspace_root, relative)
        if not target.is_file():
            raise PublicInputError(f"registered input is missing on disk: {raw!r}")
        expected = self.hashes.get(relative)
        if expected is not None:
            actual = hashlib.sha256(target.read_bytes()).hexdigest()
            if actual != expected:
                raise PublicInputError(f"registered input changed since it was frozen: {raw!r}")
        return target

    def as_dict(self) -> dict[str, object]:
        return {
            "digest": self.digest,
            "entries": len(self.entries),
            "paths": sorted(self.entries),
        }


__all__ = ["PublicInputError", "PublicInputManifest", "canonical_relative"]
