"""Stage G3-1 agent-loop integration: a known bash failure must not end the worker.

The stack is the production one (M1 tool runtime + M3 gate + M4 ledger/bridge + M5 sandbox tools +
M6 runner seam + G1 handoff + G2a budget hook). Only two things are faked: the model provider and
the sandbox provider. Every tool call therefore still goes through AnyCode's real ``ToolExecutor``
and the real ``SandboxWorkspaceTools.bash`` mapping, which is exactly where G3-I01 lived.

Covered:

* ``test_completed_error_keeps_worker_alive`` — Turn 1 bash exits 2, Turn 2 sees the error and
  repairs, Turn 3 finishes. One execution, no replay, budget hook still admits every request.
* ``test_parallel_batch_mixes_completed_error_and_success`` — one failing bash and one successful
  read in the same batch; both results stay correlated and the worker continues.
* ``test_unknown_outcome_still_stops_the_worker`` — the outcome was not observed, so the legacy
  ``side_effect_unknown`` stop must remain.
"""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path
from typing import Any

from anycode import AgentConfig, OrchestratorConfig, TaskSpec, TeamConfig
from anycode.identity.context import ExecutionContext
from anycode.plugins.registry import register_provider_factory
from anycode.tools.idempotency import InMemoryToolIdempotencyStore
from anycode.types import (
    LLMChatOptions,
    LLMMessage,
    LLMResponse,
    LLMStreamOptions,
    StreamEvent,
    TextBlock,
    TokenUsage,
    ToolResultBlock,
    ToolUseBlock,
)
from tests.helpers.fake_sandbox_provider import FakeSandboxProvider

from atlasos.actions import (
    DEFAULT_MAPPINGS,
    ActionCatalog,
    ActionPolicy,
    ActionRule,
    StaticActionReviewer,
)
from atlasos.approvals.store import SQLiteApprovalStore
from atlasos.budget import RunBudgetState
from atlasos.handoff import (
    HANDOFF_READ_ACTION_MAPPING,
    HANDOFF_READ_TOOL,
    HandoffNode,
    HandoffRuntime,
)
from atlasos.integrations.anycode_engine import AtlasContextEngine
from atlasos.ledger import SQLiteActionLedgerStore
from atlasos.sandbox import ProjectSandboxManager, SandboxProjectConfig, SandboxWorkspaceTools
from atlasos.sandbox.tools import TOOL_ERROR_MODE_CORRECTED, TOOL_ERROR_MODE_LEGACY
from atlasos.suspension.store import SQLiteSuspensionStore
from atlasos.tools import AtlasToolRuntime
from atlasos.tools.conversation import reset_current_task_id, set_current_task_id

FAILING_COMMAND = (
    "python3 -c \"import sys; sys.stderr.write('boom: missing config\\n'); sys.exit(2)\""
)
WRITE_THEN_FAIL_COMMAND = "python3 -c \"open('side.txt','a').write('x\\n'); raise SystemExit(1)\""
PROVIDER_NAME = "g31-scripted"


class _Scripted:
    """Deterministic provider that records the exact messages each model call saw."""

    def __init__(self, script: list[list[Any]], seen: list[list[LLMMessage]]) -> None:
        self._script = script
        self._seen = seen
        self.name = "scripted"

    def _next(self, messages: list[LLMMessage], options: Any) -> LLMResponse:
        self._seen.append(list(messages))
        index = len(self._seen) - 1
        blocks: list[Any] = (
            self._script[index] if index < len(self._script) else [TextBlock(text="done")]
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

    async def chat(self, messages: list[LLMMessage], options: LLMChatOptions) -> LLMResponse:
        return self._next(messages, options)

    def stream(self, messages: list[LLMMessage], options: LLMStreamOptions):
        async def _gen():
            response = self._next(messages, options)
            for block in response.content:
                if isinstance(block, ToolUseBlock):
                    yield StreamEvent(type="tool_use", data=block)
                elif isinstance(block, TextBlock):
                    yield StreamEvent(type="text", data=block.text)
            yield StreamEvent(type="done", data=response)

        return _gen()


def _register(script: list[list[Any]], seen: list[list[LLMMessage]]) -> str:
    name = f"{PROVIDER_NAME}-{uuid.uuid4().hex}"

    async def factory(**kwargs: Any) -> _Scripted:
        del kwargs
        return _Scripted(script, seen)

    register_provider_factory(name, factory)
    return name


def _make_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    repo = root / "host-project"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=repo, check=True)
    return repo


def _policy() -> ActionPolicy:
    rules = tuple(
        ActionRule(id=f"allow-{action}", decision="ALLOW", action=action)
        for action in ("filesystem.read", "filesystem.write", "shell.execute", "external.read")
    )
    return ActionPolicy(name="g31", default="DENY", rules=rules)


