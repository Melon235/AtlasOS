"""Task-end export from the sandbox, never auto-applied to Host.

M5 exports a patch for provenance and an explicit workspace snapshot for external acceptance.
Both artifacts come from the remote sandbox through the provider's file-transfer API; the Host
project is never used as a fallback or mutated.
"""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import tarfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from anycode.sandbox import SandboxCommand, SandboxHandle, SandboxProvider

from atlasos.sandbox.models import (
    SandboxExecutionError,
    SandboxFileTransferError,
    SandboxLostError,
)

_EXPORT_HELPER = r"""
import json, os, sys, tarfile

root, archive = sys.argv[1], sys.argv[2]
excluded = {".git", "__pycache__", ".pytest_cache", ".venv", "node_modules", "dist", "build"}
files = []
with tarfile.open(archive, "w") as tar:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(name for name in dirnames if name not in excluded)
        for name in sorted(filenames):
            path = os.path.join(dirpath, name)
            if os.path.islink(path) or not os.path.isfile(path):
                continue
            relative = os.path.relpath(path, root)
            tar.add(path, arcname=relative, recursive=False)
            files.append(relative)
print(json.dumps({"file_count": len(files), "size": os.path.getsize(archive)}))
""".strip()


@dataclass(frozen=True)
class SandboxPatch:
    base_commit: str
    patch_text: str
    patch_digest: str
    changed_file_count: int


@dataclass(frozen=True)
class SandboxWorkspaceExport:
    destination: Path
    archive_digest: str
    file_count: int
    size_bytes: int


async def export_patch(
    provider: SandboxProvider,
    handle: SandboxHandle,
    *,
    base_commit: str,
    workspace_root: str = "/workspace/project",
) -> SandboxPatch:
    cwd = workspace_root
    status = await provider.execute(
        handle,
        SandboxCommand(
            command=f"git -C {workspace_root} status --porcelain",
            cwd=cwd,
            timeout_seconds=30,
        ),
    )
    if not status.ok or status.exit_code != 0:
        raise SandboxLostError(f"cannot export sandbox status: {status.stderr or status.error}")
    prepare = await provider.execute(
        handle,
        SandboxCommand(
            command=f"git -C {workspace_root} add -N --all",
            cwd=cwd,
            timeout_seconds=30,
        ),
    )
    if not prepare.ok or prepare.exit_code != 0:
        raise SandboxLostError(f"cannot prepare sandbox patch: {prepare.stderr or prepare.error}")
    diff = await provider.execute(
        handle,
        SandboxCommand(
            command=(f"git -C {workspace_root} diff --binary --no-ext-diff HEAD"),
            cwd=cwd,
            timeout_seconds=60,
        ),
    )
    if not diff.ok or diff.exit_code != 0:
        raise SandboxLostError(f"cannot export sandbox patch: {diff.stderr or diff.error}")
    patch_text = diff.stdout
    digest = "sha256:" + hashlib.sha256(patch_text.encode("utf-8")).hexdigest()
    changed = [line for line in status.stdout.splitlines() if line.strip()]
    return SandboxPatch(
        base_commit=base_commit,
        patch_text=patch_text,
        patch_digest=digest,
        changed_file_count=len(changed),
    )


async def export_workspace(
    provider: SandboxProvider,
    handle: SandboxHandle,
    *,
    destination: Path,
    workspace_root: str = "/workspace/project",
    max_bytes: int = 64 * 1024 * 1024,
) -> SandboxWorkspaceExport:
    """Copy the remote workspace into ``destination`` through provider file transfer.

    The remote helper writes an uncompressed tar archive inside the sandbox; Atlas downloads it
    with ``read_file`` and extracts it Host-side with traversal checks. This is a control-plane
    export, not a model action, and it never writes back to the Host fixture.
    """

    helper_path = "/tmp/atlas_export_workspace.py"
    archive_path = "/tmp/atlas_workspace_export.tar"
    upload = await provider.write_file(handle, helper_path, _EXPORT_HELPER.encode("utf-8"))
    if not upload.ok:
        raise SandboxFileTransferError(f"cannot upload export helper: {upload.error}")
    result = await provider.execute(
        handle,
        SandboxCommand(
            command=f"python3 {helper_path} {workspace_root} {archive_path}",
            cwd="/",
            timeout_seconds=120,
        ),
    )
    if not result.ok or result.exit_code != 0:
        raise SandboxExecutionError(
            f"cannot create workspace export in sandbox: {result.stderr or result.error}"
        )
    try:
        manifest = json.loads(result.stdout.strip().splitlines()[-1])
        remote_file_count = int(manifest["file_count"])
        remote_size = int(manifest["size"])
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise SandboxExecutionError(f"invalid export manifest: {result.stdout!r}") from error
    if remote_size > max_bytes:
        raise SandboxFileTransferError(
            f"workspace export is {remote_size} bytes, above max_bytes={max_bytes}"
        )
    download = await provider.read_file(handle, archive_path)
    if not download.ok or download.data is None:
        raise SandboxFileTransferError(f"cannot download workspace export: {download.error}")
    archive_bytes = download.data
    if len(archive_bytes) > max_bytes:
        raise SandboxFileTransferError(
            f"workspace export download is {len(archive_bytes)} bytes, above max_bytes={max_bytes}"
        )
    if destination.exists():  # noqa: ASYNC240 - control-plane filesystem check
        raise SandboxFileTransferError(f"export destination already exists: {destination}")
    extracted = _extract_workspace_archive(archive_bytes, destination)
    if extracted != remote_file_count:
        shutil.rmtree(destination, ignore_errors=True)
        raise SandboxFileTransferError(
            f"exported file count mismatch: sandbox={remote_file_count}, host={extracted}"
        )
    return SandboxWorkspaceExport(
        destination=destination,
        archive_digest="sha256:" + hashlib.sha256(archive_bytes).hexdigest(),
        file_count=extracted,
        size_bytes=len(archive_bytes),
    )


def _extract_workspace_archive(data: bytes, destination: Path) -> int:
    destination.mkdir(parents=True, exist_ok=False)
    count = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
            for member in archive.getmembers():
                relative = PurePosixPath(member.name.replace("\\", "/"))
                if relative.is_absolute() or ".." in relative.parts or not relative.parts:
                    raise SandboxFileTransferError(f"unsafe archive member: {member.name!r}")
                if not member.isfile():
                    raise SandboxFileTransferError(f"unsupported archive member: {member.name!r}")
                source = archive.extractfile(member)
                if source is None:
                    raise SandboxFileTransferError(f"cannot read archive member: {member.name!r}")
                target = destination.joinpath(*relative.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(source.read())
                count += 1
    except tarfile.TarError as error:
        shutil.rmtree(destination, ignore_errors=True)
        raise SandboxFileTransferError(f"invalid workspace archive: {error}") from error
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    return count
