"""Thin UAT CLI adapter over the frozen AtlasOS V2 runtime.

This is a user-facing test surface, not a new runtime. It parses a natural-language task, loads
the configured provider, starts one Atlas worker/session, runs one engine task, and prints the
final result. It owns no scheduling, policy, execution, approval, or recovery logic.

The current environment has no production AnyCode sandbox provider, so the coding mode uses the
repository's deterministic local FakeSandboxProvider for UAT infrastructure only. A production UAT
must select a real provider (e.g. an AnyCode sandbox extra + credentials); this is reported as
``BLOCKED_NO_REAL_SANDBOX_PROVIDER`` by ``experiments/uat_v2.py``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from anycode import OrchestratorConfig, TaskSpec, TeamConfig
from anycode.identity.context import ExecutionContext
from anycode.plugins.registry import register_provider_factory
from anycode.runstore.store import FilesystemRunStore
from anycode.tools.idempotency import SQLiteToolIdempotencyStore
from anycode.types import (
    LLMChatOptions,
    LLMMessage,
    LLMResponse,
    LLMStreamOptions,
    StreamEvent,
    TextBlock,
    TokenUsage,
    ToolSecurityPolicy,
    ToolUseBlock,
)
from tests.helpers.fake_sandbox_provider import FakeSandboxProvider

from atlasos.actions import ActionCatalog, ActionPolicy, ActionRule
from atlasos.approvals.store import SQLiteApprovalStore
from atlasos.integrations.anycode_engine import AtlasContextEngine
from atlasos.integrations.deepseek import DeepSeekAdapter
from atlasos.ledger import SQLiteActionLedgerStore
from atlasos.sandbox import (
    ProjectSandboxBinding,
    ProjectSandboxManager,
    SandboxProjectConfig,
    SandboxWorkspaceTools,
)
from atlasos.sandbox.models import trusted_sandbox_spec
from atlasos.suspension.store import SQLiteSuspensionStore
from atlasos.tools import AtlasToolRuntime
from atlasos.tools.conversation import reset_current_task_id, set_current_task_id
from atlasos.trajectory.recorder import TrajectoryRecorder
from atlasos.workers import ScopedWorkerFactory, WorkerAssignment, WorkerStore

REPO_ROOT = Path(__file__).resolve().parents[1]


class ScriptedAdapter:
    """Smoke-only adapter; live UAT uses DeepSeek."""

    def __init__(self, responses: list[Any], counter: list[int]) -> None:
        self.responses = responses
        self.counter = counter
        self.name = "scripted"

    async def chat(self, messages: list[LLMMessage], options: LLMChatOptions) -> LLMResponse:
        del messages
        self.counter[0] += 1
        index = self.counter[0] - 1
        blocks = (
            self.responses[index]() if index < len(self.responses) else [TextBlock(text="done")]
        )
        return LLMResponse(
            id=f"resp-{index}",
            content=blocks,
            model=options.model,
            stop_reason="tool_use"
            if any(isinstance(block, ToolUseBlock) for block in blocks)
            else "end_turn",
            usage=TokenUsage(input_tokens=1, output_tokens=1),
        )

    def stream(self, messages: list[LLMMessage], options: LLMStreamOptions):
        async def _gen():
            response = await self.chat(messages, options)
            for block in response.content:
                if isinstance(block, ToolUseBlock):
                    yield StreamEvent(type="tool_use", data=block)
                elif isinstance(block, TextBlock):
                    yield StreamEvent(type="text", data=block.text)
            yield StreamEvent(type="done", data=response)

        return _gen()


def _register_provider(name: str, *, scripted: bool) -> None:
    if scripted:

        def read_and_fix() -> list[Any]:
            return [
                ToolUseBlock(
                    id="call-read",
                    name="file_read",
                    input={"path": "/workspace/project/csv_parser.py"},
                ),
                ToolUseBlock(
                    id="call-test",
                    name="bash",
                    input={"command": "python3 -m pytest -q", "cwd": "/workspace/project"},
                ),
            ]

        counter = [0]
        adapter = ScriptedAdapter([read_and_fix, lambda: [TextBlock(text="done")]], counter)

        async def factory(**_: Any) -> ScriptedAdapter:
            return adapter

        register_provider_factory(name, factory)
        return

    async def deepseek_factory(**_: Any) -> DeepSeekAdapter:
        adapter = DeepSeekAdapter.from_env()
        return adapter

    register_provider_factory(name, deepseek_factory)


def _coding_policy() -> ActionPolicy:
    allow = (
        "control.search",
        "control.dispatch",
        "filesystem.read",
        "filesystem.write",
        "shell.execute",
    )
    return ActionPolicy(
        name="uat-coding",
        default="DENY",
        rules=tuple(
            ActionRule(id=f"allow-{action}", decision="ALLOW", action=action) for action in allow
        ),
    )


def _copy_fixture(fixture: Path, workspace: Path) -> None:
    for entry in fixture.iterdir():
        if entry.name in {".git", "__pycache__", ".pytest_cache"}:
            continue
        target = workspace / entry.name
        if entry.is_dir():
            shutil.copytree(entry, target, ignore=shutil.ignore_patterns("__pycache__"))
        else:
            shutil.copy2(entry, target)


_RESERVED_WINDOWS_NAMES = frozenset(
    {"nul", "con", "prn", "aux", "com1", "com2", "com3", "com4", "lpt1", "lpt2", "lpt3"}
)


def _export_workspace(source: Path, target: Path) -> None:
    """Copy the sandbox workspace, excluding VCS/cache and Windows device names."""

    for item in source.rglob("*"):
        if not item.is_file():
            continue
        parts = set(item.relative_to(source).parts)
        if parts & {".git", "__pycache__", ".pytest_cache"}:
            continue
        if item.name.lower() in _RESERVED_WINDOWS_NAMES:
            continue
        destination = target / item.relative_to(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(item.read_bytes())


def _init_git(workspace: Path) -> None:
    """Give the agent a normal git working tree; ignore failure for non-git environments."""

    commands = (
        ["git", "init", "-q"],
        ["git", "-c", "user.email=uat@example.test", "-c", "user.name=AtlasUAT", "add", "-A"],
        [
            "git",
            "-c",
            "user.email=uat@example.test",
            "-c",
            "user.name=AtlasUAT",
            "commit",
            "-qm",
            "Initial fixture",
        ],
    )
    for command in commands:
        subprocess.run(
            command,
            cwd=workspace,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    run_id = f"uat-{uuid.uuid4().hex}"
    runs_root = args.runs_root.resolve()
    run_dir = runs_root / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    fixture = args.fixture.resolve()
    if not (fixture / "csv_parser.py").exists():
        raise SystemExit(f"fixture does not look like the UAT repo: {fixture}")

    task_id = f"{run_id}-task"
    worker_id = f"{run_id}-worker"
    provider_name = args.provider
    _register_provider(provider_name, scripted=args.scripted)

    store = FilesystemRunStore(run_dir / "trajectory")
    recorder = TrajectoryRecorder(store, run_id, agent_name="atlas-uat", model=args.model)
    suspension_store = SQLiteSuspensionStore(runs_root / "suspension.db")
    approval_store = SQLiteApprovalStore(runs_root / "approvals.db")
    ledger = SQLiteActionLedgerStore(runs_root / "ledger.db")
    idempotency = SQLiteToolIdempotencyStore(runs_root / "idempotency.db")

    parent = ExecutionContext(
        principal="user:uat",
        tenant_scope="uat",
        classification="internal",
        attributes={
            "run_id": run_id,
            "task_id": task_id,
            "worker_profile": "coding_worker",
            "operating_scope": "coding",
        },
    )
    worker_factory = ScopedWorkerFactory(
        model=args.model,
        provider=provider_name,
        store=WorkerStore(run_dir / "workers.json"),
        trajectory_recorder=recorder,
    )
    worker = worker_factory.create(
        parent_context=parent,
        parent_agent="uat-cli",
        assignment=WorkerAssignment(
            profile_id="coding_worker",
            task=(
                "The repository is mounted at /workspace/project. "
                "Sandbox commands execute on Windows; prefer portable Python or "
                "cmd-compatible commands over Unix-only commands such as find. "
                f"{args.task}"
            ),
        ),
        worker_id=worker_id,
        task_id=task_id,
    )

    sandbox_provider = FakeSandboxProvider(run_dir / "sandbox", shell="git-bash")
    sandbox_config = SandboxProjectConfig(
        provider_name="fake", image="fake@sha256:0123456789abcdef"
    )
    manager = ProjectSandboxManager(
        sandbox_provider, sandbox_config, run_id=run_id, trajectory_recorder=recorder
    )
    spec = trusted_sandbox_spec(
        run_id=run_id,
        task_id=task_id,
        worker_id=worker.worker_id,
        context=worker.execution_context,
        config=sandbox_config,
    )
    created = await sandbox_provider.create(spec)
    assert created.handle is not None
    binding = ProjectSandboxBinding(
        run_id=run_id,
        task_id=task_id,
        worker_id=worker.worker_id,
        provider_name="fake",
        sandbox_handle=created.handle,
        workspace_root="/workspace/project",
        source_commit="0" * 40,
        source_digest="sha256:uat",
        bundle_digest="sha256:uat",
        state="READY",
        created_at=datetime.now(UTC),
    )
    manager.bindings[task_id] = binding
    manager._record_lifecycle(binding, "READY", occurred_at=binding.created_at)  # noqa: SLF001
    workspace = sandbox_provider.workspaces[created.handle.id] / "workspace" / "project"
    _copy_fixture(fixture, workspace)
    _init_git(workspace)
    sandbox_tools = SandboxWorkspaceTools(manager, task_id, sandbox_provider)

    runtime = AtlasToolRuntime(
        run_id,
        state_path=run_dir / "tool_state.json",
        conversation_path=run_dir / "tool_conversations.json",
        external_tools=(),
        action_policy=_coding_policy(),
        action_catalog=ActionCatalog(),
        action_ledger=ledger,
        idempotency_store=idempotency,
        suspension_store=suspension_store,
        approval_store=approval_store,
        sandbox_tools=sandbox_tools,
        trajectory_recorder=recorder,
        runtime_instance_id=f"uat-{run_id}",
    )
    agent_config = worker.agent_config.model_copy(
        update={
            "tool_security": ToolSecurityPolicy(workspace_root="/workspace/project"),
            "max_turns": args.max_turns,
        }
    )
    engine = AtlasContextEngine(OrchestratorConfig(), tool_runtime=runtime)
    team = engine.create_team("uat", TeamConfig(name="uat", agents=[agent_config]))
    task = TaskSpec(
        title="UAT coding task",
        description=(
            "The repository is mounted at /workspace/project. "
            "Sandbox commands execute on Windows; prefer portable Python or "
            "cmd-compatible commands over Unix-only commands such as find. "
            f"{args.task}"
        ),
        assignee=worker.worker_name,
    )
    token = set_current_task_id(task_id)
    try:
        result = await engine.run_tasks(team, [task])
    finally:
        reset_current_task_id(token)
        await engine.close()
        await idempotency.teardown()

    agent_result = result.agent_results.get(worker.worker_name)
    final_output = agent_result.output if agent_result is not None else ""
    tool_calls = [
        {
            "tool_name": record.tool_name,
            "input": record.input,
            "output": record.output[:4000],
        }
        for agent_result_item in result.agent_results.values()
        for record in agent_result_item.tool_calls
    ]
    export = run_dir / "workspace"
    _export_workspace(workspace, export)
    session = {
        "run_id": run_id,
        "mode": "coding",
        "task": args.task,
        "fixture": str(fixture),
        "workspace_export": str(export),
        "provider": provider_name,
        "model": args.model,
        "worker_profile": "coding_worker",
        "worker_id": worker.worker_id,
        "task_id": task_id,
        "run_dir": str(run_dir),
        "sandbox_provider": "fake-local",
        "status": "completed" if result.success else "failed",
        "final_output": final_output,
        "tool_calls": tool_calls,
    }
    (run_dir / "session.json").write_text(json.dumps(session, indent=2), encoding="utf-8")
    recorder.record_run_terminal(
        status=session["status"],
        task_count=1,
        final_output_digest=None,
        final_output_size=len(final_output),
        occurred_at=datetime.now(UTC),
    )
    return session


def main() -> None:
    parser = argparse.ArgumentParser(prog="atlas-uat")
    parser.add_argument(
        "task",
        help="natural-language task for the UAT coding session",
    )
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--runs-root", type=Path, default=REPO_ROOT / "experiments" / "runs")
    parser.add_argument("--provider", default="deepseek")
    parser.add_argument("--model", default="deepseek-chat")
    parser.add_argument("--max-turns", type=int, default=12)
    parser.add_argument("--scripted", action="store_true")
    args = parser.parse_args()
    session = asyncio.run(_run(args))
    print(json.dumps(session, ensure_ascii=False))


if __name__ == "__main__":
    main()
