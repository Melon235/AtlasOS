"""Trusted clean-Git provisioning into an existing AnyCode sandbox."""

from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from anycode.sandbox import SandboxCommand, SandboxHandle, SandboxProvider

from atlasos.sandbox.models import SandboxCapabilityError, SandboxProvisionError


@dataclass(frozen=True)
class GitProjectSource:
    repo: Path
    source_commit: str
    bundle_path: Path
    bundle_digest: str
    bundle_size: int


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
    ) -> None:
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
            raise SandboxProvisionError(f"sandbox bundle upload failed: {upload.error}")
        workspace = self.workspace_root
        clone = await provider.execute(
            handle,
            SandboxCommand(
                command=(f"git clone /tmp/project.bundle {workspace}"),
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

    @staticmethod
    def require_capabilities(provider: SandboxProvider) -> None:
        capabilities = provider.capabilities()
        if not capabilities.file_transfer:
            raise SandboxCapabilityError("sandbox provider lacks file_transfer capability")
        if not capabilities.persistent_filesystem:
            raise SandboxCapabilityError("sandbox provider lacks persistent_filesystem capability")
        if capabilities.networking != "none":
            raise SandboxCapabilityError(
                "M5 v1 requires a provider that can enforce network='none'; "
                f"capabilities report {capabilities.networking!r}"
            )
        if capabilities.isolation not in ("container", "microvm", "vm", "remote"):
            raise SandboxCapabilityError(f"M5 v1 refuses isolation={capabilities.isolation!r}")
