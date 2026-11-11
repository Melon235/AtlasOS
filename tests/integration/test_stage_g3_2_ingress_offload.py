"""Stage G3-2 agent-loop integration: large tool results are archived before the model sees them.

The stack is the production one (M1 registry + M3 gate + M4 ledger + M5 sandbox tools + M6 runner
seam + G2a budget hook) with the Stage G2b-1 ``HistoryStore``/``history.read`` reused as the
archive. Only the model provider and the sandbox provider are faked, so every assertion below is
made against the exact messages the provider received.

Covered:

* ``test_large_result_is_offloaded_and_recoverable`` — Turn 1 bash emits 20k chars, the model sees
  a reference, ``history.read`` restores the original, the reference never changes afterwards and
  nothing is archived twice.
* ``test_off_mode_keeps_the_full_result`` — control arm.
* ``test_large_error_result_keeps_semantics`` — G3-1 ``is_error``/``retry_safe`` regression.
* ``test_unknown_outcome_is_not_offloaded_and_still_stops`` — safety regression.
* ``test_parallel_batch_offloads_each_result_once`` — two large results in one batch.
"""

from __future__ import annotations

import json
import subprocess
import uuid
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
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
from atlasos.handoff import HANDOFF_READ_ACTION_MAPPING, HandoffNode, HandoffRuntime
from atlasos.history_compaction import (
    HISTORY_READ_ACTION_MAPPING,
    CompactionMetrics,
    HistoryCompactionRuntime,
    HistoryReader,
    HistoryReadInput,
)
from atlasos.integrations.anycode_engine import AtlasContextEngine
from atlasos.ledger import SQLiteActionLedgerStore
from atlasos.sandbox import ProjectSandboxManager, SandboxProjectConfig, SandboxWorkspaceTools
from atlasos.sandbox.tools import TOOL_ERROR_MODE_CORRECTED
from atlasos.suspension.store import SQLiteSuspensionStore
from atlasos.tool_result_offload import (
    TOOL_RESULT_INGRESS_OFFLOAD,
    TOOL_RESULT_OFF,
    ToolResultIngressRuntime,
)
from atlasos.tools import AtlasToolRuntime

BIG_OUTPUT_COMMAND = "python3 -c \"print('BEGIN-MARKER\\n' + 'x' * 20000 + '\\nEND-MARKER')\""
BIG_ERROR_COMMAND = (
    'python3 -c "import sys; '
    "print('BEGIN-MARKER\\n' + 'y' * 6000); "
    "sys.stderr.write('AssertionError: boom\\n'); sys.exit(1)\""
)
MIDDLE_FRAGMENT = "x" * 200
PROVIDER_NAME = "g32-scripted"


def _tool_context(task_id: str) -> Any:
    return SimpleNamespace(
        execution_context=SimpleNamespace(attributes={"run_id": "run-g32", "task_id": task_id})
    )


class _Scripted:
    """Deterministic provider driven by per-turn functions that see the current messages."""

    def __init__(
        self, script: list[Callable[[list[LLMMessage]], list[Any]]], seen: list[list[LLMMessage]]
    ) -> None:
        self._script = script
        self._seen = seen
        self.name = "scripted"
        self.calls = 0

    def _next(self, messages: list[LLMMessage], options: Any) -> LLMResponse:
        self._seen.append(list(messages))
        index = self.calls
        self.calls += 1
        blocks: list[Any] = (
            self._script[index](messages) if index < len(self._script) else [TextBlock(text="done")]
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
        for action in (
            "control.search",
            "control.dispatch",
            "filesystem.read",
            "filesystem.write",
            "shell.execute",
            "external.read",
        )
    )
    return ActionPolicy(name="g32", default="DENY", rules=rules)


