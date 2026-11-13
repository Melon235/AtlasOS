# ruff: noqa: E501
"""Final-4 Phase 1: real-container parity between the native baseline and Atlas.

Deterministic — no model is called.

Two independent containers, same image, same network policy, same 1 CPU / 1 Gi:

* **Native arm** — ``OpenSandboxProvider.create`` + ``SandboxToolBackend`` + ``NeutralEngine``
  (upstream AnyCode ``ToolRegistry``/``ToolExecutor``). No Atlas gate, ledger, binding state machine,
  context plane, disclosure, router or recovery.
* **Atlas arm** — ``ProjectSandboxManager.ensure_binding`` (Atlas's own binding/lifecycle) over the
  same provider, then the *same* ``SandboxToolBackend`` for the probe. The probe layer is held
  constant, so the comparison isolates *who creates and binds the environment*.

The native arm writes the seed **through the six registered tools** (not through the backend API),
which is what proves the upstream executor really reaches the container.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from anycode.sandbox import SandboxSpec
from anycode.types import AgentInfo, ExecutionContext, ToolUseContext
from experiments.evaluation.final4.neutral_adapter import (
    NEUTRAL_TOOLS,
    NeutralEngine,
    SandboxToolBackend,
)

from atlasos.sandbox.binding import ProjectSandboxManager
from atlasos.sandbox.models import SandboxProjectConfig
from atlasos.sandbox.opensandbox_provider import OpenSandboxOptions
from atlasos.sandbox.provider import build_sandbox_provider

IMAGE = "atlas-uat-python:3.12"
WORKSPACE = "/workspace/project"

SEED: dict[str, str] = {
    "calc.py": "def add(a: int, b: int) -> int:\n    return a + b\n",
    "test_calc.py": "from calc import add\n\n\ndef test_add() -> None:\n    assert add(2, 3) == 5\n",
    "README.md": "# parity fixture\n\nA two-file project used only for environment comparison.\n",
}

PROBE = (
    "python3 -m pytest -q 2>&1 | tail -2 | sed 's/ in [0-9.]*s//'; "
    "echo SHELL=$0; uname -s; python3 -V; "
    "python3 -c 'import socket; socket.create_connection((\"1.1.1.1\",443),3)' 2>&1 | tail -1"
)


def _digest(files: dict[str, str]) -> str:
    payload = json.dumps(files, sort_keys=True).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _provider():
    return build_sandbox_provider(
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


def _context() -> ToolUseContext:
    return ToolUseContext(
        agent=AgentInfo(name="neutral-parity", role="coder", model="fake"), cwd=WORKSPACE
    )


def _grep_paths(output: str) -> list[str]:
    """Same matched files, ignoring absolute prefixes and line ordering."""

    return sorted(
        {line.split(":", 1)[0].split("/")[-1] for line in output.splitlines() if line.strip()}
    )


async def _probe(backend: SandboxToolBackend) -> dict[str, Any]:
    code, raw = await backend.arun(PROBE)
    lines = [line for line in raw.splitlines() if line.strip()]
    last = lines[-1] if lines else ""
    return {
        "exit_code": code,
        "pytest_summary": [line for line in lines if "passed" in line or "failed" in line],
        "shell": next((line for line in lines if line.startswith("SHELL=")), None),
        "uname": next((line for line in lines if line in {"Linux", "Darwin"}), None),
        "python": next((line for line in lines if line.startswith("Python ")), None),
        "network_blocked": any(
            marker in last
            for marker in ("Network is unreachable", "Name or service not known", "timed out")
        ),
        "network_last_line": last[:200],
        "files": await backend.alist("."),
    }


async def _native_arm(provider: Any) -> dict[str, Any]:
    created = await provider.create(
        SandboxSpec(
            run_id="final4-parity-native",
            task_id="parity",
            correlation_id="final4-parity:native",
            context=ExecutionContext(
                principal="system:neutral-parity",
                tenant_scope="atlas-neutral",
                classification="internal",
                attributes={"run_id": "final4-parity-native"},
            ),
            image=IMAGE,
            network="none",
            persistent=False,
        )
    )
    handle = created.handle
    assert handle is not None
    backend = SandboxToolBackend(provider=provider, handle=handle, workspace_root=WORKSPACE)
    engine = NeutralEngine(backend)
    tools = {tool.name: tool for tool in engine._neutral_registry.list()}  # noqa: SLF001
    context = _context()
    result: dict[str, Any] = {
        "sandbox_id": handle.id,
        "tool_surface": sorted(tools),
        "writes_via_tools": [],
    }
    try:
        for name, content in SEED.items():
            written = await tools["file_write"].execute(  # type: ignore[attr-defined]
                type("Args", (), {"path": name, "content": content})(), context
            )
            result["writes_via_tools"].append(
                {"file": name, "is_error": bool(written.is_error), "data": str(written.data)[:160]}
            )
        edited = await tools["file_edit"].execute(  # type: ignore[attr-defined]
            type("Args", (), {"path": "calc.py", "old": "a + b", "new": "a + b  # parity"})(),
            context,
        )
        result["edit_ok"] = not bool(edited.is_error)
        read = await tools["file_read"].execute(  # type: ignore[attr-defined]
            type("Args", (), {"path": "calc.py"})(), context
        )
        result["read_has_marker"] = "# parity" in str(read.data)
        result["read_matches_atlas_later"] = str(read.data)
        result["grep"] = str(
            await tools["grep"].execute(  # type: ignore[attr-defined]
                type("Args", (), {"pattern": "def add", "path": "."})(), context
            )
        )[:400]
        result["probe"] = await _probe(backend)
        return result
    finally:
        await provider.destroy(handle)


def _git_fixture() -> Path:
    root = Path(tempfile.mkdtemp(prefix="final4-parity-repo-"))
    for name, content in SEED.items():
        (root / name).write_text(content, encoding="utf-8")
    ident = {
        **os.environ,
        "GIT_AUTHOR_NAME": "parity",
        "GIT_AUTHOR_EMAIL": "parity@example.invalid",
        "GIT_COMMITTER_NAME": "parity",
        "GIT_COMMITTER_EMAIL": "parity@example.invalid",
    }
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, env=ident)
    subprocess.run(["git", "add", "."], cwd=root, check=True, env=ident)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=root, check=True, env=ident)
    return root


async def _atlas_arm(provider: Any) -> dict[str, Any]:
    config = SandboxProjectConfig(
        provider_name="opensandbox", image=IMAGE, workspace_root=WORKSPACE
    )
    manager = ProjectSandboxManager(provider, config, run_id="final4-parity-atlas")
    repo = _git_fixture()
    binding = await manager.ensure_binding(
        task_id="parity",
        source_repo=str(repo),
        context=ExecutionContext(
            principal="system:parity-atlas",
            tenant_scope="atlas-parity",
            classification="internal",
            attributes={"run_id": "final4-parity-atlas"},
        ),
    )
    handle = binding.sandbox_handle
    backend = SandboxToolBackend(provider=provider, handle=handle, workspace_root=WORKSPACE)
    try:
        result = {
            "sandbox_id": handle.id,
            "source_commit": getattr(binding, "source_commit", None),
            "probe": await _probe(backend),
        }
        # Apply the same edit as the native arm so the exported trees are comparable.
        await backend.aedit("calc.py", "a + b", "a + b  # parity")
        result["read_matches_native_later"] = await backend.aread("calc.py")
        result["grep"] = (await backend.agrep("def add", "."))[:400]
        return result
    finally:
        await manager.close("parity")


async def run() -> dict[str, Any]:
    provider = _provider()
    health = await provider.health()
    report: dict[str, Any] = {
        "image": IMAGE,
        "workspace_root": WORKSPACE,
        "network_policy": "none",
        "seed_digest": _digest(SEED),
        "seed_files": sorted(SEED),
        "provider_health": getattr(health, "status", str(health)),
        "neutral_tools": list(NEUTRAL_TOOLS),
    }
    try:
        report["native"] = await _native_arm(provider)
    except Exception as error:  # noqa: BLE001 - a blocked arm is a result
        report["native_error"] = f"{type(error).__name__}: {error}"
    try:
        report["atlas"] = await _atlas_arm(provider)
    except Exception as error:  # noqa: BLE001
        report["atlas_error"] = f"{type(error).__name__}: {error}"

    if "native" in report and "atlas" in report:
        native_probe = report["native"]["probe"]
        atlas_probe = report["atlas"]["probe"]
        report["parity"] = {
            "same_pytest_result": native_probe["pytest_summary"] == atlas_probe["pytest_summary"],
            "same_shell": native_probe["shell"] == atlas_probe["shell"],
            "same_uname": native_probe["uname"] == atlas_probe["uname"],
            "same_python": native_probe["python"] == atlas_probe["python"],
            "network_blocked_both": native_probe["network_blocked"]
            and atlas_probe["network_blocked"],
            "same_seed_files": native_probe["files"] == atlas_probe["files"],
            "same_file_content": report["native"]["read_matches_atlas_later"]
            == report["atlas"]["read_matches_native_later"],
            "same_grep_paths": _grep_paths(report["native"]["grep"])
            == _grep_paths(report["atlas"]["grep"]),
            "native_tool_surface_exact": report["native"]["tool_surface"] == sorted(NEUTRAL_TOOLS),
            "native_writes_all_ok": all(
                item["is_error"] is False for item in report["native"]["writes_via_tools"]
            ),
        }
        report["parity"]["all_match"] = all(report["parity"].values())
        report["status"] = "BASELINE_PARITY_PASS" if report["parity"]["all_match"] else "PARTIAL"
    else:
        report["status"] = "BASELINE_PARITY_BLOCKED"
    return report


if __name__ == "__main__":
    print(json.dumps(asyncio.run(run()), indent=2, sort_keys=True))
