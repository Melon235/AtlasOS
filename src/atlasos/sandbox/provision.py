"""Trusted clean-Git provisioning into an existing AnyCode sandbox."""

from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from anycode.sandbox import SandboxCommand, SandboxHandle, SandboxProvider

from atlasos.sandbox.models import (
    SandboxFileTransferError,
    SandboxProvisionError,
)
from atlasos.sandbox.provider import require_capabilities


@dataclass(frozen=True)
class GitProjectSource:
    repo: Path
    source_commit: str
    bundle_path: Path
    bundle_digest: str
    bundle_size: int


@dataclass(frozen=True)
class ProvisionReport:
    commands: int
    uploads: int


_PROVISION_COMMANDS = 4
_PROVISION_UPLOADS = 1


class GitProjectProvisioner:
    def __init__(self, *, max_bundle_bytes: int, workspace_root: str) -> None:
        self.max_bundle_bytes = max_bundle_bytes
        self.workspace_root = workspace_root

    def inspect(self, repo: str | Path) -> GitProjectSource:
        path = Path(repo).resolve()
        if not path.is_dir():
            raise SandboxProvisionError(f"project repository does not exist: {path}")
        status = subprocess.run(
            ["git", "-C", str(path), "status", "--porcelain"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if status.returncode != 0:
            raise SandboxProvisionError(f"not a Git repository: {path}")
        if status.stdout.strip():
            raise SandboxProvisionError(
                "M5 v1 does not provision dirty working trees. Commit or stash changes first."
            )
        commit = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.strip()
        if not commit:
            raise SandboxProvisionError("could not resolve project source commit")
        descriptor, name = tempfile.mkstemp(prefix="atlas-m5-", suffix=".bundle")
        os.close(descriptor)
        bundle = Path(name)
        try:
            result = subprocess.run(
                ["git", "-C", str(path), "bundle", "create", str(bundle), "HEAD"],
                check=False,
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode != 0:
                raise SandboxProvisionError(
                    f"git bundle failed: {result.stderr.strip() or result.stdout.strip()}"
                )
            size = bundle.stat().st_size
            if size > self.max_bundle_bytes:
                raise SandboxProvisionError(
                    f"project bundle is {size} bytes, above "
                    f"max_bundle_bytes={self.max_bundle_bytes}"
                )
            digest = "sha256:" + hashlib.sha256(bundle.read_bytes()).hexdigest()
            return GitProjectSource(path, commit, bundle, digest, size)
        except Exception:
            bundle.unlink(missing_ok=True)
            raise

    async def provision(
        self, provider: SandboxProvider, handle: SandboxHandle, source: GitProjectSource
    ) -> ProvisionReport:
        version = await provider.execute(
            handle, SandboxCommand(command="git --version", timeout_seconds=30)
        )
        if not version.ok or version.exit_code != 0:
            raise SandboxProvisionError(
                f"sandbox image lacks usable Git before task READY: "
                f"{version.stderr or version.error}"
            )
        bundle_bytes = source.bundle_path.read_bytes()
        upload = await provider.write_file(handle, "/tmp/project.bundle", bundle_bytes)
        if not upload.ok:
            raise SandboxFileTransferError(f"sandbox bundle upload failed: {upload.error}")
        workspace = self.workspace_root
        prepare = await provider.execute(
            handle,
            SandboxCommand(
                command=(
                    'python3 -c "import os, sys; '
                    f'os.makedirs(sys.argv[1], exist_ok=True)" {workspace}'
                ),
                cwd="/",
                timeout_seconds=30,
            ),
        )
        if not prepare.ok or prepare.exit_code != 0:
            raise SandboxProvisionError(
                f"sandbox workspace preparation failed: {prepare.stderr or prepare.error}"
            )
        clone = await provider.execute(
            handle,
            SandboxCommand(
                command=(f"git -c core.autocrlf=false clone /tmp/project.bundle {workspace}"),
                cwd="/",
                timeout_seconds=120,
            ),
        )
        if not clone.ok or clone.exit_code != 0:
            raise SandboxProvisionError(
                f"git clone inside sandbox failed: {clone.stderr or clone.error}"
            )
        verify = await provider.execute(
            handle,
            SandboxCommand(
                command=f"git -C {workspace} rev-parse HEAD",
                cwd="/",
                timeout_seconds=30,
            ),
        )
        if not verify.ok or verify.exit_code != 0:
            raise SandboxProvisionError(f"sandbox HEAD verification failed: {verify.stderr}")
        if verify.stdout.strip() != source.source_commit:
            raise SandboxProvisionError(
                f"sandbox HEAD {verify.stdout.strip()!r} != source commit {source.source_commit!r}"
            )
        return ProvisionReport(commands=_PROVISION_COMMANDS, uploads=_PROVISION_UPLOADS)

    @staticmethod
    def require_capabilities(provider: SandboxProvider) -> None:
        require_capabilities(provider.capabilities())