async def _stack(tmp_path: Path, *, mode: str) -> dict[str, Any]:
    repo = _make_repo(tmp_path / "repo")
    provider = FakeSandboxProvider(tmp_path / "provider")
    manager = ProjectSandboxManager(
        provider,
        SandboxProjectConfig(provider_name="fake", image="fake@sha256:0123456789abcdef"),
        run_id="run-g32",
    )
    task_id = "run-g32-loader"
    binding = await manager.ensure_binding(
        task_id=task_id,
        source_repo=str(repo),
        context=ExecutionContext(principal="user:test", attributes={"run_id": "run-g32"}),
        worker_id="w1",
    )
    tools = SandboxWorkspaceTools(
        manager, task_id, provider, tool_error_mode=TOOL_ERROR_MODE_CORRECTED
    )
    compaction = HistoryCompactionRuntime.create(run_dir=tmp_path, run_id="run-g32", mode="off")
    ingress = ToolResultIngressRuntime.wrap(store=compaction.store, mode=mode)
    handoff = HandoffRuntime.create(
        run_dir=tmp_path / "handoff",
        run_id="run-g32",
        nodes=[HandoffNode(key=task_id, title="task-g32")],
        consumer_keys={},
        mode="dual",
    )
    runtime = AtlasToolRuntime(
        "run-g32",
        state_path=tmp_path / "tool_state.json",
        conversation_path=tmp_path / "conversation.json",
        external_tools=(*handoff.external_tools(), *compaction.external_tools()),
        action_policy=_policy(),
        action_catalog=ActionCatalog(
            DEFAULT_MAPPINGS + (HANDOFF_READ_ACTION_MAPPING, HISTORY_READ_ACTION_MAPPING)
        ),
        action_reviewer=StaticActionReviewer("CONSISTENT"),
        action_ledger=SQLiteActionLedgerStore(tmp_path / "ledger.db"),
        idempotency_store=InMemoryToolIdempotencyStore(),
        suspension_store=SQLiteSuspensionStore(tmp_path / "runtime.db"),
        approval_store=SQLiteApprovalStore(tmp_path / "approvals.db"),
        runtime_instance_id="runtime-g32",
        sandbox_tools=tools,
        tool_result_offloader=ingress,
    )
    return {
        "repo": repo,
        "provider": provider,
        "binding": binding,
        "task_id": task_id,
        "tools": tools,
        "compaction": compaction,
        "ingress": ingress,
        "runtime": runtime,
        "root": tmp_path,
    }


