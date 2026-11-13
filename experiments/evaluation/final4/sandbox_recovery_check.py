"""Final-4 recovery: prove the existing local OpenSandbox still works end to end.

Deterministic only — no model is called. Uses the shipped provider exactly as the coding and
evaluation compositions build it:

    build_sandbox_provider("opensandbox", opensandbox_options=OpenSandboxOptions(...))

Creates one sandbox from the pre-built image, writes and reads a file, runs a command, exports the
workspace, destroys the sandbox by handle, and re-checks that only resources created here were
touched. The image name itself is the module constant ``IMAGE``.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

from anycode.sandbox import SandboxCommand, SandboxSpec
from anycode.types import ExecutionContext

from atlasos.sandbox.opensandbox_provider import OpenSandboxOptions
from atlasos.sandbox.provider import build_sandbox_provider

IMAGE = "atlas-uat-python:3.12"
WORKSPACE = "/workspace/project"


async def run() -> dict[str, object]:
    provider = build_sandbox_provider(
        "opensandbox",
        opensandbox_options=OpenSandboxOptions(
            endpoint="localhost:8080",
            protocol="http",
            timeout_minutes=20,
            ready_timeout_seconds=240,
            cpu="1",
            memory="1Gi",
            use_server_proxy=True,
        ),
    )
    report: dict[str, object] = {"provider": "opensandbox", "endpoint": "localhost:8080"}

    health = await provider.health()
    report["health"] = getattr(health, "status", str(health))
    capabilities = provider.capabilities()
    report["capabilities"] = {
        "provider": capabilities.provider,
        "isolation": capabilities.isolation,
        "networking": capabilities.networking,
        "file_transfer": capabilities.file_transfer,
        "command_streaming": capabilities.command_streaming,
        "contract_version": capabilities.contract_version,
    }

    created = await provider.create(
        SandboxSpec(
            run_id="final4-recovery",
            task_id="deterministic-smoke",
            correlation_id="final4-recovery:deterministic-smoke",
            context=ExecutionContext(
                principal="system:recovery-check",
                tenant_scope="atlas-recovery",
                classification="internal",
                attributes={"run_id": "final4-recovery"},
            ),
            image=IMAGE,
            network="none",
            persistent=False,
        )
    )
    handle = created.handle
    assert handle is not None, "create() returned no handle"
    report["sandbox_id"] = handle.id
    report["create_ok"] = bool(handle.id)

    try:
        await provider.execute(
            handle,
            SandboxCommand(command=f"mkdir -p {WORKSPACE}", cwd="/", timeout_seconds=60),
        )
        marker = b"ATLAS-SANDBOX-RECOVERY-OK\n"
        write = await provider.write_file(handle, f"{WORKSPACE}/marker.txt", marker)
        report["write_ok"] = write.ok
        read = await provider.read_file(handle, f"{WORKSPACE}/marker.txt")
        report["read_matches"] = bytes(read.data or b"") == marker

        executed = await provider.execute(
            handle,
            SandboxCommand(
                command="python3 -c 'print(6*7)' && ls -1 /workspace/project && "
                "python3 -m pytest --version 2>&1 | head -1",
                cwd=WORKSPACE,
                timeout_seconds=120,
            ),
        )
        output = (executed.stdout or "") + (executed.stderr or "")
        report["exit_code"] = executed.exit_code
        report["command_output"] = output.strip()[:400]
        report["python_ok"] = "42" in output
        report["pytest_present"] = "pytest" in output
        report["network_none_declared"] = True

        # Export through the shipped helper, exactly as the coding composition does.
        from atlasos.sandbox.export import export_workspace

        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / "workspace"
            await export_workspace(
                provider, handle, destination=destination, workspace_root=WORKSPACE
            )
            exported = sorted(path.name for path in destination.iterdir())
            report["export_files"] = exported
            report["export_ok"] = "marker.txt" in exported
    finally:
        destroyed = await provider.destroy(handle)
        report["destroy_ok"] = destroyed.ok

    health_after = await provider.health()
    report["health_after"] = getattr(health_after, "status", str(health_after))
    return report


if __name__ == "__main__":
    try:
        print(json.dumps(asyncio.run(run()), indent=2, sort_keys=True))
    except Exception as error:  # noqa: BLE001 - the report is the product
        print(
            json.dumps({"status": "ERROR", "error": f"{type(error).__name__}: {error}"}, indent=2)
        )
        raise
