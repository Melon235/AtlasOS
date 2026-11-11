"""Stage G1 unit tests: dual-channel handoff store, projector, and read-only tool."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from anycode import AnyCode, OrchestratorConfig, create_task
from anycode.identity.context import ExecutionContext
from anycode.tasks.queue import TaskQueue
from anycode.types import AgentInfo, ToolResult, ToolUseContext
from pydantic import ValidationError

from atlasos.handoff import (
    HANDOFF_READ_TOOL,
    INLINE_MAX_CHARS,
    INLINE_TOTAL_BUDGET_CHARS,
    PREVIEW_MAX_CHARS,
    READ_MAX_LIMIT,
    HandoffIntegrityError,
    HandoffNode,
    HandoffReadInput,
    HandoffRuntime,
    HandoffSource,
    HandoffStore,
)

RUN_ID = "run-g1"
NODES = (
    HandoffNode("p1", "task-p1"),
    HandoffNode("p2", "task-p2"),
    HandoffNode("consumer", "task-consumer", ("p1", "p2")),
)
CONSUMER_ID = f"{RUN_ID}-consumer"


def _runtime(tmp_path: Path, mode: str = "dual") -> HandoffRuntime:
    return HandoffRuntime.create(
        run_dir=tmp_path,
        run_id=RUN_ID,
        nodes=NODES,
        consumer_keys={
            CONSUMER_ID: "consumer",
            f"{RUN_ID}-p1": "p1",
            f"{RUN_ID}-p2": "p2",
        },
        mode="dual" if mode == "dual" else "legacy",
    )


def _context(task_id: str | None) -> ToolUseContext:
    attributes: dict[str, Any] = {"run_id": RUN_ID}
    if task_id is not None:
        attributes["task_id"] = task_id
    return ToolUseContext(
        agent=AgentInfo(name="worker", role="coding", model="fake"),
        execution_context=ExecutionContext(
            principal="user:evaluation",
            tenant_scope="atlas-evaluation",
            classification="internal",
            attributes=attributes,
        ),
    )


def _payload(key: str, size: int) -> str:
    """Deterministic payload whose first characters identify its producer."""

    unit = f"<{key}>"
    return (unit * (size // len(unit) + 1))[:size]


def _source(key: str, size: int) -> HandoffSource:
    return HandoffSource(key=key, title=f"task-{key}", result=_payload(key, size))


async def _read(
    runtime: HandoffRuntime, request: HandoffReadInput, context: ToolUseContext
) -> ToolResult:
    return await runtime.reader.read(request, context)


def test_short_result_is_inlined_in_full(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    payload = _payload("p1", INLINE_MAX_CHARS - 100)
    projection = runtime.projector.project(
        consumer_key="consumer", sources=[_source("p1", len(payload))]
    )
    block = projection.blocks[0]
    assert block.channel == "inline"
    assert block.original_chars == len(payload)
    assert block.projected_chars == len(payload)
    assert block.ref_id is None
    assert payload in projection.text
    assert HANDOFF_READ_TOOL not in projection.text


def test_long_result_is_offloaded_with_a_reference(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    payload = _payload("p1", 5_000)
    projection = runtime.projector.project(consumer_key="consumer", sources=[_source("p1", 5_000)])
    block = projection.blocks[0]
    assert block.channel == "offload"
    assert block.ref_id is not None
    assert block.projected_chars == PREVIEW_MAX_CHARS
    assert payload[:PREVIEW_MAX_CHARS] in projection.text
    assert block.ref_id in projection.text
    assert HANDOFF_READ_TOOL in projection.text
    stored = tmp_path / "handoffs" / f"{block.ref_id}.txt"
    assert stored.read_text(encoding="utf-8") == payload
    ref, content = runtime.store.load(block.ref_id)
    assert ref.length == len(payload)
    assert content == payload
    assert ref.digest == hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def test_paged_read_restores_the_full_result_losslessly(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    payload = _payload("p1", 9_500)
    projection = runtime.projector.project(consumer_key="consumer", sources=[_source("p1", 9_500)])
    ref_id = str(projection.blocks[0].ref_id)
    context = _context(CONSUMER_ID)

    pages: list[str] = []
    offset = 0
    while True:
        result = await _read(runtime, HandoffReadInput(ref_id=ref_id, offset=offset), context)
        assert result.is_error is False
        page = json.loads(result.data)
        assert page["returned_chars"] <= READ_MAX_LIMIT
        pages.append(page["content"])
        if page["next_offset"] is None:
            break
        offset = int(page["next_offset"])
    assert "".join(pages) == payload
    assert runtime.metrics.read_calls == len(pages)
    assert runtime.metrics.as_dict()["consumers_read_complete"] == 1


async def test_preview_plus_pages_count_as_complete_content(tmp_path: Path) -> None:
    """A consumer that continues from the preview offset has obtained the full result."""

    runtime = _runtime(tmp_path)
    payload = _payload("p1", 3_000)
    projection = runtime.projector.project(consumer_key="consumer", sources=[_source("p1", 3_000)])
    ref_id = str(projection.blocks[0].ref_id)
    assert projection.blocks[0].projected_chars == PREVIEW_MAX_CHARS

    result = await _read(
        runtime,
        HandoffReadInput(ref_id=ref_id, offset=PREVIEW_MAX_CHARS),
        _context(CONSUMER_ID),
    )
    page = json.loads(result.data)
    assert page["next_offset"] is None
    consumed = runtime.metrics.consumed_full()
    assert consumed == [
        {
            "consumer": "consumer",
            "ref_id": ref_id,
            "total_chars": 3_000,
            "preview_chars": PREVIEW_MAX_CHARS,
            "covered_chars": 3_000,
            "read_complete": True,
        }
    ]
    assert payload[:PREVIEW_MAX_CHARS] + page["content"] == payload


async def test_read_payload_rejects_oversized_limits(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    projection = runtime.projector.project(consumer_key="consumer", sources=[_source("p1", 9_000)])
    ref_id = str(projection.blocks[0].ref_id)
    with pytest.raises(ValidationError):
        HandoffReadInput(ref_id=ref_id, limit=READ_MAX_LIMIT + 1)
    result = await _read(
        runtime, HandoffReadInput(ref_id=ref_id, limit=READ_MAX_LIMIT), _context(CONSUMER_ID)
    )
    assert json.loads(result.data)["returned_chars"] <= READ_MAX_LIMIT


def test_total_inline_budget_forces_offload_for_later_dependencies(tmp_path: Path) -> None:
    nodes = (
        HandoffNode("a", "task-a"),
        HandoffNode("b", "task-b"),
        HandoffNode("c", "task-c"),
        HandoffNode("consumer", "task-consumer", ("a", "b", "c")),
    )
    runtime = HandoffRuntime.create(
        run_dir=tmp_path,
        run_id=RUN_ID,
        nodes=nodes,
        consumer_keys={CONSUMER_ID: "consumer"},
        mode="dual",
    )
    size = INLINE_MAX_CHARS - 100
    projection = runtime.projector.project(
        consumer_key="consumer", sources=[_source(key, size) for key in ("a", "b", "c")]
    )
    # 2 x (1200 - 100) = 2200 fits; the third would exceed the 3000-character budget.
    assert [block.channel for block in projection.blocks] == ["inline", "inline", "offload"]
    assert projection.inline_chars <= INLINE_TOTAL_BUDGET_CHARS
    assert projection.blocks[2].ref_id is not None
    assert runtime.metrics.as_dict()["offloaded_handoffs"] == 1


def test_store_writes_are_idempotent_and_content_addressed(tmp_path: Path) -> None:
    store = HandoffStore(tmp_path / "handoffs", run_id=RUN_ID)
    first = store.put(producer_key="p1", producer_title="task-p1", content="same payload")
    second = store.put(producer_key="p1", producer_title="task-p1", content="same payload")
    assert first.ref_id == second.ref_id
    assert len(list((tmp_path / "handoffs").glob("*.txt"))) == 1


def test_reference_ids_never_accept_host_paths(tmp_path: Path) -> None:
    store = HandoffStore(tmp_path / "handoffs", run_id=RUN_ID)
    for candidate in ("../../etc/passwd", "C:/Windows/system.ini", "ho-ZZZZ", "ho-"):
        assert store.is_ref_id(candidate) is False


async def test_digest_failure_is_rejected(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    projection = runtime.projector.project(consumer_key="consumer", sources=[_source("p1", 4_000)])
    ref_id = str(projection.blocks[0].ref_id)
    (tmp_path / "handoffs" / f"{ref_id}.txt").write_text("tampered payload", encoding="utf-8")
    with pytest.raises(HandoffIntegrityError):
        runtime.store.load(ref_id)
    result = await _read(runtime, HandoffReadInput(ref_id=ref_id), _context(CONSUMER_ID))
    assert result.is_error is True
    assert result.data.startswith("handoff_integrity:")
    assert runtime.metrics.read_failures == 1


def test_cross_run_reference_is_rejected(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    projection = runtime.projector.project(consumer_key="consumer", sources=[_source("p1", 4_000)])
    ref_id = str(projection.blocks[0].ref_id)
    foreign = HandoffStore(tmp_path / "handoffs", run_id="run-other")
    with pytest.raises(HandoffIntegrityError):
        foreign.load(ref_id)
    elsewhere = HandoffStore(tmp_path.parent / "elsewhere" / "handoffs", run_id=RUN_ID)
    with pytest.raises(Exception):  # noqa: B017 - a ref never resolves outside its run directory
        elsewhere.load(ref_id)


async def test_non_dependency_and_untrusted_access_are_rejected(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    projection = runtime.projector.project(consumer_key="consumer", sources=[_source("p2", 4_000)])
    ref_id = str(projection.blocks[0].ref_id)

    denied = await _read(runtime, HandoffReadInput(ref_id=ref_id), _context(f"{RUN_ID}-p1"))
    assert denied.is_error is True
    assert denied.data.startswith("handoff_denied:")

    anonymous = ToolUseContext(agent=AgentInfo(name="w", role="r", model="m"))
    untrusted = await _read(runtime, HandoffReadInput(ref_id=ref_id), anonymous)
    assert untrusted.data.startswith("handoff_denied:")

    traversal = await _read(
        runtime, HandoffReadInput(ref_id="../../etc/passwd"), _context(CONSUMER_ID)
    )
    assert traversal.is_error is True
    assert traversal.data.startswith("handoff_denied:")

    missing = await _read(runtime, HandoffReadInput(ref_id=f"ho-{'0' * 24}"), _context(CONSUMER_ID))
    assert missing.data.startswith("handoff_not_found:")
    assert runtime.metrics.read_denied == 4
    assert runtime.metrics.read_success == 0


def test_legacy_mode_matches_the_upstream_anycode_projection(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, "legacy")
    long_result = "x" * 900
    queue = TaskQueue()
    producer = create_task(title="task-p1", description="produce", assignee="p1")
    consumer = create_task(
        title="task-consumer",
        description="consume",
        assignee="consumer",
        depends_on=[producer.id],
    )
    queue.add(producer)
    queue.add(consumer)
    queue.complete(producer.id, long_result)

    engine = AnyCode(OrchestratorConfig())
    upstream = engine._build_task_prompt(consumer, queue)  # noqa: SLF001 - equivalence under test
    assert runtime.projector.build_prompt(consumer, queue) == upstream
    assert long_result[:500] in upstream
    assert long_result not in upstream
    assert runtime.metrics.as_dict()["offloaded_handoffs"] == 0
    assert not list((tmp_path / "handoffs").glob("*.txt"))


def test_dual_mode_prompt_carries_the_reference_for_a_long_dependency(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    long_result = _payload("p1", 6_000)
    queue = TaskQueue()
    producer = create_task(title="task-p1", description="produce", assignee="p1")
    consumer = create_task(
        title="task-consumer",
        description="consume",
        assignee="consumer",
        depends_on=[producer.id],
    )
    queue.add(producer)
    queue.add(consumer)
    queue.complete(producer.id, long_result)

    prompt = runtime.projector.build_prompt(consumer, queue)
    blocks = runtime.metrics.blocks
    assert len(blocks) == 1
    assert blocks[0]["channel"] == "offload"
    ref_id = str(blocks[0]["ref_id"])
    assert prompt.startswith("Task: task-consumer\n\nconsume")
    assert ref_id in prompt
    assert HANDOFF_READ_TOOL in prompt
    assert long_result[:PREVIEW_MAX_CHARS] in prompt
    assert long_result not in prompt
    assert runtime.graph.key_for_title("task-consumer") == "consumer"


def test_short_dependency_prompt_is_inlined_without_a_reference(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    queue = TaskQueue()
    producer = create_task(title="task-p1", description="produce", assignee="p1")
    consumer = create_task(
        title="task-consumer",
        description="consume",
        assignee="consumer",
        depends_on=[producer.id],
    )
    queue.add(producer)
    queue.add(consumer)
    queue.complete(producer.id, "short result")

    prompt = runtime.projector.build_prompt(consumer, queue)
    assert "short result" in prompt
    assert HANDOFF_READ_TOOL not in prompt
    assert runtime.metrics.as_dict()["inline_handoffs"] == 1


def test_metrics_report_result_and_channel_sizes(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    runtime.projector.project(
        consumer_key="consumer", sources=[_source("p1", 500), _source("p2", 4_000)]
    )
    payload = runtime.metrics.as_dict()
    assert payload["result_chars"] == 4_500
    assert payload["inline_chars"] == 500
    assert payload["offloaded_chars"] == 4_000
    assert payload["refs_issued"] == 1
    assert payload["projections"] == 1


def test_tool_schema_is_identical_in_both_modes(tmp_path: Path) -> None:
    legacy = _runtime(tmp_path / "legacy", "legacy").external_tools()
    dual = _runtime(tmp_path / "dual", "dual").external_tools()
    assert [tool.name for tool in legacy] == [tool.name for tool in dual] == [HANDOFF_READ_TOOL]
    assert legacy[0].input_model.model_json_schema() == dual[0].input_model.model_json_schema()
