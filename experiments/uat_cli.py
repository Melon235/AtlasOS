"""Thin UAT CLI adapter over the frozen AtlasOS V2 runtime.

This is a user-facing test surface, not a new runtime. It parses a natural-language task, constructs
the trusted Atlas Sandbox provider selected by Host configuration, stages the fixture into a
one-task M5 binding, runs one engine task, and exports the remote workspace for external
acceptance. It owns no scheduling, policy, execution, approval, or recovery logic.

Live coding UAT requires a real AnyCode-compatible provider (Daytona). The deterministic local
provider is reachable only through the explicitly scripted smoke path; it is never selected as a
fallback when the real provider or its credentials are unavailable.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from anycode import OrchestratorConfig, TaskSpec, TeamConfig
from anycode.identity.context import ExecutionContext
from anycode.plugins.registry import register_provider_factory
from anycode.runstore.store import FilesystemRunStore
from anycode.security.redaction import safe_exception_message
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
from dotenv import load_dotenv

from atlasos.actions import ActionCatalog, ActionPolicy, ActionRule
from atlasos.approvals.store import SQLiteApprovalStore
from atlasos.config import AtlasConfig, load_config
from atlasos.integrations.anycode_engine import AtlasContextEngine
from atlasos.integrations.deepseek import DeepSeekAdapter
from atlasos.ledger import SQLiteActionLedgerStore
from atlasos.sandbox import (
    REAL_SANDBOX_PROVIDERS,
    ProjectSandboxManager,
    SandboxCapabilityError,
    SandboxCredentialError,
    SandboxExecutionError,
    SandboxFileTransferError,
    SandboxIsolationError,
    SandboxLostError,
    SandboxProjectConfig,
    SandboxProviderError,
    SandboxProvisionError,
    SandboxWorkspaceTools,
    build_sandbox_provider,
    capability_digest,
    capability_summary,
    export_patch,
    export_workspace,
    is_real_sandbox_provider,
    require_capabilities,
    require_real_sandbox_provider,
)
from atlasos.sandbox.validation import (
    probe_host_separation,
    probe_network_none,
    require_host_separation,
)
from atlasos.suspension.store import SQLiteSuspensionStore
from atlasos.tools import AtlasToolRuntime
from atlasos.tools.conversation import reset_current_task_id, set_current_task_id
from atlasos.trajectory.models import derive_sandbox_binding_id
from atlasos.trajectory.recorder import TrajectoryRecorder
from atlasos.workers import ScopedWorkerFactory, WorkerAssignment, WorkerStore

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = REPO_ROOT / "experiments" / "configs" / "v2-uat.yaml"
_IGNORED_FIXTURE_PARTS = frozenset({".git", "__pycache__", ".pytest_cache"})
_FAKE_IMAGE = "fake@sha256:0123456789abcdef"


class _UATFailure(RuntimeError):
    def __init__(self, failure_class: str, stage: str, message: str) -> None:
        super().__init__(message)
        self.failure_class = failure_class
        self.stage = stage
        self.message = message


@dataclass
class _SandboxMetrics:
    provider_name: str = ""
    provider_is_real: bool = False
    provider_capabilities: dict[str, Any] | None = None
    capabilities_digest: str | None = None
    sandbox_created: bool = False
    sandbox_ready: bool = False
    sandbox_logical_id: str | None = None
    binding_id: str | None = None
    sandbox_destroyed: bool = False
    workspace_root: str = "/workspace/project"
    network_policy_requested: str = "none"
    network_policy_verified: str = "UNKNOWN"
    same_binding_all_project_tools: bool = False
    commands_executed: int = 0
    files_uploaded: int = 0
    files_read: int = 0
    files_written: int = 0
    host_fixture_digest_before: str | None = None
    host_fixture_digest_after: str | None = None
    host_fixture_digest_unchanged: bool = True
    unexpected_host_writes: int = 0
    host_workspace_fallbacks: int = 0
    sandbox_recreations: int = 0
    host_only_sentinel_visible: bool | None = None
    sandbox_only_sentinel_visible_on_host: bool | None = None
    sandbox_only_sentinel_read: bool = False
    retain_on_failure: bool = False
    retained_sandbox_id: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        value = {
            "provider_name": self.provider_name,
            "provider_is_real": self.provider_is_real,
            "provider_capabilities": self.provider_capabilities,
            "capabilities_digest": self.capabilities_digest,
            "sandbox_created": self.sandbox_created,
            "sandbox_ready": self.sandbox_ready,
            "sandbox_logical_id": self.sandbox_logical_id,
            "binding_id": self.binding_id,
            "sandbox_destroyed": self.sandbox_destroyed,
            "workspace_root": self.workspace_root,
            "network_policy_requested": self.network_policy_requested,
            "network_policy_verified": self.network_policy_verified,
            "same_binding_all_project_tools": self.same_binding_all_project_tools,
            "commands_executed": self.commands_executed,
            "files_uploaded": self.files_uploaded,
            "files_read": self.files_read,
            "files_written": self.files_written,
            "host_fixture_digest_before": self.host_fixture_digest_before,
            "host_fixture_digest_after": self.host_fixture_digest_after,
            "host_fixture_digest_unchanged": self.host_fixture_digest_unchanged,
            "unexpected_host_writes": self.unexpected_host_writes,
            "host_workspace_fallbacks": self.host_workspace_fallbacks,
            "sandbox_recreations": self.sandbox_recreations,
            "host_only_sentinel_visible": self.host_only_sentinel_visible,
            "sandbox_only_sentinel_visible_on_host": self.sandbox_only_sentinel_visible_on_host,
            "sandbox_only_sentinel_read": self.sandbox_only_sentinel_read,
            "retain_on_failure": self.retain_on_failure,
            "retained_sandbox_id": self.retained_sandbox_id,
        }
        value.update(self.extra)
        return value


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

        def read_and_test() -> list[Any]:
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
        adapter = ScriptedAdapter([read_and_test, lambda: [TextBlock(text="done")]], counter)

        async def factory(**_: Any) -> ScriptedAdapter:
            return adapter

        register_provider_factory(name, factory)
        return

    async def deepseek_factory(**_: Any) -> DeepSeekAdapter:
        return DeepSeekAdapter.from_env()

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


def _fixture_digest(path: Path) -> str:
    digest = hashlib.sha256()
    for item in sorted(path.rglob("*")):
        if (
            item.is_file()
            and not _IGNORED_FIXTURE_PARTS.intersection(item.relative_to(path).parts)
            and ".pytest_cache" not in item.parts
        ):
            digest.update(item.relative_to(path).as_posix().encode("utf-8"))
            digest.update(item.read_bytes())
    return digest.hexdigest()


def _stage_fixture_repo(fixture: Path, destination: Path) -> Path:
    """Copy the Host fixture into a clean one-commit staging repo for M5 provisioning."""

    destination.mkdir(parents=True, exist_ok=False)
    for entry in fixture.iterdir():
        if entry.name in _IGNORED_FIXTURE_PARTS:
            continue
        target = destination / entry.name
        if entry.is_dir():
            shutil.copytree(
                entry,
                target,
                ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"),
            )
        else:
            shutil.copy2(entry, target)
    commands = (
        ["git", "init", "-q"],
        ["git", "config", "core.autocrlf", "false"],
        ["git", "config", "core.safecrlf", "false"],
        ["git", "add", "-A"],
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
        result = subprocess.run(
            command,
            cwd=destination,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        if result.returncode != 0:
            raise _UATFailure(
                "ENVIRONMENT_FAILURE",
                "fixture_stage",
                "cannot stage fixture repository: "
                f"{result.stderr.strip() or result.stdout.strip()}",
            )
    return destination


def _load_sandbox_config(args: argparse.Namespace) -> AtlasConfig:
    path = args.config or (DEFAULT_CONFIG if DEFAULT_CONFIG.is_file() else None)
    if path is not None:
        return load_config(path)
    return AtlasConfig()


def _select_provider(args: argparse.Namespace, config: AtlasConfig) -> str:
    if args.scripted:
        if args.require_real_sandbox:
            raise _UATFailure(
                "ENVIRONMENT_FAILURE",
                "configuration",
                "--scripted cannot be combined with --require-real-sandbox",
            )
        return "fake"
    name = args.sandbox_provider or config.sandbox.provider
    if name not in REAL_SANDBOX_PROVIDERS:
        raise _UATFailure(
            "SANDBOX_PROVIDER_FAILURE",
            "configuration",
            "live UAT requires a real Atlas Sandbox provider; "
            f"configured provider={name!r}. The fake provider is scripted-smoke-only.",
        )
    return name


def _project_config(
    config: AtlasConfig, provider_name: str, *, scripted: bool
) -> SandboxProjectConfig:
    sandbox = config.sandbox
    if scripted or provider_name == "fake":
        return SandboxProjectConfig(
            provider_name="fake",
            image=_FAKE_IMAGE,
            workspace_root="/workspace/project",
            max_bundle_bytes=sandbox.max_bundle_bytes,
        )
    return SandboxProjectConfig(
        provider_name=provider_name,
        image=sandbox.image,
        snapshot=sandbox.snapshot,
        workspace_root=sandbox.workspace_root,
        max_bundle_bytes=sandbox.max_bundle_bytes,
    )


def _build_provider(args: argparse.Namespace, provider_name: str, run_dir: Path) -> Any:
    if provider_name == "fake":
        # The deterministic provider exists only for the explicit scripted smoke path. Importing it
        # lazily keeps the live path free of test-only sandbox code.
        from tests.helpers.fake_sandbox_provider import FakeSandboxProvider

        shell = "git-bash" if os.name == "nt" else "cmd"
        return FakeSandboxProvider(run_dir / "sandbox", shell=shell)
    return build_sandbox_provider(provider_name)


def _classify_exception(error: BaseException) -> str:
    if isinstance(error, SandboxCredentialError):
        return "CREDENTIAL_BLOCKED"
    if isinstance(error, SandboxIsolationError):
        return "SANDBOX_ISOLATION_FAILURE"
    if isinstance(error, SandboxFileTransferError):
        return "SANDBOX_FILE_TRANSFER_FAILURE"
    if isinstance(error, SandboxExecutionError):
        return "SANDBOX_EXECUTION_FAILURE"
    if isinstance(error, SandboxProvisionError):
        return "SANDBOX_PROVISION_FAILURE"
    if isinstance(error, SandboxProviderError):
        return "SANDBOX_PROVIDER_FAILURE"
    if isinstance(error, SandboxCapabilityError):
        return "SANDBOX_CAPABILITY_FAILURE"
    if isinstance(error, SandboxLostError):
        return "SANDBOX_PROVIDER_FAILURE"
    if isinstance(error, (OSError, subprocess.SubprocessError)):
        return "ENVIRONMENT_FAILURE"
    return "RUNTIME_FAILURE"


def _failure_from_exception(error: BaseException, stage: str) -> _UATFailure:
    if isinstance(error, _UATFailure):
        return error
    return _UATFailure(_classify_exception(error), stage, safe_exception_message(error))


def _record_terminal(
    recorder: TrajectoryRecorder | None,
    *,
    status: str,
    final_output: str,
) -> None:
    if recorder is None:
        return
    with contextlib.suppress(Exception):  # evidence is optional
        recorder.record_run_terminal(
            status=status,
            task_count=1,
            final_output_digest=None,
            final_output_size=len(final_output),
            occurred_at=datetime.now(UTC),
        )


async def _run(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    run_id = f"uat-{uuid.uuid4().hex}"
    runs_root = args.runs_root.resolve()
    run_dir = runs_root / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    fixture = args.fixture.resolve()
    if not (fixture / "csv_parser.py").exists():
        raise SystemExit(f"fixture does not look like the UAT repo: {fixture}")

    load_dotenv(REPO_ROOT / ".env", override=False)
    config = _load_sandbox_config(args)
    provider_name = ""
    provider: Any = None
    manager: ProjectSandboxManager | None = None
    binding: Any = None
    sandbox_tools: SandboxWorkspaceTools | None = None
    engine: AtlasContextEngine | None = None
    idempotency: SQLiteToolIdempotencyStore | None = None
    recorder: TrajectoryRecorder | None = None
    failure: _UATFailure | None = None
    agent_success = False
    final_output = ""
    tool_calls: list[dict[str, Any]] = []
    workspace_export: Path | None = None
    patch_digest: str | None = None
    patch_changed_files = 0
    control_commands = 0
    control_uploads = 0
    control_reads = 0
    host_digest_before = _fixture_digest(fixture)
    metrics = _SandboxMetrics()
    metrics.host_fixture_digest_before = host_digest_before
    metrics.retain_on_failure = bool(args.retain_on_failure)

    task_id = f"{run_id}-task"
    worker_id = f"{run_id}-worker"
    worker: Any = None

    try:
        if not args.scripted and not os.environ.get("DEEPSEEK_API_KEY"):
            raise _UATFailure(
                "CREDENTIAL_BLOCKED",
                "configuration",
                "DEEPSEEK_API_KEY is not configured in the Host environment",
            )
        provider_name = _select_provider(args, config)
        metrics.provider_name = provider_name
        provider = _build_provider(args, provider_name, run_dir)
        capabilities = provider.capabilities()
        metrics.provider_capabilities = capability_summary(capabilities)
        metrics.capabilities_digest = capability_digest(capabilities)
        metrics.provider_is_real = is_real_sandbox_provider(provider, expected_name=provider_name)
        if provider_name != "fake":
            require_real_sandbox_provider(provider, expected_name=provider_name)
        require_capabilities(capabilities)
        health = await provider.health()
        if health.status == "unavailable":
            raise SandboxProviderError(f"sandbox provider is unavailable: {health.message}")

        project_config = _project_config(config, provider_name, scripted=args.scripted)
        metrics.workspace_root = project_config.workspace_root
        _register_provider(args.provider, scripted=args.scripted)

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
            provider=args.provider,
            store=WorkerStore(run_dir / "workers.json"),
            trajectory_recorder=recorder,
        )
        environment_note = (
            "The repository is at /workspace/project inside a remote Linux sandbox. "
            "Use Python 3 and portable POSIX shell commands. Do not assume access to the Host."
            if metrics.provider_is_real
            else "The repository is at /workspace/project. Use portable Python commands."
        )
        worker = worker_factory.create(
            parent_context=parent,
            parent_agent="uat-cli",
            assignment=WorkerAssignment(
                profile_id="coding_worker",
                task=f"{environment_note} {args.task}",
            ),
            worker_id=worker_id,
            task_id=task_id,
        )

        manager = ProjectSandboxManager(
            provider,
            project_config,
            run_id=run_id,
            trajectory_recorder=recorder,
        )
        source_repo = _stage_fixture_repo(fixture, run_dir / "source")
        binding = await manager.ensure_binding(
            task_id=task_id,
            source_repo=str(source_repo),
            context=worker.execution_context,
            worker_id=worker.worker_id,
        )
        metrics.sandbox_created = manager.create_count > 0
        metrics.sandbox_ready = binding.state == "READY"
        metrics.sandbox_logical_id = binding.sandbox_handle.id
        metrics.binding_id = derive_sandbox_binding_id(
            run_id=run_id,
            task_id=task_id,
            worker_id=worker.worker_id,
            provider_name=provider_name,
            sandbox_handle_id=binding.sandbox_handle.id,
        )

        metrics.network_policy_verified = await probe_network_none(provider, binding.sandbox_handle)
        if metrics.network_policy_verified == "FAIL":
            raise SandboxIsolationError(
                "sandbox requested network='none' but the empirical egress probe succeeded"
            )
        control_commands += 1
        separation = await probe_host_separation(provider, binding.sandbox_handle, host_dir=run_dir)
        require_host_separation(separation)
        control_commands += 1
        control_uploads += 2  # sandbox sentinel + workspace export helper
        control_reads += 2  # sentinel read-back + workspace archive
        metrics.host_only_sentinel_visible = separation.host_only_visible
        metrics.sandbox_only_sentinel_visible_on_host = separation.sandbox_only_visible_on_host
        metrics.sandbox_only_sentinel_read = separation.sandbox_only_read_back

        sandbox_tools = SandboxWorkspaceTools(manager, task_id, provider)
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
            description=f"{environment_note} {args.task}",
            assignee=worker.worker_name,
        )
        token = set_current_task_id(task_id)
        try:
            result = await engine.run_tasks(team, [task])
        finally:
            reset_current_task_id(token)
        agent_success = result.success
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

        patch = await export_patch(
            provider,
            binding.sandbox_handle,
            base_commit=binding.source_commit,
            workspace_root=binding.workspace_root,
        )
        patch_digest = patch.patch_digest
        patch_changed_files = patch.changed_file_count
        (run_dir / "workspace.patch").write_text(patch.patch_text, encoding="utf-8")
        workspace_export = run_dir / "workspace"
        await export_workspace(
            provider,
            binding.sandbox_handle,
            destination=workspace_export,
            workspace_root=binding.workspace_root,
        )
    except BaseException as error:  # noqa: BLE001 - converted into a classified UAT failure
        failure = _failure_from_exception(error, "session")
        if not isinstance(error, Exception):
            raise
    finally:
        host_digest_after = _fixture_digest(fixture)
        metrics.host_fixture_digest_after = host_digest_after
        metrics.host_fixture_digest_unchanged = host_digest_before == host_digest_after

        if engine is not None:
            try:
                await engine.close()
            except Exception as error:  # noqa: BLE001
                failure = failure or _failure_from_exception(error, "engine_close")
        if idempotency is not None:
            try:
                await idempotency.teardown()
            except Exception as error:  # noqa: BLE001
                failure = failure or _failure_from_exception(error, "idempotency_close")

        retained = False
        if manager is not None and binding is not None:
            if args.retain_on_failure and failure is not None:
                retained = True
                metrics.retained_sandbox_id = binding.sandbox_handle.id
            else:
                try:
                    metrics.sandbox_destroyed = await manager.close(task_id)
                except Exception as error:  # noqa: BLE001
                    failure = failure or _failure_from_exception(error, "sandbox_cleanup")
        elif manager is not None:
            metrics.sandbox_destroyed = manager.destroyed_count > 0
        if not retained and failure is None and not metrics.sandbox_destroyed:
            failure = _UATFailure(
                "SANDBOX_PROVIDER_FAILURE",
                "sandbox_cleanup",
                "sandbox was not confirmed destroyed",
            )

        if manager is not None:
            metrics.commands_executed = manager.provision_commands + control_commands
            metrics.files_uploaded = manager.provision_uploads + control_uploads
            metrics.files_read = control_reads
            metrics.sandbox_ready = metrics.sandbox_ready or bool(
                manager.bindings.get(task_id) is not None
            )
            metrics.sandbox_recreations = max(0, manager.create_count - 1)
        if sandbox_tools is not None:
            metrics.commands_executed += sandbox_tools.command_count
            metrics.files_read += sandbox_tools.file_read_count
            metrics.files_written = sandbox_tools.file_write_count + sandbox_tools.file_edit_count
            expected_handle = binding.sandbox_handle.id if binding is not None else None
            metrics.same_binding_all_project_tools = bool(
                expected_handle and sandbox_tools.used_handle_ids == {expected_handle}
            )

        status = "completed" if failure is None and agent_success else "failed"
        _record_terminal(recorder, status=status, final_output=final_output)

        session: dict[str, Any] = {
            "run_id": run_id,
            "mode": "coding",
            "task": args.task,
            "fixture": str(fixture),
            "workspace_export": str(workspace_export) if workspace_export else None,
            "provider": args.provider,
            "model": args.model,
            "worker_profile": "coding_worker",
            "worker_id": worker.worker_id if worker is not None else None,
            "task_id": task_id,
            "run_dir": str(run_dir),
            "sandbox_provider": provider_name,
            "status": status,
            "agent_success": agent_success,
            "final_output": final_output,
            "tool_calls": tool_calls,
            "patch_digest": patch_digest,
            "patch_changed_files": patch_changed_files,
            "failure_class": failure.failure_class if failure else None,
            "failure_stage": failure.stage if failure else None,
            "failure_message": failure.message if failure else None,
            "sandbox": metrics.as_dict(),
        }
        (run_dir / "session.json").write_text(json.dumps(session, indent=2), encoding="utf-8")

    exit_code = 0 if failure is None else 1
    return session, exit_code


def main() -> None:
    parser = argparse.ArgumentParser(prog="atlas-uat")
    parser.add_argument("task", help="natural-language task for the UAT coding session")
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--runs-root", type=Path, default=REPO_ROOT / "experiments" / "runs")
    parser.add_argument("--provider", default="deepseek", help="LLM provider")
    parser.add_argument("--model", default="deepseek-chat")
    parser.add_argument("--max-turns", type=int, default=12)
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--sandbox-provider", choices=sorted(REAL_SANDBOX_PROVIDERS), default=None)
    parser.add_argument("--require-real-sandbox", action="store_true")
    parser.add_argument("--retain-on-failure", action="store_true")
    parser.add_argument("--scripted", action="store_true")
    args = parser.parse_args()
    session, exit_code = asyncio.run(_run(args))
    print(json.dumps(session, ensure_ascii=False))
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