async def _stack(tmp_path: Path, *, mode: str) -> dict[str, Any]:
    repo = _make_repo(tmp_path / "repo")
    provider = FakeSandboxProvider(tmp_path / "provider")
    config = SandboxProjectConfig(
        provider_name="fake",
        image="fake@sha256:0123456789abcdef",
    )
    manager = ProjectSandboxManager(provider, config, run_id="run-g31")
    task_id = "task-g31"
    binding = await manager.ensure_binding(
        task_id=task_id,
        source_repo=str(repo),
        context=ExecutionContext(principal="user:test", attributes={"run_id": "run-g31"}),
        worker_id="w1",
    )
    tools = SandboxWorkspaceTools(manager, task_id, provider, tool_error_mode=mode)
    handoff = HandoffRuntime.create(
        run_dir=tmp_path / "handoff",
        run_id="run-g31",
        nodes=[HandoffNode(key=task_id, title="task-g31")],
        consumer_keys={},
        mode="dual",
    )
    runtime = AtlasToolRuntime(
        "run-g31",
        state_path=tmp_path / "tool_state.json",
        conversation_path=tmp_path / "conversation.json",
        external_tools=handoff.external_tools(),
        action_policy=_policy(),
        action_catalog=ActionCatalog(DEFAULT_MAPPINGS + (HANDOFF_READ_ACTION_MAPPING,)),
        action_reviewer=StaticActionReviewer("CONSISTENT"),
        action_ledger=SQLiteActionLedgerStore(tmp_path / "ledger.db"),
        idempotency_store=InMemoryToolIdempotencyStore(),
        suspension_store=SQLiteSuspensionStore(tmp_path / "runtime.db"),
        approval_store=SQLiteApprovalStore(tmp_path / "approvals.db"),
        runtime_instance_id="runtime-g31",
        sandbox_tools=tools,
    )
    return {
        "repo": repo,
        "provider": provider,
        "binding": binding,
        "task_id": task_id,
        "tools": tools,
        "handoff": handoff,
        "runtime": runtime,
    }


async def _run(
    stack: dict[str, Any],
    *,
    script: list[list[Any]],
    budget: RunBudgetState | None = None,
) -> tuple[Any, list[list[LLMMessage]]]:
    seen: list[list[LLMMessage]] = []
    provider_name = _register(script, seen)
    engine = AtlasContextEngine(
        OrchestratorConfig(),
        tool_runtime=stack["runtime"],
        budget_state=budget,
    )
    context = ExecutionContext(
        principal="user:test",
        tenant_scope="tenant-g31",
        classification="internal",
        attributes={
            "run_id": "run-g31",
            "task_id": stack["task_id"],
            "worker_id": "w1",
            "profile_id": "coding_worker",
            "worker_profile": "coding_worker",
            "operating_scope": "coding",
        },
    )
    agent = AgentConfig(
        name="worker",
        model="fake-model",
        provider=provider_name,
        system_prompt="worker",
        execution_context=context,
        max_tokens=256,
    )
    team = engine.create_team("g31", TeamConfig(name="g31", agents=[agent]))
    spec = TaskSpec(title="Task", description="fix the module", assignee="worker")
    # Production sets the trusted task id from the runtime's task lifecycle event
    # (``atlasos/runtime.py``); the M6 snapshot hook reads it from the context var.
    token = set_current_task_id(stack["task_id"])
    try:
        result = await engine.run_tasks(team, [spec])
    finally:
        reset_current_task_id(token)
        await engine.close()
    return result, seen


def _blocks(message: LLMMessage) -> list[Any]:
    content = message.content
    if isinstance(content, str):
        return [TextBlock(text=content)]
    return list(content)


def _tool_results(messages: list[LLMMessage]) -> dict[str, ToolResultBlock]:
    found: dict[str, ToolResultBlock] = {}
    for message in messages:
        for block in _blocks(message):
            if isinstance(block, ToolResultBlock):
                found[block.tool_use_id] = block
    return found


def _transcript(messages: list[LLMMessage]) -> str:
    parts: list[str] = []
    for message in messages:
        for block in _blocks(message):
            if isinstance(block, ToolResultBlock):
                parts.append(str(block.content))
            elif isinstance(block, TextBlock):
                parts.append(block.text)
    return "\n".join(parts)


