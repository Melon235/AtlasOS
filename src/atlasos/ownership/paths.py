"""Path normalization for the ownership boundary.

Every write target is reduced to one canonical workspace-relative POSIX path *before* it is
compared with the manifest. String prefix matching on unnormalized input is never used, because
``ledger/money.py``, ``./ledger/money.py``, ``ledger/../ledger/money.py`` and
``/workspace/project/ledger/money.py`` must all resolve to the same authorization decision.

Known limitation: the sandbox provider exposes no lstat, so a symlink created *inside* the
workspace cannot be resolved here. R1-1 therefore reports ``OWNERSHIP_ENFORCEMENT_COVERAGE =
PARTIAL`` and closes the practical path by refusing shell commands it cannot classify.
"""

from __future__ import annotations

import posixpath

from atlasos.ownership.models import OWNERSHIP_PATH_ESCAPE


class OwnershipPathError(ValueError):
    """A write target that cannot be safely normalized inside the workspace."""

    def __init__(self, message: str, *, reason_code: str = OWNERSHIP_PATH_ESCAPE) -> None:
        super().__init__(message)
        self.reason_code = reason_code


def normalize_workspace_path(raw: object, *, workspace_root: str) -> str:
    """Return the canonical workspace-relative path of ``raw``.

    Rejects empty input, NUL bytes, Windows separators, absolute paths outside the workspace, and
    every ``..`` escape. Identity of the workspace root itself is rejected: an ownership check
    authorizes files, not the workspace.
    """

    if not isinstance(raw, str):
        raise OwnershipPathError(f"path must be a string, got {type(raw).__name__}")
    text = raw.strip()
    if not text:
        raise OwnershipPathError("path is empty")
    if "\x00" in text:
        raise OwnershipPathError("path contains a NUL byte")
    if "\\" in text:
        raise OwnershipPathError(f'backslash path alias "{raw}" is not accepted')
    if len(text) > 1 and text[1] == ":" and text[0].isalpha():
        raise OwnershipPathError(f'drive-letter path "{raw}" is not a workspace path')

    root = posixpath.normpath(workspace_root.rstrip("/") or "/")
    if text.startswith("/"):
        absolute = posixpath.normpath(text)
        if absolute == root:
            raise OwnershipPathError(f'"{raw}" addresses the workspace root, not a file')
        prefix = root + "/"
        if not absolute.startswith(prefix):
            raise OwnershipPathError(f'absolute path "{raw}" is outside {root}')
        text = absolute[len(prefix) :]

    normalized = posixpath.normpath(text)
    if normalized in ("", "."):
        raise OwnershipPathError(f'"{raw}" addresses the workspace root, not a file')
    if normalized == ".." or normalized.startswith("../"):
        raise OwnershipPathError(f'path "{raw}" escapes the workspace')
    return normalized


def normalize_declared_path(raw: str, *, workspace_root: str) -> str:
    """Normalize one manifest declaration, preserving directory semantics.

    ``ledger/**``, ``ledger/`` and ``ledger/*`` all mean "the ledger directory"; a declaration
    without a wildcard or trailing slash names exactly one file.
    """

    text = raw.strip()
    directory = False
    if text.endswith("/**"):
        text = text[: -len("/**")]
        directory = True
    elif text.endswith("/*"):
        text = text[: -len("/*")]
        directory = True
    elif text.endswith("/"):
        directory = True
    normalized = normalize_workspace_path(text, workspace_root=workspace_root)
    return normalized + "/" if directory else normalized


class WorkspacePathSet:
    """Small helper for ancestor checks used by directory-creating commands."""

    @staticmethod
    def is_ancestor(container: str, target: str) -> bool:
        return target != container and target.startswith(container.rstrip("/") + "/")


__all__ = [
    "OwnershipPathError",
    "WorkspacePathSet",
    "normalize_declared_path",
    "normalize_workspace_path",
]
