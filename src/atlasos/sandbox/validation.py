"""Control-plane probes for Atlas Sandbox isolation evidence.

These probes observe the provider through its public contract. They are not a sandbox abstraction,
do not execute project workloads, and never replace provider-enforced isolation with an Atlas
implementation. Their results are empirical evidence only; they do not prove kernel or
cryptographic isolation.
"""

from __future__ import annotations

import shlex
import uuid
from dataclasses import dataclass
from pathlib import Path

from anycode.sandbox import SandboxCommand, SandboxHandle, SandboxProvider

from atlasos.sandbox.models import (
    SandboxExecutionError,
    SandboxFileTransferError,
    SandboxIsolationError,
)

_NETWORK_SCRIPT = (
    "import socket, sys\n"
    "s = socket.socket()\n"
    "s.settimeout(3)\n"
    "try:\n"
    "    s.connect(('1.1.1.1', 443))\n"
    "except OSError:\n"
    "    print('BLOCKED')\n"
    "else:\n"
    "    print('REACHABLE')\n"
    "    sys.exit(2)\n"
    "finally:\n"
    "    s.close()\n"
)

_SEARCH_SCRIPT = (
    "import os, sys\n"
    "token = sys.argv[1].encode()\n"
    "for root in ('/workspace', '/tmp'):\n"
    "    for dirpath, _, filenames in os.walk(root):\n"
    "        for name in filenames:\n"
    "            path = os.path.join(dirpath, name)\n"
    "            try:\n"
    "                data = open(path, 'rb').read()\n"
    "            except OSError:\n"
    "                continue\n"
    "            if token in data:\n"
    "                print(path)\n"
    "                sys.exit(2)\n"
    "print('NOT_FOUND')\n"
)


@dataclass(frozen=True)
class HostSeparationEvidence:
    host_only_visible: bool
    sandbox_only_visible_on_host: bool
    sandbox_only_read_back: bool


async def probe_network_none(provider: SandboxProvider, handle: SandboxHandle) -> str:
    """Return ``PASS``, ``FAIL``, or ``UNKNOWN`` for an egress-block probe."""

    command = "python3 -c " + shlex.quote(_NETWORK_SCRIPT)
    result = await provider.execute(
        handle, SandboxCommand(command=command, cwd="/", timeout_seconds=15)
    )
    if result.ok and result.exit_code == 0 and "BLOCKED" in result.stdout:
        return "PASS"
    if result.exit_code == 2 or "REACHABLE" in result.stdout:
        return "FAIL"
    return "UNKNOWN"


async def probe_host_separation(
    provider: SandboxProvider,
    handle: SandboxHandle,
    *,
    host_dir: Path,
) -> HostSeparationEvidence:
    """Create a Host-only sentinel and a sandbox-only sentinel and check separation."""

    host_token = uuid.uuid4().hex
    (host_dir / "HOST_ONLY_SENTINEL.txt").write_text(host_token, encoding="utf-8")
    search_command = f"python3 -c {shlex.quote(_SEARCH_SCRIPT)} {shlex.quote(host_token)}"
    search = await provider.execute(
        handle, SandboxCommand(command=search_command, cwd="/", timeout_seconds=30)
    )
    if search.exit_code == 2:
        host_only_visible = True
    elif search.ok and search.exit_code == 0:
        host_only_visible = False
    else:
        raise SandboxExecutionError(
            f"Host-only sentinel probe failed: {search.stderr or search.error}"
        )

    sandbox_token = uuid.uuid4().hex
    sandbox_sentinel = f"/tmp/atlas-sandbox-only-sentinel-{sandbox_token}.txt"
    upload = await provider.write_file(handle, sandbox_sentinel, sandbox_token.encode())
    if not upload.ok:
        raise SandboxFileTransferError(f"cannot write sandbox-only sentinel: {upload.error}")
    download = await provider.read_file(handle, sandbox_sentinel)
    if not download.ok or download.data is None:
        raise SandboxFileTransferError(f"cannot read sandbox-only sentinel: {download.error}")
    sandbox_only_read_back = download.data == sandbox_token.encode()
    sandbox_only_visible_on_host = (host_dir / "SANDBOX_ONLY_SENTINEL.txt").exists()
    return HostSeparationEvidence(
        host_only_visible=host_only_visible,
        sandbox_only_visible_on_host=sandbox_only_visible_on_host,
        sandbox_only_read_back=sandbox_only_read_back,
    )


def require_host_separation(evidence: HostSeparationEvidence) -> None:
    if evidence.host_only_visible:
        raise SandboxIsolationError("sandbox can see the Host-only sentinel")
    if evidence.sandbox_only_visible_on_host:
        raise SandboxIsolationError("a sandbox-only sentinel appeared on the Host")
    if not evidence.sandbox_only_read_back:
        raise SandboxIsolationError("sandbox file write/read-back did not preserve the sentinel")
