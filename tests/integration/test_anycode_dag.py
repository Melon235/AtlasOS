"""Stage 0 spike: a dependency-ordered multi-agent run on AnyCode with deterministic fakes.

Proves the upstream extension path (provider factories, team/task construction, wave scheduling)
without touching AtlasOS optimization code, which does not exist yet.

Stage G1 adds the dual-channel handoff: the same engine seam, driven through the real M1 tool
registry and M3 action safety gate, with two concurrent consumers that must never cross-read.

Stage G2a adds the run-scoped token budget: the same seam, showing that a shared budget rejects
LLM requests before the provider is called and settles real usage when the run fits.

Stage G2b-1 adds rolling tool-result compaction: the same seam, showing that older tool results are
replaced by references in the prompt the provider receives, that the newest two rounds stay
verbatim, and that the model can read an archived result back through ``history.read``.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import AsyncIterable
from pathlib import Path
from typing import Any

import pytest
from anycode import AnyCode, OrchestratorConfig
from anycode.identity.context import ExecutionContext
from anycode.plugins.registry import register_provider_factory
from anycode.providers.fake import FakeAdapter, FakeResponse
from anycode.types import (
    AgentConfig,
    LLMChatOptions,
    LLMMessage,
    LLMResponse,
    LLMStreamOptions,
    ProviderResilienceConfig,
    StreamEvent,
    TeamConfig,
    TextBlock,
    TokenUsage,
    ToolUseBlock,
)

from atlasos.actions import ActionCatalog, ActionMapping, ActionPolicy, ActionRule
from atlasos.actions.catalog import DEFAULT_MAPPINGS
from atlasos.budget import REJECTION_CODE, RunBudgetState
from atlasos.handoff import HANDOFF_READ_ACTION_MAPPING, HandoffNode, HandoffRuntime
from atlasos.history_compaction import (
    COMPACTION_OFF,
    COMPACTION_ROLLING_2,
    HISTORY_READ_ACTION_MAPPING,
    HISTORY_READ_TOOL,
    OFFLOAD_MARKER,
    HistoryCompactionRuntime,
    ToolResultCompactionHook,
)
from atlasos.integrations.anycode_engine import AtlasContextEngine  # noqa: E402
from atlasos.integrations.anycode_runtime import build_agent, build_task, build_team
from atlasos.tools import AtlasToolRuntime
from atlasos.tools.conversation import (
    ConversationSnapshotHook,
    reset_current_task_id,
    set_current_task_id,
)

PROVIDERS: dict[str, tuple[str, str]] = {
    "atlasos-test-research-a": ("researcher-a", "fact A"),
    "atlasos-test-research-b": ("researcher-b", "fact B"),
    "atlasos-test-reviewer": ("reviewer", "merged review"),
}

RECORDED: dict[str, list[list[LLMMessage]]] = {}


class _RecordingFake(FakeAdapter):
    """FakeAdapter that also captures the prompts AnyCode assembled."""

    def __init__(self, reply: str, sink: list[list[LLMMessage]]) -> None:
        super().__init__(responses=[FakeResponse(text=reply)])
        self._sink = sink

    async def chat(self, messages: list[LLMMessage], options: LLMChatOptions):  # type: ignore[override]
        self._sink.append(list(messages))
        return await super().chat(messages, options)


def _install_provider(provider: str, reply: str) -> None:
    async def factory(**_: Any) -> FakeAdapter:
        return _RecordingFake(reply, RECORDED.setdefault(provider, []))

    register_provider_factory(provider, factory)


async def test_dependencies_run_before_dependents_and_outputs_are_forwarded() -> None:
    for provider, (_, reply) in PROVIDERS.items():
        _install_provider(provider, reply)

    events: list[Any] = []
    engine = AnyCode(OrchestratorConfig(max_concurrency=3, on_progress=events.append))
    team = build_team(
        engine,
        name="stage0-spike",
        agents=[
            build_agent(name=name, provider=provider, system_prompt=f"You are {name}.")
            for provider, (name, _) in PROVIDERS.items()
        ],
    )

    # Deliberately shuffled: dependency resolution must come from `depends_on`, not list order.
    specs = [
        build_task(
            title="Merge review",
            description="Merge both research outputs.",
            assignee="reviewer",
            depends_on=["Research A", "Research B"],
        ),
        build_task(title="Research A", description="Collect fact A.", assignee="researcher-a"),
        build_task(title="Research B", description="Collect fact B.", assignee="researcher-b"),
    ]

    result = await engine.run_tasks(team, specs)

    assert result.success is True
    assert set(result.agent_results) == {"researcher-a", "researcher-b", "reviewer"}
    # One deterministic fake call per agent.
    assert result.total_token_usage.input_tokens == 15
    assert result.total_token_usage.output_tokens == 15

    title_by_id = {
        event.data.id: event.data.title for event in events if event.type == "task_start"
    }
    sequence: list[tuple[str, str]] = []
    for event in events:
        if event.type == "task_start":
            sequence.append(("start", title_by_id[event.data.id]))
        elif event.type == "task_complete":
            sequence.append(("complete", title_by_id[event.task]))

    assert sequence.index(("complete", "Research A")) < sequence.index(("start", "Merge review"))
    assert sequence.index(("complete", "Research B")) < sequence.index(("start", "Merge review"))

    reviewer_prompt = "\n".join(
        block.text
        for message in RECORDED["atlasos-test-reviewer"][0]
        for block in message.content
        if isinstance(block, TextBlock)
    )
    assert "fact A" in reviewer_prompt
    assert "fact B" in reviewer_prompt


@pytest.mark.parametrize("provider", sorted(PROVIDERS))
async def test_each_provider_sees_exactly_one_call(provider: str) -> None:
    assert len(RECORDED.get(provider, [])) <= 1


# --- Stage G1: dual-channel handoff through the M1 registry and the M3 gate -------------------

G1_READ = "handoff.read"
G1_P1 = "<p1>" * 1_500
G1_P2 = "<p2>" * 1_500
_REF_RE = re.compile(r"ho-[0-9a-f]{24}")
_P2_REF = "ho-" + hashlib.sha256(G1_P2.encode("utf-8")).hexdigest()[:24]


def _prompt_text(messages: list[LLMMessage]) -> str:
    return "\n".join(
        block.text
        for message in messages
        for block in message.content
        if isinstance(block, TextBlock)
    )


class _HandoffAdapter:
    """Deterministic producer / consumer adapter that can drive the tool surface."""

    def __init__(self, agent: str) -> None:
        self._agent = agent
        self._cursor = 0
        self.prompts: list[str] = []

    @property
    def name(self) -> str:
        return f"g1-{self._agent}"

    def _blocks(self, turn: int, prompt: str) -> list[Any]:
        if self._agent == "p1":
            return [TextBlock(text=G1_P1)]
        if self._agent == "p2":
            return [TextBlock(text=G1_P2)]
        if self._agent == "join":
            return [TextBlock(text="joined")]
        own_ref = _REF_RE.findall(prompt)[0]
        if turn == 0:
            return [ToolUseBlock(id="s", name="tool_search", input={"query": "handoff result"})]
        if turn == 1:
            return [
                ToolUseBlock(
                    id="r0",
                    name="dispatch_action",
                    input={"tool_id": G1_READ, "arguments": {"ref_id": own_ref}},
                )
            ]
        if turn == 2:
            return [
                ToolUseBlock(
                    id="r1",
                    name="dispatch_action",
                    input={
                        "tool_id": G1_READ,
                        "arguments": {"ref_id": own_ref, "offset": 3000},
                    },
                )
            ]
        if turn == 3 and self._agent == "c1":
            # c2 runs in the same wave; reading its producer must be refused.
            return [
                ToolUseBlock(
                    id="foreign",
                    name="dispatch_action",
                    input={"tool_id": G1_READ, "arguments": {"ref_id": _P2_REF}},
                )
            ]
        return [TextBlock(text=f"{self._agent} done")]

    async def chat(self, messages: list[LLMMessage], options: LLMChatOptions) -> LLMResponse:
        prompt = _prompt_text(messages)
        self.prompts.append(prompt)
        blocks = self._blocks(self._cursor, prompt)
        self._cursor += 1
        return LLMResponse(
            id=f"{self.name}-{self._cursor}",
            content=blocks,
            model=options.model or "fake",
            stop_reason="tool_use"
            if any(isinstance(b, ToolUseBlock) for b in blocks)
            else "end_turn",
            usage=TokenUsage(input_tokens=10, output_tokens=5),
        )

    def stream(
        self, messages: list[LLMMessage], options: LLMStreamOptions
    ) -> AsyncIterable[StreamEvent]:
        async def _generate() -> AsyncIterable[StreamEvent]:
            yield StreamEvent(type="done", data=await self.chat(messages, options))

        return _generate()


def _g1_policy() -> ActionPolicy:
    allow = ("control.search", "control.dispatch", "external.read")
    return ActionPolicy(
        name="g1-integration",
        default="DENY",
        rules=tuple(
            ActionRule(id=f"allow-{action}", decision="ALLOW", action=action) for action in allow
        ),
    )


def _g1_agent(name: str) -> AgentConfig:
    return AgentConfig(
        name=name,
        model="fake",
        provider=f"g1-{name}",
        execution_context=ExecutionContext(
            principal="user:evaluation",
            tenant_scope="atlas-evaluation",
            classification="internal",
            attributes={"run_id": "g1-run", "task_id": name},
        ),
    )


def _read_pages(records: list[Any]) -> list[dict[str, Any]]:
    pages: list[dict[str, Any]] = []
    for record in records:
        raw = record.input if isinstance(record.input, dict) else {}
        if raw.get("tool_id") != G1_READ:
            continue
        output = str(record.output)
        pages.append(
            {"denied": True, "error": output}
            if output.startswith("handoff_denied:")
            else json.loads(output)
        )
    return pages


async def test_dual_handoff_uses_m1_and_m3_without_concurrent_cross_talk(tmp_path: Path) -> None:
    adapters = {name: _HandoffAdapter(name) for name in ("p1", "p2", "c1", "c2", "join")}
    for adapter in adapters.values():

        def register(instance: _HandoffAdapter = adapter) -> None:
            async def factory(**_: Any) -> _HandoffAdapter:
                return instance

            register_provider_factory(instance.name, factory)

        register()

    handoff = HandoffRuntime.create(
        run_dir=tmp_path,
        run_id="g1-run",
        nodes=(
            HandoffNode("p1", "task-p1"),
            HandoffNode("p2", "task-p2"),
            HandoffNode("c1", "task-c1", ("p1",)),
            HandoffNode("c2", "task-c2", ("p2",)),
            HandoffNode("join", "task-join", ("c1", "c2")),
        ),
        consumer_keys={name: name for name in ("p1", "p2", "c1", "c2", "join")},
        mode="dual",
    )
    tool_runtime = AtlasToolRuntime(
        "g1-run",
        external_tools=handoff.external_tools(),
        action_policy=_g1_policy(),
        action_catalog=ActionCatalog(mappings=(*DEFAULT_MAPPINGS, HANDOFF_READ_ACTION_MAPPING)),
    )
    engine = AtlasContextEngine(
        OrchestratorConfig(),
        tool_runtime=tool_runtime,
        prompt_builder=handoff.build_prompt,
    )
    try:
        team = engine.create_team(
            "g1",
            TeamConfig(
                name="g1",
                agents=[_g1_agent(name) for name in ("p1", "p2", "c1", "c2", "join")],
            ),
        )
        specs = [
            build_task(title="task-p1", description="produce alpha", assignee="p1"),
            build_task(title="task-p2", description="produce bravo", assignee="p2"),
            build_task(
                title="task-c1",
                description="consume alpha",
                assignee="c1",
                depends_on=["task-p1"],
            ),
            build_task(
                title="task-c2",
                description="consume bravo",
                assignee="c2",
                depends_on=["task-p2"],
            ),
            build_task(
                title="task-join",
                description="join outputs",
                assignee="join",
                depends_on=["task-c1", "task-c2"],
            ),
        ]
        run = await engine.run_tasks(team, specs)
    finally:
        await engine.close()

    assert run.success is True
    assert tool_runtime.action_safety_gate is not None

    c1_pages = _read_pages(run.agent_results["c1"].tool_calls)
    c2_pages = _read_pages(run.agent_results["c2"].tool_calls)
    c1_content = "".join(page.get("content", "") for page in c1_pages)
    c2_content = "".join(page.get("content", "") for page in c2_pages)
    assert c1_content == G1_P1
    assert c2_content == G1_P2
    assert "<p2>" not in c1_content
    assert "<p1>" not in c2_content

    denied = [page for page in c1_pages if page.get("denied")]
    assert len(denied) == 1
    assert denied[0]["error"].startswith("handoff_denied:")

    metrics = handoff.metrics.as_dict()
    assert metrics["offloaded_handoffs"] == 2
    assert metrics["read_success"] == 4
    assert metrics["read_denied"] == 1
    assert metrics["consumers_read_complete"] == 2
    assert metrics["unmapped_consumers"] == []

    tool_metrics = tool_runtime.metrics()
    assert tool_metrics.search_count == 2
    assert tool_metrics.dispatch_count == 5
    assert sum(1 for item in tool_metrics.dispatches if not item.success) == 1

    join_prompt = adapters["join"].prompts[0]
    assert "task-c1" in join_prompt and "task-c2" in join_prompt
    assert G1_READ not in join_prompt


# --- Stage G2a: run-scoped token budget at the same engine seam ------------------------------


class _UsageAdapter:
    """Deterministic provider that reports fixed usage and counts real calls."""

    def __init__(
        self, name: str, calls: list[str], *, input_tokens: int, output_tokens: int
    ) -> None:
        self._name = name
        self._calls = calls
        self._input_tokens = input_tokens
        self._output_tokens = output_tokens

    @property
    def name(self) -> str:
        return self._name

    async def chat(self, messages: list[LLMMessage], options: LLMChatOptions) -> LLMResponse:
        del messages
        self._calls.append(self._name)
        return LLMResponse(
            id=f"{self._name}-{len(self._calls)}",
            content=[TextBlock(text=f"{self._name} done")],
            model=options.model or "fake",
            stop_reason="end_turn",
            usage=TokenUsage(input_tokens=self._input_tokens, output_tokens=self._output_tokens),
        )

    def stream(
        self, messages: list[LLMMessage], options: LLMStreamOptions
    ) -> AsyncIterable[StreamEvent]:
        async def _generate() -> AsyncIterable[StreamEvent]:
            yield StreamEvent(type="done", data=await self.chat(messages, options))

        return _generate()


def _budget_agent(name: str, provider: str, *, max_tokens: int | None = None) -> AgentConfig:
    return AgentConfig(
        name=name,
        model="fake",
        provider=provider,
        max_tokens=max_tokens,
        execution_context=ExecutionContext(
            principal="user:evaluation",
            tenant_scope="atlas-evaluation",
            classification="internal",
            attributes={"run_id": "g2a-run", "task_id": name},
        ),
    )


async def _run_two_workers(
    tmp_path: Path,
    *,
    budget: RunBudgetState,
    calls: list[str],
    input_tokens: int,
    output_tokens: int,
    max_tokens: int | None = None,
) -> tuple[Any, Any]:
    names = ("w1", "w2")
    tag = uuid.uuid4().hex[:10]
    providers = {name: f"g2a-{tag}-{name}" for name in names}
    for name, provider in providers.items():

        def register(worker: str = name, registered: str = provider) -> None:
            async def factory(**_: Any) -> _UsageAdapter:
                return _UsageAdapter(
                    worker, calls, input_tokens=input_tokens, output_tokens=output_tokens
                )

            register_provider_factory(registered, factory)

        register()

    tool_runtime = AtlasToolRuntime("g2a-run")
    engine = AtlasContextEngine(
        OrchestratorConfig(), tool_runtime=tool_runtime, budget_state=budget
    )
    try:
        team = engine.create_team(
            "g2a",
            TeamConfig(
                name="g2a",
                agents=[
                    _budget_agent(name, providers[name], max_tokens=max_tokens) for name in names
                ],
            ),
        )
        specs = [
            build_task(title="task-w1", description="work one", assignee="w1"),
            build_task(title="task-w2", description="work two", assignee="w2"),
        ]
        run = await engine.run_tasks(team, specs)
    finally:
        await engine.close()
    return run, budget


async def test_budget_guard_rejects_before_the_provider_is_called(tmp_path: Path) -> None:
    calls: list[str] = []
    budget = RunBudgetState(limit_tokens=1_000)
    run, _ = await _run_two_workers(
        tmp_path, budget=budget, calls=calls, input_tokens=300, output_tokens=50
    )

    assert calls == []  # nothing reached the provider
    assert run.success is False
    assert budget.requests_started == 0
    assert budget.admission_rejections == 2
    assert budget.reserved_tokens == 0
    for name in ("w1", "w2"):
        output = str(run.agent_results[name].output)
        assert output.startswith(REJECTION_CODE)
    snapshot = budget.finalize()
    assert snapshot["stop_reason"].startswith(REJECTION_CODE)
    assert snapshot["requests_completed"] == 0
    assert snapshot["peak_reserved_tokens"] == 0


async def test_budget_guard_settles_real_usage_when_the_run_fits(tmp_path: Path) -> None:
    calls: list[str] = []
    budget = RunBudgetState(limit_tokens=100_000)
    run, _ = await _run_two_workers(
        tmp_path,
        budget=budget,
        calls=calls,
        input_tokens=300,
        output_tokens=50,
        max_tokens=100,
    )

    assert sorted(calls) == ["w1", "w2"]
    assert run.success is True
    assert budget.requests_started == 2
    assert budget.requests_completed == 2
    assert budget.used_prompt_tokens == 600
    assert budget.used_completion_tokens == 100
    assert budget.reserved_tokens == 0
    assert budget.admission_rejections == 0
    snapshot = budget.finalize()
    assert snapshot["in_flight"] == 0
    assert snapshot["workers"]["w1"]["completed"] == 1
    assert snapshot["workers"]["w2"]["completed"] == 1


# --- Stage G2b-1: rolling tool-result compaction at the same engine seam ----------------------

PROBE_TOOL = "probe.read"
PAYLOAD_SIZE = 1_500
_HISTORY_REF_RE = re.compile(r"hc-[0-9a-f]{24}")


def _probe_payload(key: int) -> str:
    unit = f"<round-{key}>"
    return (unit * (PAYLOAD_SIZE // len(unit) + 1))[:PAYLOAD_SIZE]


def _probe_tool() -> Any:
    from anycode.types import ToolDefinition, ToolResult
    from pydantic import BaseModel

    class ProbeInput(BaseModel):
        key: int = 0

    async def execute(validated: ProbeInput, context: Any) -> ToolResult:
        del context
        return ToolResult(data=_probe_payload(validated.key), is_error=False)

    return ToolDefinition(
        name=PROBE_TOOL,
        description="Read a bulky probe payload for a round key.",
        input_model=ProbeInput,
        execute=execute,  # type: ignore[arg-type]
        side_effecting=False,
    )


class _CompactionAdapter:
    """Six tool rounds, then a history.read call, then a final answer."""

    def __init__(self, name: str, seen: list[list[LLMMessage]], rounds: int = 6) -> None:
        self._name = name
        self._seen = seen
        self._rounds = rounds

    @property
    def name(self) -> str:
        return self._name

    async def chat(self, messages: list[LLMMessage], options: LLMChatOptions) -> LLMResponse:
        self._seen.append(list(messages))
        turn = len(self._seen) - 1
        # References live in tool results, not in text blocks.
        prompt = "\n".join(
            str(getattr(block, "text", "") or getattr(block, "content", ""))
            for message in messages
            for block in message.content
        )
        blocks: list[Any] = []
        if turn == 0:
            blocks = [
                ToolUseBlock(id="s0", name="tool_search", input={"query": "probe payload"}),
                ToolUseBlock(
                    id="s1",
                    name="tool_search",
                    input={"query": "history read archived tool result"},
                ),
            ]
        elif turn <= self._rounds:
            blocks = [
                ToolUseBlock(
                    id=f"p{turn}",
                    name="dispatch_action",
                    input={
                        "tool_id": PROBE_TOOL,
                        "arguments": {"key": turn},
                    },
                )
            ]
        elif turn == self._rounds + 1:
            refs = _HISTORY_REF_RE.findall(prompt)
            blocks = [
                ToolUseBlock(
                    id="h1",
                    name="dispatch_action",
                    input={"tool_id": HISTORY_READ_TOOL, "arguments": {"ref_id": refs[0]}},
                )
            ]
        else:
            blocks = [TextBlock(text="done")]
        return LLMResponse(
            id=f"{self._name}-{turn}",
            content=blocks,
            model=options.model or "fake",
            stop_reason="tool_use"
            if any(isinstance(b, ToolUseBlock) for b in blocks)
            else "end_turn",
            usage=TokenUsage(input_tokens=10, output_tokens=5),
        )

    def stream(
        self, messages: list[LLMMessage], options: LLMStreamOptions
    ) -> AsyncIterable[StreamEvent]:
        async def _generate() -> AsyncIterable[StreamEvent]:
            yield StreamEvent(type="done", data=await self.chat(messages, options))

        return _generate()


def _compaction_policy() -> ActionPolicy:
    allow = ("control.search", "control.dispatch", "external.read")
    return ActionPolicy(
        name="g2b1-integration",
        default="DENY",
        rules=tuple(
            ActionRule(id=f"allow-{action}", decision="ALLOW", action=action) for action in allow
        ),
    )


async def _run_compaction_agent(
    tmp_path: Path, *, mode: str, budget: RunBudgetState | None = None
) -> tuple[Any, Any, list[list[LLMMessage]], HistoryCompactionRuntime]:
    tag = uuid.uuid4().hex[:10]
    provider = f"g2b1-{tag}"
    seen: list[list[LLMMessage]] = []
    adapter = _CompactionAdapter(provider, seen)

    async def factory(**_: Any) -> _CompactionAdapter:
        return adapter

    register_provider_factory(provider, factory)

    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    compaction = HistoryCompactionRuntime.create(run_dir=run_dir, run_id="g2b1-run", mode=mode)
    tool_runtime = AtlasToolRuntime(
        "g2b1-run",
        conversation_path=run_dir / "tool_conversations.json",
        external_tools=(*compaction.external_tools(), _probe_tool()),
        action_policy=_compaction_policy(),
        action_catalog=ActionCatalog(
            mappings=(
                *DEFAULT_MAPPINGS,
                HISTORY_READ_ACTION_MAPPING,
                ActionMapping(PROBE_TOOL, "external.read"),
            )
        ),
    )
    engine = AtlasContextEngine(
        OrchestratorConfig(),
        tool_runtime=tool_runtime,
        budget_state=budget,
        compaction=compaction,
    )
    try:
        team = engine.create_team(
            "g2b1",
            TeamConfig(
                name="g2b1",
                agents=[
                    AgentConfig(
                        name="worker",
                        model="fake",
                        provider=provider,
                        max_tokens=64,
                        provider_resilience=ProviderResilienceConfig(enabled=False),
                        execution_context=ExecutionContext(
                            principal="user:evaluation",
                            tenant_scope="atlas-evaluation",
                            classification="internal",
                            attributes={"run_id": "g2b1-run", "task_id": "g2b1-worker"},
                        ),
                    )
                ],
            ),
        )
        agent = engine._pool.get("worker")  # noqa: SLF001 - the wired agent is under test
        # The evaluation runner sets this from task lifecycle events; the snapshot hook needs it.
        token = set_current_task_id("g2b1-worker")
        try:
            run = await engine.run_tasks(
                team, [build_task(title="task-w", description="work", assignee="worker")]
            )
        finally:
            reset_current_task_id(token)
    finally:
        await engine.close()
    return run, agent, seen, compaction


def _tool_result_texts(messages: list[LLMMessage]) -> list[str]:
    from anycode.types import ToolResultBlock

    return [
        block.content
        for message in messages
        for block in message.content
        if isinstance(block, ToolResultBlock)
    ]


async def test_compaction_elides_old_results_and_keeps_the_newest_two(tmp_path: Path) -> None:
    run, agent, seen, compaction = await _run_compaction_agent(tmp_path, mode=COMPACTION_ROLLING_2)

    assert run.success is True
    # The prompt at the history.read turn: rounds 5 and 6 are the two newest tool rounds.
    at_read = _tool_result_texts(seen[7])
    assert any(OFFLOAD_MARKER in text for text in at_read)
    assert _probe_payload(6) in at_read
    assert _probe_payload(5) in at_read
    assert _probe_payload(1) not in at_read

    # The final prompt carries the page returned by history.read and has archived round 5.
    final_results = _tool_result_texts(seen[-1])
    assert any(_probe_payload(1) in text for text in final_results)
    assert any('"ref_id"' in text and '"total_chars"' in text for text in final_results)
    assert _probe_payload(5) not in final_results
    assert _probe_payload(6) in final_results

    metrics = compaction.metrics.as_dict()
    assert metrics["results_archived"] >= 1
    assert metrics["read_success"] == 1
    assert metrics["archive_failures"] == 0
    assert metrics["original_chars_archived"] > PAYLOAD_SIZE

    # M6 snapshot stores the compacted conversation, so a resume replays references.
    snapshot = json.loads((tmp_path / "run" / "tool_conversations.json").read_text("utf-8"))
    stored = json.dumps(snapshot)
    assert OFFLOAD_MARKER in stored

    # Hook order: compaction -> conversation snapshot -> budget.
    hooks = agent._hooks  # noqa: SLF001 - wiring under test
    assert isinstance(hooks[0], ToolResultCompactionHook)
    assert isinstance(hooks[1], ConversationSnapshotHook)


async def test_compaction_off_leaves_every_tool_result_intact(tmp_path: Path) -> None:
    run, agent, seen, compaction = await _run_compaction_agent(tmp_path, mode=COMPACTION_OFF)

    assert run.success is True
    results = _tool_result_texts(seen[-1])
    assert not any(OFFLOAD_MARKER in text for text in results)
    assert any(_probe_payload(1) in text for text in results)
    assert compaction.metrics.as_dict()["results_archived"] == 0
    assert not (tmp_path / "run" / "history").exists() or not list(
        (tmp_path / "run" / "history").glob("*.txt")
    )
    hooks = agent._hooks  # noqa: SLF001 - wiring under test
    assert isinstance(hooks[0], ToolResultCompactionHook)
    assert hooks[0].mode == COMPACTION_OFF


async def test_budget_hook_registered_after_compaction(tmp_path: Path) -> None:
    budget = RunBudgetState(limit_tokens=100_000)
    run, agent, seen, _ = await _run_compaction_agent(
        tmp_path, mode=COMPACTION_ROLLING_2, budget=budget
    )

    assert run.success is True
    hooks = agent._hooks  # noqa: SLF001 - wiring under test
    kinds = [type(hook).__name__ for hook in hooks]
    assert kinds.index("ToolResultCompactionHook") < kinds.index("ConversationSnapshotHook")
    assert kinds.index("ConversationSnapshotHook") < kinds.index("RunBudgetHook")

    # The budget settled one request per turn, on the compacted context.
    snapshot = budget.finalize()
    assert snapshot["requests_completed"] == len(seen)
    assert snapshot["used_prompt_tokens"] > 0
    results = _tool_result_texts(seen[-1])
    assert any(OFFLOAD_MARKER in text for text in results)
    assert _probe_payload(6) in results
