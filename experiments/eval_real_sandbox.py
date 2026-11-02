"""Minimal real Atlas Sandbox isolation evaluator.

This is a control-plane smoke evaluation, not a model workload. It creates one real self-hosted
OpenSandbox sandbox through the configured AnyCode-compatible provider, exercises file transfer and
command execution, checks empirical Host/sandbox separation and network-none behavior, exports the
workspace, then destroys the sandbox.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from anycode.sandbox import SandboxCommand, SandboxProvider
from anycode.security.redaction import safe_exception_message
from dotenv import load_dotenv

from atlasos.config import load_config
from atlasos.sandbox import (
    REAL_SANDBOX_PROVIDERS,
    OpenSandboxOptions,
    SandboxCapabilityError,
    SandboxCredentialError,
    SandboxEnvironmentBlocked,
    SandboxProjectConfig,
    build_sandbox_provider,
    capability_digest,
    capability_summary,
    export_workspace,
    require_capabilities,
    require_real_sandbox_provider,
    trusted_sandbox_spec,
)
from atlasos.sandbox.models import SandboxBindingError, SandboxIsolationError
from atlasos.sandbox.validation import (
    probe_host_separation,
    probe_network_none,
    require_host_separation,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "experiments" / "fixtures" / "uat_repo"
UAT_CONFIG = REPO_ROOT / "experiments" / "configs" / "v2-uat.yaml"


@dataclass
class RealSandboxRecord:
    timestamp: str
    provider_name: str
    provider_is_real: bool
    provider_is_external_cloud: bool
    provider_is_self_hosted: bool
    opensandbox_server_health: str
    runtime_backend: str
    provider_capabilities: dict[str, Any] | None
    capabilities_digest: str | None
    sandbox_created: bool
    sandbox_ready: bool
    sandbox_logical_id: str | None
    workspace_path: str
    workspace_export: str | None
    export_file_count: int
    file_upload_ok: bool
    file_read_ok: bool
    python_version_stdout: str
    python_version_exit_code: int | None
    script_stdout: str
    script_exit_code: int | None
    network_policy_requested: str
    network_policy_accepted: bool
    network_policy_verified: str
    host_only_sentinel_visible: bool | None
    sandbox_only_sentinel_visible_on_host: bool | None
    sandbox_only_sentinel_read: bool
    host_fixture_digest_before: str
    host_fixture_digest_after: str
    host_fixture_digest_unchanged: bool
    sandbox_destroyed: bool
    failure_class: str | None
    failure_message: str | None
    status: str


def _fixture_digest(path: Path) -> str:
    digest = hashlib.sha256()
    for item in sorted(path.rglob("*")):
        if item.is_file() and "__pycache__" not in item.parts:
            digest.update(item.relative_to(path).as_posix().encode())
            digest.update(item.read_bytes())
    return digest.hexdigest()


def _text(value: str, limit: int = 500) -> str:
    return value if len(value) <= limit else value[:limit] + "...[truncated]"


async def _run(args: argparse.Namespace) -> tuple[RealSandboxRecord, int]:
    load_dotenv(REPO_ROOT / ".env", override=False)
    config = load_config(args.config)
    provider_name = args.sandbox_provider or config.sandbox.provider
    run_id = f"real-sandbox-{uuid.uuid4().hex}"
    runs_root = args.runs_root.resolve()
    run_dir = runs_root / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    host_before = _fixture_digest(FIXTURE)
    record = RealSandboxRecord(
        timestamp=datetime.now(UTC).isoformat(),
        provider_name=provider_name,
        provider_is_real=False,
        provider_is_external_cloud=False,
        provider_is_self_hosted=provider_name == "opensandbox",
        opensandbox_server_health="UNKNOWN",
        runtime_backend="unknown",
        provider_capabilities=None,
        capabilities_digest=None,
        sandbox_created=False,
        sandbox_ready=False,
        sandbox_logical_id=None,
        workspace_path=config.sandbox.workspace_root,
        workspace_export=None,
        export_file_count=0,
        file_upload_ok=False,
        file_read_ok=False,
        python_version_stdout="",
        python_version_exit_code=None,
        script_stdout="",
        script_exit_code=None,
        network_policy_requested=config.sandbox.network,
        network_policy_accepted=False,
        network_policy_verified="UNKNOWN",
        host_only_sentinel_visible=None,
        sandbox_only_sentinel_visible_on_host=None,
        sandbox_only_sentinel_read=False,
        host_fixture_digest_before=host_before,
        host_fixture_digest_after=host_before,
        host_fixture_digest_unchanged=True,
        sandbox_destroyed=False,
        failure_class=None,
        failure_message=None,
        status="FAIL",
    )
    provider: SandboxProvider | None = None
    handle = None
    failure_class: str | None = None
    failure_message: str | None = None

    if provider_name not in REAL_SANDBOX_PROVIDERS:
        record.failure_class = "SANDBOX_PROVIDER_FAILURE"
        record.failure_message = f"configured provider {provider_name!r} is not a real provider"
        record.status = "FAIL"
        return record, 1

    try:
        sandbox_config = config.sandbox
        options = OpenSandboxOptions(
            endpoint=sandbox_config.endpoint,
            protocol=sandbox_config.protocol,
            timeout_minutes=sandbox_config.timeout_minutes,
            ready_timeout_seconds=sandbox_config.ready_timeout_seconds,
            cpu=sandbox_config.cpu,
            memory=sandbox_config.memory,
            use_server_proxy=sandbox_config.use_server_proxy,
        )
        provider = build_sandbox_provider(provider_name, opensandbox_options=options)
        capabilities = provider.capabilities()
        record.provider_capabilities = capability_summary(capabilities)
        record.capabilities_digest = capability_digest(capabilities)
        require_real_sandbox_provider(provider, expected_name=provider_name)
        record.provider_is_real = True
        record.runtime_backend = "docker" if provider_name == "opensandbox" else "external"
        require_capabilities(capabilities)
        health = await provider.health()
        record.opensandbox_server_health = health.status
        if health.status == "unavailable":
            raise SandboxEnvironmentBlocked(f"sandbox provider unavailable: {health.message}")

        project_config = SandboxProjectConfig(
            provider_name=provider_name,
            image=config.sandbox.image,
            snapshot=config.sandbox.snapshot,
            workspace_root=config.sandbox.workspace_root,
            max_bundle_bytes=config.sandbox.max_bundle_bytes,
            network=config.sandbox.network,
        )
        spec = trusted_sandbox_spec(
            run_id=run_id,
            task_id=f"{run_id}-task",
            worker_id=None,
            context=_execution_context(run_id),
            config=project_config,
        )
        created = await provider.create(spec)
        if not created.ok or created.handle is None:
            raise SandboxBindingError(f"sandbox create failed: {created.error}")
        handle = created.handle
        record.sandbox_created = True
        record.sandbox_ready = True
        record.sandbox_logical_id = handle.id
        record.network_policy_accepted = True

        prepare = await provider.execute(
            handle,
            SandboxCommand(command="mkdir -p /workspace/project", cwd="/", timeout_seconds=30),
        )
        if not prepare.ok or prepare.exit_code != 0:
            raise SandboxBindingError(
                f"cannot prepare workspace: {prepare.stderr or prepare.error}"
            )
        payload = b"atlas-real-sandbox-smoke\n"
        upload = await provider.write_file(handle, "/workspace/project/smoke.txt", payload)
        record.file_upload_ok = upload.ok
        if not upload.ok:
            raise SandboxBindingError(f"file upload failed: {upload.error}")
        download = await provider.read_file(handle, "/workspace/project/smoke.txt")
        record.file_read_ok = download.ok and download.data == payload
        if not record.file_read_ok:
            raise SandboxBindingError(f"file read-back failed: {download.error}")

        version = await provider.execute(
            handle,
            SandboxCommand(
                command="python3 --version", cwd="/workspace/project", timeout_seconds=30
            ),
        )
        record.python_version_stdout = _text(version.stdout.strip() or version.stderr.strip())
        record.python_version_exit_code = version.exit_code
        if not version.ok or version.exit_code != 0:
            raise SandboxBindingError(f"python --version failed: {version.stderr or version.error}")
        script = await provider.execute(
            handle,
            SandboxCommand(
                command='python3 -c "print(sum(i for i in range(10)))"',
                cwd="/workspace/project",
                timeout_seconds=30,
            ),
        )
        record.script_stdout = _text(script.stdout.strip())
        record.script_exit_code = script.exit_code
        if not script.ok or script.exit_code != 0 or script.stdout.strip() != "45":
            raise SandboxBindingError(
                f"small Python script failed: {script.stderr or script.error}"
            )

        record.network_policy_verified = await probe_network_none(provider, handle)
        if record.network_policy_verified == "FAIL":
            raise SandboxIsolationError(
                "sandbox requested network='none' but the empirical egress probe succeeded"
            )
        separation = await probe_host_separation(provider, handle, host_dir=run_dir)
        record.host_only_sentinel_visible = separation.host_only_visible
        record.sandbox_only_sentinel_visible_on_host = separation.sandbox_only_visible_on_host
        record.sandbox_only_sentinel_read = separation.sandbox_only_read_back
        require_host_separation(separation)

        export_destination = run_dir / "workspace"
        exported = await export_workspace(
            provider,
            handle,
            destination=export_destination,
            workspace_root=config.sandbox.workspace_root,
        )
        record.workspace_export = str(export_destination)
        record.export_file_count = exported.file_count
        if not (export_destination / "smoke.txt").is_file():
            raise SandboxBindingError("explicit workspace export did not contain smoke.txt")
    except SandboxCredentialError as error:
        failure_class = "CREDENTIAL_BLOCKED"
        failure_message = safe_exception_message(error)
    except SandboxEnvironmentBlocked as error:
        failure_class = "SANDBOX_ENVIRONMENT_BLOCKED"
        failure_message = safe_exception_message(error)
    except SandboxIsolationError as error:
        failure_class = "SANDBOX_ISOLATION_FAILURE"
        failure_message = safe_exception_message(error)
    except SandboxCapabilityError as error:
        failure_class = "SANDBOX_CAPABILITY_FAILURE"
        failure_message = safe_exception_message(error)
    except SandboxBindingError as error:
        failure_class = "SANDBOX_PROVIDER_FAILURE"
        failure_message = safe_exception_message(error)
    except Exception as error:  # noqa: BLE001 - evaluator must classify every failure
        failure_class = "SANDBOX_PROVIDER_FAILURE"
        failure_message = safe_exception_message(error)
    finally:
        if provider is not None and handle is not None:
            try:
                destroyed = await provider.destroy(handle)
                record.sandbox_destroyed = destroyed.ok
            except Exception:  # noqa: BLE001 - cleanup evidence is recorded below
                record.sandbox_destroyed = False
        record.host_fixture_digest_after = _fixture_digest(FIXTURE)
        record.host_fixture_digest_unchanged = (
            record.host_fixture_digest_before == record.host_fixture_digest_after
        )
        if not record.sandbox_destroyed and record.sandbox_created:
            failure_class = failure_class or "SANDBOX_CLEANUP_FAILURE"
            failure_message = failure_message or "sandbox destroy was not confirmed"
        if failure_class is None and not record.host_fixture_digest_unchanged:
            failure_class = "SANDBOX_ISOLATION_FAILURE"
            failure_message = "Host fixture digest changed during the sandbox evaluation"
        record.failure_class = failure_class
        record.failure_message = failure_message
        if failure_class is None:
            record.status = "PASS"
        elif failure_class == "CREDENTIAL_BLOCKED":
            record.status = "BLOCKED_NO_CREDENTIALS"
        elif failure_class == "SANDBOX_ENVIRONMENT_BLOCKED":
            record.status = "BLOCKED_ENVIRONMENT"
        else:
            record.status = "FAIL"
        (run_dir / "result.json").write_text(json.dumps(asdict(record), indent=2), encoding="utf-8")

    exit_code = 0 if record.status == "PASS" else 2 if record.status.startswith("BLOCKED") else 1
    return record, exit_code


def _execution_context(run_id: str):
    from anycode.identity.context import ExecutionContext

    return ExecutionContext(
        principal="user:real-sandbox-eval",
        tenant_scope="verification",
        classification="internal",
        attributes={"run_id": run_id, "task_id": f"{run_id}-task"},
    )


def main() -> None:
    parser = argparse.ArgumentParser(prog="atlas-real-sandbox-eval")
    parser.add_argument("--config", type=Path, default=UAT_CONFIG)
    parser.add_argument(
        "--runs-root", type=Path, default=REPO_ROOT / "experiments" / "runs" / "real-sandbox"
    )
    parser.add_argument("--sandbox-provider", choices=sorted(REAL_SANDBOX_PROVIDERS), default=None)
    args = parser.parse_args()
    record, exit_code = asyncio.run(_run(args))
    print(json.dumps(asdict(record), indent=2, ensure_ascii=False))
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
