"""Task-end patch export from the sandbox, never auto-applied to Host."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from anycode.sandbox import SandboxCommand, SandboxHandle, SandboxProvider

from atlasos.sandbox.models import SandboxLostError


@dataclass(frozen=True)
class SandboxPatch:
    base_commit: str
    patch_text: str
    patch_digest: str
    changed_file_count: int


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
