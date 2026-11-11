"""Stage G2b-1: recoverable rolling tool-result compaction."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from anycode.identity.context import ExecutionContext
from anycode.types import (
    AgentInfo,
    LLMMessage,
    LLMResponse,
    TextBlock,
    TokenUsage,
    ToolResultBlock,
    ToolUseBlock,
)

from atlasos.history_compaction import (
    COMPACTION_OFF,
    COMPACTION_ROLLING_2,
    HISTORY_READ_TOOL,
    MIN_COMPACT_CHARS,
    OFFLOAD_MARKER,
    HistoryCompactionRuntime,
    HistoryIntegrityError,
    HistoryReadInput,
    HistoryStore,
    ToolResultCompactionHook,
    archived_refs,
)

RUN_ID = "run-g2b1"
TASK_ID = "run-g2b1-worker"


def _runtime(tmp_path: Path, mode: str = COMPACTION_ROLLING_2) -> HistoryCompactionRuntime:
    return HistoryCompactionRuntime.create(run_dir=tmp_path, run_id=RUN_ID, mode=mode)


def _context(task_id: str | None, run_id: str | None = RUN_ID) -> Any:
    attributes: dict[str, Any] = {}
    if run_id is not None:
        attributes["run_id"] = run_id
    if task_id is not None:
        attributes["task_id"] = task_id
    return type(
        "Ctx",
        (),
        {
            "execution_context": ExecutionContext(
                principal="user:evaluation",
                tenant_scope="atlas-evaluation",
                classification="internal",
                attributes=attributes,
            )
        },
    )()


def _tool_use(call_id: str) -> ToolUseBlock:
    return ToolUseBlock(id=call_id, name="bash", input={"command": f"echo {call_id}"})


def _tool_result(call_id: str, content: str, *, is_error: bool | None = False) -> ToolResultBlock:
    return ToolResultBlock(tool_use_id=call_id, content=content, is_error=is_error)


def _payload(key: str, size: int) -> str:
    unit = f"{key}|"
    return (unit * (size // len(unit) + 1))[:size]


def build_conversation(rounds: int, *, size: int = 1_200, per_round: int = 1) -> list[LLMMessage]:
    """Task prompt plus *rounds* assistant/tool-result pairs, one distinct result per round."""

    messages: list[LLMMessage] = [
        LLMMessage(role="user", content=[TextBlock(text="Task: do the thing")])
    ]
    for round_index in range(rounds):
        blocks: list[Any] = [TextBlock(text=f"round {round_index}")]
        calls = []
        for slot in range(per_round):
            call_id = f"call_{round_index}_{slot}"
            blocks.append(_tool_use(call_id))
            calls.append((call_id, _payload(f"r{round_index}s{slot}", size)))
        messages.append(LLMMessage(role="assistant", content=blocks))
        messages.append(
            LLMMessage(
                role="user",
                content=[_tool_result(call_id, content) for call_id, content in calls],
            )
        )
    return messages


def _compact(hook: ToolResultCompactionHook, messages: list[LLMMessage]) -> list[LLMMessage]:
    import asyncio

    return asyncio.run(hook.before_turn(messages, AgentInfo(name="w", role="r", model="m")))


def _result_block(messages: list[LLMMessage], index: int, slot: int = 0) -> ToolResultBlock:
    block = messages[index].content[slot]
    assert isinstance(block, ToolResultBlock)
    return block


def test_off_mode_returns_the_conversation_unchanged(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, COMPACTION_OFF)
    hook = runtime.hook_for(task_id=TASK_ID)
    messages = build_conversation(6)
    assert hook.compact(messages) is messages
    assert runtime.metrics.results_archived == 0
    assert not (tmp_path / "history").exists()


def test_newest_two_rounds_stay_verbatim(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    hook = runtime.hook_for(task_id=TASK_ID)
    messages = build_conversation(5)
    compacted = hook.compact(messages)

    # Tool results live in the user messages at even indices; rounds 3 and 4 must be untouched.
    assert _result_block(compacted, 10).content == _result_block(messages, 10).content
    assert _result_block(compacted, 8).content == _result_block(messages, 8).content
    # Rounds 0..2 (indices 2, 4, 6) are archived.
    for index in (2, 4, 6):
        assert OFFLOAD_MARKER in _result_block(compacted, index).content
    assert runtime.metrics.results_archived == 3


def test_single_tool_round_is_never_compacted(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    hook = runtime.hook_for(task_id=TASK_ID)
    messages = build_conversation(2)
    assert hook.compact(messages) is messages
    assert runtime.metrics.results_archived == 0


def test_multiple_tool_results_in_one_round_share_the_round(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    hook = runtime.hook_for(task_id=TASK_ID)
    messages = build_conversation(4, per_round=2)
    compacted = hook.compact(messages)
    # Round 0 has two results (message index 2), both must be compacted together.
    results = [_result_block(compacted, 2, slot) for slot in range(2)]
    assert all(OFFLOAD_MARKER in block.content for block in results)
    assert [block.tool_use_id for block in results] == [
        _result_block(messages, 2, slot).tool_use_id for slot in range(2)
    ]
    # Round 3 (message index 8) stays verbatim.
    assert OFFLOAD_MARKER not in _result_block(compacted, 8).content


def test_short_results_are_not_compacted(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    hook = runtime.hook_for(task_id=TASK_ID)
    messages = build_conversation(6, size=MIN_COMPACT_CHARS - 10)
    assert hook.compact(messages) is messages
    assert runtime.metrics.results_archived == 0


def test_structure_is_preserved_and_error_state_survives(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    hook = runtime.hook_for(task_id=TASK_ID)
    messages = build_conversation(4)
    # Turn round 0's result into an error result.
    error_message = messages[2].model_copy(
        update={
            "content": [
                _tool_result(
                    "call_0_0", "Traceback: boom\n" + _payload("err", 1_500), is_error=True
                )
            ]
        }
    )
    messages = [messages[0], messages[1], error_message, *messages[3:]]
    compacted = hook.compact(messages)

    block = _result_block(compacted, 2)
    assert block.is_error is True
    assert block.tool_use_id == "call_0_0"
    assert "Error result" in block.content and "boom" in block.content
    # Assistant tool_use blocks are unchanged.
    tool_use = compacted[1].content[1]
    assert isinstance(tool_use, ToolUseBlock)
    assert tool_use.id == "call_0_0"
    assert len(compacted) == len(messages)


def test_archived_content_is_losslessly_recoverable(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    hook = runtime.hook_for(task_id=TASK_ID)
    messages = build_conversation(5, size=9_000, per_round=1)
    original = _result_block(messages, 2).content
    compacted = hook.compact(messages)
    ref_id = archived_refs(compacted)[0]

    pages: list[str] = []
    offset = 0
    import asyncio

    while True:
        result = asyncio.run(
            runtime.reader.read(HistoryReadInput(ref_id=ref_id, offset=offset), _context(TASK_ID))
        )
        assert result.is_error is False
        page = json.loads(result.data)
        assert page["returned_chars"] <= 4_000
        pages.append(page["content"])
        if page["next_offset"] is None:
            break
        offset = int(page["next_offset"])
    assert "".join(pages) == original
    assert runtime.metrics.read_success == len(pages)


def test_already_compacted_blocks_are_not_compacted_twice(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    hook = runtime.hook_for(task_id=TASK_ID)
    messages = build_conversation(6)
    once = hook.compact(messages)
    archived = runtime.metrics.results_archived
    twice = hook.compact(once)
    assert twice is once
    assert runtime.metrics.results_archived == archived
    files = list((tmp_path / "history").glob("*.txt"))
    assert len(files) == archived


def test_store_is_idempotent_for_identical_content(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "history", run_id=RUN_ID)
    first = store.put(task_id=TASK_ID, tool_use_id="call_1", content="same payload")
    second = store.put(task_id=TASK_ID, tool_use_id="call_1", content="same payload")
    assert first.ref_id == second.ref_id
    assert len(list((tmp_path / "history").glob("*.txt"))) == 1


def test_digest_failure_is_rejected(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    hook = runtime.hook_for(task_id=TASK_ID)
    compacted = hook.compact(build_conversation(5))
    ref_id = archived_refs(compacted)[0]
    (tmp_path / "history" / f"{ref_id}.txt").write_text("tampered", encoding="utf-8")
    with pytest.raises(HistoryIntegrityError):
        runtime.store.load(ref_id, task_id=TASK_ID)
    result = _read(runtime, ref_id, TASK_ID)
    assert result.is_error is True
    assert result.data.startswith("history_integrity:")


def _read(runtime: HistoryCompactionRuntime, ref_id: str, task_id: str | None) -> Any:
    import asyncio

    return asyncio.run(runtime.reader.read(HistoryReadInput(ref_id=ref_id), _context(task_id)))


def test_cross_worker_cross_run_and_anonymous_access_are_denied(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    hook = runtime.hook_for(task_id=TASK_ID)
    compacted = hook.compact(build_conversation(5))
    ref_id = archived_refs(compacted)[0]

    other_worker = _read(runtime, ref_id, "run-g2b1-other")
    assert other_worker.is_error is True
    assert other_worker.data.startswith("history_integrity:")

    other_run = _read(runtime, ref_id, TASK_ID) if False else None
    assert other_run is None
    foreign = type("Ctx", (), {"execution_context": None})()
    import asyncio

    denied = asyncio.run(runtime.reader.read(HistoryReadInput(ref_id=ref_id), foreign))
    assert denied.is_error is True
    assert denied.data.startswith("history_denied:")

    wrong_run = asyncio.run(
        runtime.reader.read(
            HistoryReadInput(ref_id=ref_id), _context(TASK_ID, run_id="another-run")
        )
    )
    assert wrong_run.is_error is True
    assert wrong_run.data.startswith("history_denied:")

    bad_ref = _read(runtime, "../../etc/passwd", TASK_ID)
    assert bad_ref.is_error is True
    assert bad_ref.data.startswith("history_denied:")
    assert runtime.metrics.read_denied >= 4


def test_archiving_failure_keeps_the_original_result(tmp_path: Path, monkeypatch: Any) -> None:
    runtime = _runtime(tmp_path)
    hook = runtime.hook_for(task_id=TASK_ID)
    messages = build_conversation(5)

    def explode(*args: Any, **kwargs: Any) -> None:
        raise OSError("disk is gone")

    monkeypatch.setattr(HistoryStore, "put", explode)
    compacted = hook.compact(messages)
    assert compacted is messages
    assert runtime.metrics.archive_failures == 3
    assert all(
        OFFLOAD_MARKER not in block.content
        for message in compacted
        for block in message.content
        if isinstance(block, ToolResultBlock)
    )


def test_round_boundary_is_derived_from_messages_not_a_counter(tmp_path: Path) -> None:
    """A resumed conversation (fresh hook instance) must make the same decision."""

    runtime = _runtime(tmp_path)
    messages = build_conversation(5)
    first = runtime.hook_for(task_id=TASK_ID).compact(messages)

    resumed_runtime = HistoryCompactionRuntime.create(
        run_dir=tmp_path, run_id=RUN_ID, mode=COMPACTION_ROLLING_2
    )
    second = resumed_runtime.hook_for(task_id=TASK_ID).compact(messages)
    assert [archived_refs([block]) for block in first] == [
        archived_refs([block]) for block in second
    ]
    assert archived_refs(first) == archived_refs(second)
    assert len(list((tmp_path / "history").glob("*.txt"))) == len(archived_refs(first))


def test_tool_schema_is_registered_in_both_modes(tmp_path: Path) -> None:
    off = _runtime(tmp_path / "off", COMPACTION_OFF).external_tools()
    rolling = _runtime(tmp_path / "rolling", COMPACTION_ROLLING_2).external_tools()
    assert [tool.name for tool in off] == [tool.name for tool in rolling] == [HISTORY_READ_TOOL]
    assert off[0].input_model.model_json_schema() == rolling[0].input_model.model_json_schema()


def test_hook_rejects_unknown_mode(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        HistoryCompactionRuntime.create(run_dir=tmp_path, run_id=RUN_ID, mode="aggressive")


def test_after_turn_is_a_passthrough(tmp_path: Path) -> None:
    import asyncio

    runtime = _runtime(tmp_path)
    hook = runtime.hook_for(task_id=TASK_ID)
    response = LLMResponse(
        id="r",
        content=[TextBlock(text="ok")],
        model="fake",
        stop_reason="end_turn",
        usage=TokenUsage(),
    )
    assert (
        asyncio.run(hook.after_turn(response, AgentInfo(name="w", role="r", model="m"))) is response
    )