async def _run(
    stack: dict[str, Any],
    *,
    script: list[Callable[[list[LLMMessage]], list[Any]]],
    budget: RunBudgetState | None = None,
) -> tuple[Any, list[list[LLMMessage]]]:
    seen: list[list[LLMMessage]] = []
    provider_name = f"{PROVIDER_NAME}-{uuid.uuid4().hex}"

    async def factory(**kwargs: Any) -> _Scripted:
        del kwargs
        return _Scripted(script, seen)

    register_provider_factory(provider_name, factory)
    engine = AtlasContextEngine(
        OrchestratorConfig(), tool_runtime=stack["runtime"], budget_state=budget
    )
    context = ExecutionContext(
        principal="user:test",
        tenant_scope="tenant-g32",
        classification="internal",
        attributes={
            "run_id": "run-g32",
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
    team = engine.create_team("g32", TeamConfig(name="g32", agents=[agent]))
    spec = TaskSpec(title="Task", description="inspect the workspace", assignee="worker")
    from atlasos.tools.conversation import reset_current_task_id, set_current_task_id

    token = set_current_task_id(stack["task_id"])
    try:
        result = await engine.run_tasks(team, [spec])
    finally:
        reset_current_task_id(token)
        await engine.close()
    return result, seen


def _agent_config(stack: dict[str, Any], provider_name: str) -> AgentConfig:
    return AgentConfig(
        name="worker",
        model="fake-model",
        provider=provider_name,
        system_prompt="worker",
        execution_context=ExecutionContext(
            principal="user:test",
            tenant_scope="tenant-g32",
            classification="internal",
            attributes={
                "run_id": "run-g32",
                "task_id": stack["task_id"],
                "worker_id": "w1",
                "profile_id": "coding_worker",
                "worker_profile": "coding_worker",
                "operating_scope": "coding",
            },
        ),
        max_tokens=256,
    )


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


def _all_text(messages: list[LLMMessage]) -> str:
    parts: list[str] = []
    for message in messages:
        for block in _blocks(message):
            if isinstance(block, ToolResultBlock):
                parts.append(str(block.content))
            elif isinstance(block, TextBlock):
                parts.append(block.text)
    return "\n".join(parts)


def _reference(text: str) -> str:
    for line in text.splitlines():
        if line.startswith("Reference: "):
            return line.split(": ", 1)[1].strip()
    raise AssertionError(f"no reference in {text[:200]!r}")


async def test_large_result_is_offloaded_and_recoverable(tmp_path: Path) -> None:
    stack = await _stack(tmp_path, mode=TOOL_RESULT_INGRESS_OFFLOAD)
    provider = stack["provider"]
    executes_before = provider.execute_calls
    budget = RunBudgetState(limit_tokens=120_000)
    # Separate metric object: the shared reader counters must reflect model calls only.
    reader = HistoryReader(store=stack["compaction"].store, metrics=CompactionMetrics())

    def turn1(_: list[LLMMessage]) -> list[Any]:
        return [ToolUseBlock(id="call-bash", name="bash", input={"command": BIG_OUTPUT_COMMAND})]

    def turn2(messages: list[LLMMessage]) -> list[Any]:
        placeholder = str(_tool_results(messages)["call-bash"].content)
        assert "[TOOL_RESULT_OFFLOADED]" in placeholder
        return [ToolUseBlock(id="call-search", name="tool_search", input={"query": "history.read"})]

    def turn3(messages: list[LLMMessage]) -> list[Any]:
        assert "history.read" in str(_tool_results(messages)["call-search"].content)
        ref_id = _reference(str(_tool_results(messages)["call-bash"].content))
        return [
            ToolUseBlock(
                id="call-read",
                name="dispatch_action",
                input={
                    "tool_id": "history.read",
                    "arguments": {"ref_id": ref_id, "offset": 0, "limit": 4000},
                },
            )
        ]

    def turn4(_: list[LLMMessage]) -> list[Any]:
        return [TextBlock(text="inspected")]

    result, seen = await _run(stack, script=[turn1, turn2, turn3, turn4], budget=budget)

    # 1. The model never received the 20k body: the first following turn saw a reference.
    first_result = str(_tool_results(seen[1])["call-bash"].content)
    assert first_result.startswith("[TOOL_RESULT_OFFLOADED]")
    assert "Original size: " in first_result
    assert len(first_result) < 2000
    assert MIDDLE_FRAGMENT not in first_result
    ref_id = _reference(first_result)

    # 2. history.read returned the original content, page by page.
    read_payload = json.loads(str(_tool_results(seen[3])["call-read"].content))
    assert read_payload["ref_id"] == ref_id
    assert read_payload["total_chars"] > 20000
    assert read_payload["content"].startswith("BEGIN-MARKER")
    page = await reader.read(
        HistoryReadInput(ref_id=ref_id, offset=0, limit=4000),
        _tool_context(stack["task_id"]),
    )
    assert json.loads(page.data)["total_chars"] == read_payload["total_chars"]

    # 3. The earlier reference is byte-identical in every later request: nothing was rewritten.
    later = str(_tool_results(seen[3])["call-bash"].content)
    assert later == first_result
    # The raw body only ever appears because the model asked for it explicitly.
    assert "x" * 1000 not in first_result
    assert "x" * 1000 in str(_tool_results(seen[3])["call-read"].content)

    # 4. Exactly one archive, one bash execution, no replay, no duplicate reference.
    assert stack["ingress"].metrics.offloads == 1
    assert stack["ingress"].metrics.skipped_already_offloaded == 0
    history_files = sorted((tmp_path / "history").glob("*.txt"))
    assert len(history_files) == 1
    assert provider.execute_calls - executes_before == 1
    assert stack["compaction"].metrics.read_calls == 1

    # The archived bytes are the original tool result, verbatim.
    stored = history_files[0].read_text(encoding="utf-8")
    assert stored.startswith("BEGIN-MARKER")
    assert stored.rstrip().endswith("END-MARKER")

    # 5. Task semantics unchanged.
    worker = result.agent_results["worker"]
    assert worker.stop_reason is not None and worker.stop_reason.code == "success"
    assert budget.admission_rejections == 0

    # 6. M6 snapshot stores the same reference (the read page stays a separate block).
    snapshot = stack["runtime"].conversation_store.load(stack["task_id"])
    assert snapshot is not None
    snapshot_results = _tool_results([LLMMessage.model_validate(item) for item in snapshot])
    assert str(snapshot_results["call-bash"].content) == first_result
    assert ref_id in str(snapshot_results["call-bash"].content)


async def test_off_mode_keeps_the_full_result(tmp_path: Path) -> None:
    stack = await _stack(tmp_path, mode=TOOL_RESULT_OFF)

    def turn1(_: list[LLMMessage]) -> list[Any]:
        return [ToolUseBlock(id="call-bash", name="bash", input={"command": BIG_OUTPUT_COMMAND})]

    def turn2(_: list[LLMMessage]) -> list[Any]:
        return [TextBlock(text="done")]

    result, seen = await _run(stack, script=[turn1, turn2])

    full = str(_tool_results(seen[1])["call-bash"].content)
    assert "[TOOL_RESULT_OFFLOADED]" not in full
    assert len(full) > 20000
    assert not (tmp_path / "history").exists()
    assert stack["ingress"].metrics.offloads == 0
    worker = result.agent_results["worker"]
    assert worker.stop_reason is not None and worker.stop_reason.code == "success"


async def test_parallel_batch_offloads_each_result_once(tmp_path: Path) -> None:
    stack = await _stack(tmp_path, mode=TOOL_RESULT_INGRESS_OFFLOAD)
    provider = stack["provider"]
    executes_before = provider.execute_calls

    def turn1(_: list[LLMMessage]) -> list[Any]:
        return [
            ToolUseBlock(id="call-a", name="bash", input={"command": BIG_OUTPUT_COMMAND}),
            ToolUseBlock(
                id="call-b", name="bash", input={"command": 'python3 -c "print(\\"z\\" * 9000)"'}
            ),
        ]

    def turn2(_: list[LLMMessage]) -> list[Any]:
        return [TextBlock(text="done")]

    result, seen = await _run(stack, script=[turn1, turn2])

    results = _tool_results(seen[1])
    assert set(results) == {"call-a", "call-b"}
    refs = {call_id: _reference(str(block.content)) for call_id, block in results.items()}
    assert refs["call-a"] != refs["call-b"]
    assert stack["ingress"].metrics.offloads == 2
    assert len(list((tmp_path / "history").glob("*.txt"))) == 2
    assert provider.execute_calls - executes_before == 2
    worker = result.agent_results["worker"]
    assert worker.stop_reason is not None and worker.stop_reason.code == "success"


async def test_large_error_result_keeps_semantics(tmp_path: Path) -> None:
    stack = await _stack(tmp_path, mode=TOOL_RESULT_INGRESS_OFFLOAD)

    def turn1(_: list[LLMMessage]) -> list[Any]:
        return [ToolUseBlock(id="call-bash", name="bash", input={"command": BIG_ERROR_COMMAND})]

    def turn2(messages: list[LLMMessage]) -> list[Any]:
        block = _tool_results(messages)["call-bash"]
        assert block.is_error is True
        return [TextBlock(text="recovered")]

    result, seen = await _run(stack, script=[turn1, turn2])

    placeholder = str(_tool_results(seen[1])["call-bash"].content)
    assert placeholder.startswith("[TOOL_RESULT_OFFLOADED]")
    assert "AssertionError: boom" in placeholder
    assert "exit code 1" in placeholder
    assert len(placeholder) < 2500
    worker = result.agent_results["worker"]
    # G3-1 semantics: a completed non-zero exit stays a known error, the worker continues.
    assert worker.stop_reason is not None and worker.stop_reason.code == "success"


async def test_unknown_outcome_is_not_offloaded_and_still_stops(tmp_path: Path) -> None:
    stack = await _stack(tmp_path, mode=TOOL_RESULT_INGRESS_OFFLOAD)
    stack["provider"].fail_operations = True

    def turn1(_: list[LLMMessage]) -> list[Any]:
        return [ToolUseBlock(id="call-bash", name="bash", input={"command": "echo hi"})]

    result, seen = await _run(stack, script=[turn1])

    assert len(seen) == 1
    assert stack["ingress"].metrics.offloads == 0
    worker = result.agent_results["worker"]
    assert worker.stop_reason is not None
    assert worker.stop_reason.code == "side_effect_unknown"
    assert result.success is False


@pytest.mark.parametrize("mode", [TOOL_RESULT_OFF, TOOL_RESULT_INGRESS_OFFLOAD])
async def test_both_arms_register_the_same_history_reader(tmp_path: Path, mode: str) -> None:
    stack = await _stack(tmp_path, mode=mode)
    runtime = stack["runtime"]
    assert runtime.registry.get("history.read") is not None
    assert runtime.registry.get("handoff.read") is not None


async def test_m6_resume_replays_the_reference_without_rearchiving(tmp_path: Path) -> None:
    stack = await _stack(tmp_path, mode=TOOL_RESULT_INGRESS_OFFLOAD)

    def turn1(_: list[LLMMessage]) -> list[Any]:
        return [ToolUseBlock(id="call-bash", name="bash", input={"command": BIG_OUTPUT_COMMAND})]

    def turn2(_: list[LLMMessage]) -> list[Any]:
        return [TextBlock(text="inspected")]

    _, seen = await _run(stack, script=[turn1, turn2])
    placeholder = str(_tool_results(seen[1])["call-bash"].content)
    ref_id = _reference(placeholder)
    assert stack["ingress"].metrics.offloads == 1

    # A resumed agent replays the M6 snapshot instead of the prompt and must not re-archive.
    seen_resume: list[list[LLMMessage]] = []
    provider_name = f"{PROVIDER_NAME}-resume-{uuid.uuid4().hex}"
    script = [lambda _: [TextBlock(text="resumed")]]

    async def factory(**kwargs: Any) -> _Scripted:
        del kwargs
        return _Scripted(script, seen_resume)

    register_provider_factory(provider_name, factory)
    engine = AtlasContextEngine(OrchestratorConfig(), tool_runtime=stack["runtime"])
    agent = engine.build_agent(_agent_config(stack, provider_name))
    from atlasos.tools.conversation import reset_current_task_id, set_current_task_id

    token = set_current_task_id(stack["task_id"])
    try:
        resumed = await agent.run("ignored on resume")
    finally:
        reset_current_task_id(token)
        await engine.close()

    assert len(seen_resume) == 1
    replayed = str(_tool_results(seen_resume[0])["call-bash"].content)
    assert replayed == placeholder
    assert "x" * 1000 not in replayed
    # Resume replays the snapshot: no tool ran, so nothing was archived a second time.
    assert stack["ingress"].metrics.offloads == 1
    assert len(list((tmp_path / "history").glob("*.txt"))) == 1
    stored = (tmp_path / "history" / f"{ref_id}.txt").read_text(encoding="utf-8")
    assert stored.startswith("BEGIN-MARKER")
    assert resumed.success is True


async def test_reference_survives_a_second_offload_attempt(tmp_path: Path) -> None:
    """Re-ingressing an already-referenced result is a no-op, not a second archive."""

    stack = await _stack(tmp_path, mode=TOOL_RESULT_INGRESS_OFFLOAD)
    offloader = stack["ingress"].offloader
    from anycode.types import ToolResult

    first = offloader.offload(
        ToolResult(data="w" * 9000), tool_name="bash", task_id=stack["task_id"], call_key="k"
    )
    second = offloader.offload(
        ToolResult(data=first.data), tool_name="bash", task_id=stack["task_id"], call_key="k"
    )

    assert second.data == first.data
    assert stack["ingress"].metrics.offloads == 1
    assert stack["ingress"].metrics.skipped_already_offloaded == 1