async def test_completed_error_keeps_worker_alive(tmp_path: Path) -> None:
    stack = await _stack(tmp_path, mode=TOOL_ERROR_MODE_CORRECTED)
    provider = stack["provider"]
    handle = stack["binding"].sandbox_handle
    project = provider.workspaces[handle.id] / "workspace" / "project"
    executes_before = provider.execute_calls
    budget = RunBudgetState(limit_tokens=120_000)

    script = [
        [ToolUseBlock(id="call-1", name="bash", input={"command": WRITE_THEN_FAIL_COMMAND})],
        [ToolUseBlock(id="call-2", name="bash", input={"command": FAILING_COMMAND})],
        [
            ToolUseBlock(
                id="call-3",
                name="file_write",
                input={"path": "/workspace/project/app.py", "content": "VALUE = 2\n"},
            )
        ],
        [TextBlock(text="fixed")],
    ]
    result, seen = await _run(stack, script=script, budget=budget)

    # 1. Turn 2 and Turn 3 really happened: the failing command did not end the run.
    assert len(seen) == 4
    worker = result.agent_results["worker"]
    # 3. The stop reason is no longer side_effect_unknown.
    assert worker.stop_reason is not None
    assert worker.stop_reason.code == "success"
    assert worker.success is True

    # 2. Each following model input contains the previous turn's error text and exit code.
    second_turn = _tool_results(seen[1])
    assert set(second_turn) == {"call-1"}
    assert second_turn["call-1"].is_error is True
    assert "code 1" in str(second_turn["call-1"].content)
    third_turn = _tool_results(seen[2])
    assert set(third_turn) == {"call-1", "call-2"}
    assert third_turn["call-2"].is_error is True
    assert "boom: missing config" in str(third_turn["call-2"].content)
    assert "exit code 2" in str(third_turn["call-2"].content)

    # 4/5. Bash ran exactly as often as the model asked: no framework replay.
    assert provider.execute_calls - executes_before == 2
    assert (project / "side.txt").read_text(encoding="utf-8") == "x\n"

    # 6. The G2a run budget hook still admitted every request of the longer run.
    assert budget.requests_started == 4
    assert budget.requests_completed == 4
    assert budget.admission_rejections == 0

    # 7. The G1 handoff reader is still registered in the same shared registry.
    assert stack["handoff"].mode == "dual"
    assert stack["runtime"].registry.get(HANDOFF_READ_TOOL) is not None

    # 8. The M6 conversation snapshot kept the error feedback the model saw.
    snapshot = stack["runtime"].conversation_store.load(stack["task_id"])
    assert snapshot is not None
    assert "boom: missing config" in _transcript(
        [LLMMessage.model_validate(item) for item in snapshot]
    )

    # The model's repair landed in the sandbox, not on the host.
    assert (project / "app.py").read_text(encoding="utf-8") == "VALUE = 2\n"
    assert (stack["repo"] / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"


async def test_legacy_mode_still_stops_on_the_same_command(tmp_path: Path) -> None:
    """Control: the pre-G3-1 classification reproduces the G3-I01 termination."""

    stack = await _stack(tmp_path, mode=TOOL_ERROR_MODE_LEGACY)
    budget = RunBudgetState(limit_tokens=120_000)
    script = [
        [ToolUseBlock(id="call-1", name="bash", input={"command": FAILING_COMMAND})],
        [TextBlock(text="should not be reached")],
    ]
    result, seen = await _run(stack, script=script, budget=budget)

    assert len(seen) == 1
    worker = result.agent_results["worker"]
    assert worker.stop_reason is not None
    assert worker.stop_reason.code == "side_effect_unknown"
    assert result.success is False


async def test_parallel_batch_mixes_completed_error_and_success(tmp_path: Path) -> None:
    stack = await _stack(tmp_path, mode=TOOL_ERROR_MODE_CORRECTED)
    provider = stack["provider"]
    executes_before = provider.execute_calls
    script = [
        [
            ToolUseBlock(id="call-fail", name="bash", input={"command": FAILING_COMMAND}),
            ToolUseBlock(
                id="call-read", name="file_read", input={"path": "/workspace/project/app.py"}
            ),
        ],
        [TextBlock(text="done")],
    ]
    result, seen = await _run(stack, script=script)

    assert len(seen) == 2
    worker = result.agent_results["worker"]
    assert worker.stop_reason is not None and worker.stop_reason.code == "success"

    results = _tool_results(seen[1])
    # Both results of the batch stay correlated with their own tool_use ids.
    assert set(results) == {"call-fail", "call-read"}
    assert results["call-fail"].is_error is True
    assert "boom: missing config" in str(results["call-fail"].content)
    assert results["call-read"].is_error is False
    assert "VALUE = 1" in str(results["call-read"].content)

    # The failing bash ran once; the read tool never executes a sandbox command.
    assert provider.execute_calls - executes_before == 1


async def test_unknown_outcome_still_stops_the_worker(tmp_path: Path) -> None:
    stack = await _stack(tmp_path, mode=TOOL_ERROR_MODE_CORRECTED)
    provider = stack["provider"]
    provider.fail_operations = True
    script = [
        [ToolUseBlock(id="call-1", name="bash", input={"command": "echo hi"})],
        [TextBlock(text="should not be reached")],
    ]
    result, seen = await _run(stack, script=script)

    assert len(seen) == 1
    worker = result.agent_results["worker"]
    assert worker.stop_reason is not None
    assert worker.stop_reason.code == "side_effect_unknown"
    assert "sandbox execution failed" in worker.stop_reason.message
    assert result.success is False
