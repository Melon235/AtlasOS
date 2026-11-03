"""Deterministic real OpenSandbox verified-reattachment evaluator (no LLM)."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from anycode.identity.context import ExecutionContext

from atlasos.config import load_config
from atlasos.sandbox import (
    OpenSandboxOptions,
    ProjectSandboxManager,
    SandboxProjectConfig,
    SQLiteSandboxBindingStore,
    build_sandbox_provider,
    require_capabilities,
    require_real_sandbox_provider,
)
from atlasos.sandbox.validation import probe_network_none

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = REPO_ROOT / "experiments" / "configs" / "v2-uat.yaml"
DEFAULT_FIXTURE = REPO_ROOT / "experiments" / "fixtures" / "uat_repo"
MARKER_PATH = "/workspace/project/reattach-marker.txt"
MARKER_BYTES = b"atlas-stage-b-verified-reattachment\n"


def _fixture_digest(path: Path) -> str:
    digest = hashlib.sha256()
    for item in sorted(path.rglob("*")):
        if item.is_file() and ".git" not in item.parts:
            digest.update(str(item.relative_to(path)).encode())
            digest.update(item.read_bytes())
    return "sha256:" + digest.hexdigest()


def _prepare_source_fixture(source: Path, destination: Path) -> Path:
    """Stage the fixture as an independent clean Git repository."""

    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(
        source,
        destination,
        ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"),
    )
    subprocess.run(["git", "init", "-q"], cwd=destination, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.test"],
        cwd=destination,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=destination,
        check=True,
    )
    subprocess.run(["git", "add", "."], cwd=destination, check=True)
    subprocess.run(["git", "commit", "-qm", "stage-b fixture"], cwd=destination, check=True)
    return destination


def _project_config(config) -> SandboxProjectConfig:
    sandbox = config.sandbox
    return SandboxProjectConfig(
        provider_name="opensandbox",
        image=sandbox.image,
        snapshot=sandbox.snapshot,
        workspace_root=sandbox.workspace_root,
        max_bundle_bytes=sandbox.max_bundle_bytes,
        network=sandbox.network,
    )


def _provider(config):
    sandbox = config.sandbox
    options = OpenSandboxOptions(
        endpoint=sandbox.endpoint,
        protocol=sandbox.protocol,
        timeout_minutes=sandbox.timeout_minutes,
        ready_timeout_seconds=sandbox.ready_timeout_seconds,
        cpu=sandbox.cpu,
        memory=sandbox.memory,
        use_server_proxy=sandbox.use_server_proxy,
    )
    provider = build_sandbox_provider("opensandbox", opensandbox_options=options)
    require_real_sandbox_provider(provider, expected_name="opensandbox")
    require_capabilities(provider.capabilities())
    return provider


async def run(*, config_path: Path, fixture: Path, runs_root: Path) -> dict[str, Any]:
    config = load_config(config_path)
    project_config = _project_config(config)
    runs_root.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240
    run_id = f"verified-reattach-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    result: dict[str, Any] = {
        "experiment": "V2-verified-sandbox-reattachment",
        "status": "FAIL",
        "run_id": run_id,
        "network_policy_verified": "NOT_RUN",
        "marker_persisted": False,
        "sandbox_before": None,
        "sandbox_after": None,
        "provider_created_at_before": None,
        "provider_created_at_after": None,
        "continuity_digest_before": None,
        "continuity_digest_after": None,
        "provider_create_count_total": 0,
        "host_fixture_digest_unchanged": False,
        "cleanup": False,
    }
    provider_a = _provider(config)
    provider_b = None
    manager_a: ProjectSandboxManager | None = None
    manager_b: ProjectSandboxManager | None = None
    reattached_binding = None
    try:
        with tempfile.TemporaryDirectory(prefix="atlas-stage-b-") as temporary:
            db_path = Path(temporary) / "sandbox_bindings.db"
            store = SQLiteSandboxBindingStore(db_path)
            source_repo = _prepare_source_fixture(fixture, Path(temporary) / "source-fixture")
            context = ExecutionContext(
                principal="user:stage-b-real",
                attributes={"run_id": run_id, "task_id": "task-stage-b-real"},
            )
            manager_a = ProjectSandboxManager(
                provider_a,
                project_config,
                run_id=run_id,
                binding_store=store,
            )
            binding = await manager_a.ensure_binding(
                task_id="task-stage-b-real",
                source_repo=str(source_repo),
                context=context,
                worker_id=None,
            )
            durable_before = store.get_for_task(run_id, "task-stage-b-real")
            assert durable_before is not None
            result["sandbox_before"] = binding.sandbox_handle.id
            result["provider_created_at_before"] = (
                durable_before.provider_created_at.isoformat()
                if durable_before.provider_created_at is not None
                else None
            )
            from atlasos.sandbox import compute_sandbox_continuity_digest

            result["continuity_digest_before"] = compute_sandbox_continuity_digest(durable_before)
            fixture_before = (
                _fixture_digest(fixture) if fixture.exists() else None  # noqa: ASYNC240
            )

            write = await provider_a.write_file(binding.sandbox_handle, MARKER_PATH, MARKER_BYTES)
            if not write.ok:
                raise RuntimeError(f"marker write failed: {write.error}")

            # Local detach: close SDK resources only; the remote sandbox must survive.
            await provider_a.detach_local(binding.sandbox_handle.id)
            manager_create_count = manager_a.create_count
            manager_a = None

            provider_b = _provider(config)
            manager_b = ProjectSandboxManager(
                provider_b,
                project_config,
                run_id=run_id,
                binding_store=SQLiteSandboxBindingStore(db_path),
            )
            reattached = await manager_b.reattach_verified(
                task_id="task-stage-b-real",
                context=context,
            )
            reattached_binding = reattached
            durable_after = SQLiteSandboxBindingStore(db_path).get_for_task(
                run_id, "task-stage-b-real"
            )
            assert durable_after is not None
            result["sandbox_after"] = reattached.sandbox_handle.id
            result["provider_created_at_after"] = (
                durable_after.provider_created_at.isoformat()
                if durable_after.provider_created_at is not None
                else None
            )
            result["continuity_digest_after"] = compute_sandbox_continuity_digest(durable_after)
            read = await provider_b.read_file(reattached.sandbox_handle, MARKER_PATH)
            result["marker_persisted"] = bool(read.ok and read.data == MARKER_BYTES)
            result["network_policy_verified"] = await probe_network_none(
                provider_b, reattached.sandbox_handle
            )
            fixture_after = (
                _fixture_digest(fixture) if fixture.exists() else None  # noqa: ASYNC240
            )
            result["host_fixture_digest_unchanged"] = fixture_before == fixture_after
            result["provider_create_count_total"] = manager_create_count + manager_b.create_count
            assert result["sandbox_before"] == result["sandbox_after"]
            assert result["provider_created_at_before"] == result["provider_created_at_after"]
            assert result["continuity_digest_before"] == result["continuity_digest_after"]
            assert result["marker_persisted"] is True
            assert result["network_policy_verified"] == "PASS"
            assert result["provider_create_count_total"] == 1
            assert result["host_fixture_digest_unchanged"] is True
            assert await manager_b.close("task-stage-b-real") is True
            result["cleanup"] = True
            result["status"] = "PASS"
    finally:
        if manager_b is not None:
            with contextlib.suppress(Exception):
                await manager_b.close("task-stage-b-real")
        elif manager_a is not None:
            with contextlib.suppress(Exception):
                await manager_a.close("task-stage-b-real")
        if provider_b is not None and not result["cleanup"]:
            if reattached_binding is not None:
                with contextlib.suppress(Exception):
                    await provider_b.destroy(reattached_binding.sandbox_handle)
            with contextlib.suppress(Exception):
                await provider_b.detach_local(str(result["sandbox_after"] or ""))
    path = runs_root / f"{run_id}.json"
    path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    result["result_path"] = str(path)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument(
        "--runs-root",
        type=Path,
        default=REPO_ROOT / "experiments" / "runs" / "verified-reattachment",
    )
    args = parser.parse_args()
    outcome = asyncio.run(
        run(config_path=args.config, fixture=args.fixture, runs_root=args.runs_root)
    )
    print(json.dumps(outcome, indent=2))
    raise SystemExit(0 if outcome["status"] == "PASS" else 1)
