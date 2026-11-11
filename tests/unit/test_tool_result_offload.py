"""Stage G3-2 unit tests: ingress offloading of large tool results.

The store, the reader and the conversation snapshot are the real AtlasOS components; only the
tool context is a stub.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from anycode.types import LLMMessage, TextBlock, ToolResult

from atlasos.history_compaction import (
    CompactionMetrics,
    HistoryIntegrityError,
    HistoryReader,
    HistoryReadInput,
    HistoryStore,
)
from atlasos.tool_result_offload import (
    HEAD_PREVIEW_CHARS,
    INGRESS_MARKER,
    TAIL_PREVIEW_CHARS,
    TOOL_RESULT_INGRESS_OFFLOAD,
    TOOL_RESULT_OFF,
    IngressMetrics,
    ToolResultIngressOffloader,
    ToolResultIngressRuntime,
)
from atlasos.tools.conversation import ConversationStore

TASK = "eval-run-loader"
OTHER_TASK = "eval-run-validator"


def _context(task_id: str | None = TASK, *, run_id: str = "run-g32") -> Any:
    attributes = {"run_id": run_id}
    if task_id is not None:
        attributes["task_id"] = task_id
    return cast(Any, SimpleNamespace(execution_context=SimpleNamespace(attributes=attributes)))


def _store(tmp_path: Path, *, run_id: str = "run-g32") -> HistoryStore:
    return HistoryStore(tmp_path / "history", run_id=run_id)


def _offloader(
    store: HistoryStore, *, mode: str = TOOL_RESULT_INGRESS_OFFLOAD
) -> ToolResultIngressOffloader:
    return ToolResultIngressOffloader(store=store, metrics=IngressMetrics(mode=mode), mode=mode)


def _big(size: int = 9000, *, char: str = "x") -> str:
    return "".join(f"line {index:05d} {char * 20}\n" for index in range(size // 30 + 1))[:size]


def _read(
    store: HistoryStore, ref_id: str, *, task_id: str = TASK, offset: int = 0, limit: int = 3000
) -> tuple[ToolResult, Any]:
    reader = HistoryReader(store=store, metrics=CompactionMetrics())
    request = HistoryReadInput(ref_id=ref_id, offset=offset, limit=limit)
    context = _context(task_id)
    result = asyncio.run(reader.read(request, context))
    return result, reader


def _ref_of(placeholder: str) -> str:
    for line in placeholder.splitlines():
        if line.startswith("Reference: "):
            return line.split(": ", 1)[1].strip()
    raise AssertionError("placeholder has no reference line")


# ---------------------------------------------------------------- off mode


def test_off_mode_returns_the_original_result(tmp_path: Path) -> None:
    store = _store(tmp_path)
    offloader = _offloader(store, mode=TOOL_RESULT_OFF)
    content = _big()
    result = ToolResult(data=content)

    out = offloader.offload(result, tool_name="bash", task_id=TASK, call_key="k1")

    assert out is result
    assert out.data == content
    assert offloader.metrics.offloads == 0
    assert not (tmp_path / "history").exists()


# ---------------------------------------------------------------- thresholds


@pytest.mark.parametrize("size", [10, 3999, 4000])
def test_results_at_or_below_the_inline_budget_are_untouched(tmp_path: Path, size: int) -> None:
    store = _store(tmp_path)
    offloader = _offloader(store)
    content = "y" * size

    out = offloader.offload(ToolResult(data=content), tool_name="bash", task_id=TASK, call_key="k")

    assert out.data == content
    assert offloader.metrics.offloads == 0
    assert offloader.metrics.skipped_small == 1


def test_result_above_the_budget_is_archived_and_replaced(tmp_path: Path) -> None:
    store = _store(tmp_path)
    offloader = _offloader(store)
    content = _big(9000)

    out = offloader.offload(
        ToolResult(data=content), tool_name="bash", task_id=TASK, call_key="run:3:0"
    )

    assert out.data != content
    assert out.data.startswith(INGRESS_MARKER)
    assert "Tool: bash" in out.data
    assert f"Original size: {len(content)} chars" in out.data
    ref_id = _ref_of(out.data)
    ref, stored = store.load(ref_id, task_id=TASK)
    assert stored == content
    assert ref.char_count == len(content)
    assert ref.sha256 in out.data
    assert ref.tool_use_id == "run:3:0"
    assert offloader.metrics.offloads == 1
    assert offloader.metrics.original_chars_archived == len(content)


# ---------------------------------------------------------------- preview


def test_head_and_tail_previews_are_exact(tmp_path: Path) -> None:
    content = "HEAD" + "m" * 5000 + "TAIL"
    offloader = _offloader(_store(tmp_path))

    out = offloader.offload(ToolResult(data=content), tool_name="bash", task_id=TASK, call_key="k")

    head = content[:HEAD_PREVIEW_CHARS]
    tail = content[-TAIL_PREVIEW_CHARS:]
    assert f"Head:\n{head}\n" in out.data
    assert f"Tail:\n{tail}" in out.data


# ---------------------------------------------------------------- recovery


def test_archived_content_is_fully_recoverable_in_pages(tmp_path: Path) -> None:
    store = _store(tmp_path)
    content = _big(9000)
    offloader = _offloader(store)
    out = offloader.offload(ToolResult(data=content), tool_name="bash", task_id=TASK, call_key="k")
    ref_id = _ref_of(out.data)

    collected = ""
    offset = 0
    while True:
        page, _ = _read(store, ref_id, offset=offset, limit=4000)
        assert page.is_error is not True
        payload = __import__("json").loads(page.data)
        collected += str(payload["content"])
        if payload["next_offset"] is None:
            break
        offset = int(payload["next_offset"])

    assert collected == content


def test_digest_failure_is_refused(tmp_path: Path) -> None:
    store = _store(tmp_path)
    offloader = _offloader(store)
    out = offloader.offload(
        ToolResult(data=_big(9000)), tool_name="bash", task_id=TASK, call_key="k"
    )
    ref_id = _ref_of(out.data)
    (tmp_path / "history" / f"{ref_id}.txt").write_text("tampered", encoding="utf-8")

    page, reader = _read(store, ref_id)

    assert page.is_error is True
    assert "history_integrity" in page.data
    assert reader.metrics.read_denied == 1
    with pytest.raises(HistoryIntegrityError):
        store.load(ref_id, task_id=TASK)


def test_other_worker_cannot_read_the_archive(tmp_path: Path) -> None:
    store = _store(tmp_path)
    offloader = _offloader(store)
    out = offloader.offload(
        ToolResult(data=_big(9000)), tool_name="bash", task_id=TASK, call_key="k"
    )
    ref_id = _ref_of(out.data)

    page, _ = _read(store, ref_id, task_id=OTHER_TASK)

    assert page.is_error is True
    assert "history_integrity" in page.data


def test_other_run_cannot_read_the_archive(tmp_path: Path) -> None:
    store = _store(tmp_path)
    offloader = _offloader(store)
    out = offloader.offload(
        ToolResult(data=_big(9000)), tool_name="bash", task_id=TASK, call_key="k"
    )
    ref_id = _ref_of(out.data)

    foreign_reader = HistoryReader(
        store=_store(tmp_path, run_id="run-other"), metrics=CompactionMetrics()
    )
    result = asyncio.run(
        foreign_reader.read(HistoryReadInput(ref_id=ref_id), _context(TASK, run_id="run-other"))
    )

    assert result.is_error is True
    assert result.data.startswith(("history_denied", "history_integrity", "history_not_found"))
    assert foreign_reader.metrics.read_denied == 1


# ---------------------------------------------------------------- failure paths


def test_archive_failure_keeps_the_original_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    offloader = _offloader(store)
    content = _big(9000)

    def boom(**_: Any) -> Any:
        raise OSError("disk full")

    monkeypatch.setattr(store, "put", boom)

    out = offloader.offload(ToolResult(data=content), tool_name="bash", task_id=TASK, call_key="k")

    assert out.data == content
    assert offloader.metrics.archive_failures == 1
    assert offloader.metrics.offloads == 0


def test_missing_task_identity_keeps_the_original_result(tmp_path: Path) -> None:
    offloader = _offloader(_store(tmp_path))
    content = _big(9000)

    out = offloader.offload(ToolResult(data=content), tool_name="bash", task_id=None, call_key="k")

    assert out.data == content
    assert offloader.metrics.skipped_no_identity == 1


def test_already_offloaded_result_is_not_archived_again(tmp_path: Path) -> None:
    store = _store(tmp_path)
    offloader = _offloader(store)
    content = _big(9000)
    first = offloader.offload(
        ToolResult(data=content), tool_name="bash", task_id=TASK, call_key="k"
    )

    second = offloader.offload(
        ToolResult(data=first.data), tool_name="bash", task_id=TASK, call_key="k"
    )

    assert second.data == first.data
    assert offloader.metrics.offloads == 1
    assert offloader.metrics.skipped_already_offloaded == 1


def test_history_read_results_are_never_offloaded(tmp_path: Path) -> None:
    store = _store(tmp_path)
    offloader = _offloader(store)
    payload = "z" * 9000

    out = offloader.offload(
        ToolResult(data=payload), tool_name="history.read", task_id=TASK, call_key="k"
    )

    assert out.data == payload
    assert offloader.metrics.skipped_recursive == 1
    assert offloader.metrics.offloads == 0


# ---------------------------------------------------------------- safety semantics


def test_error_and_retry_safe_flags_survive_offloading(tmp_path: Path) -> None:
    offloader = _offloader(_store(tmp_path))
    content = _big(9000) + "\n(exit code 2)\n"

    out = offloader.offload(
        ToolResult(data=content, is_error=True, retry_safe=True),
        tool_name="bash",
        task_id=TASK,
        call_key="k",
    )

    assert out.is_error is True
    assert out.retry_safe is True
    assert out.data.startswith(INGRESS_MARKER)
    assert "Status: error result, exit code 2" in out.data


def test_unknown_execution_state_is_not_reclassified(tmp_path: Path) -> None:
    offloader = _offloader(_store(tmp_path))

    out = offloader.offload(
        ToolResult(
            data="sandbox execution failed: transport error", is_error=True, retry_safe=False
        ),
        tool_name="bash",
        task_id=TASK,
        call_key="k",
    )

    assert out.is_error is True
    assert out.retry_safe is False
    assert "sandbox execution failed" in out.data


def test_large_error_keeps_a_deterministic_diagnostic_excerpt(tmp_path: Path) -> None:
    traceback = (
        "collected 3 items\n"
        + "\n".join(f"noise line {index} " + "a" * 60 for index in range(60))
        + "\nTraceback (most recent call last):\n"
        + '  File "/workspace/project/test_x.py", line 4, in <module>\n'
        + "AssertionError: add(2, 3) != 5\n"
        + "(exit code 1)\n"
    )
    store = _store(tmp_path)
    offloader = _offloader(store)

    first = offloader.offload(
        ToolResult(data=traceback, is_error=True, retry_safe=True),
        tool_name="bash",
        task_id=TASK,
        call_key="k1",
    )
    second = offloader.offload(
        ToolResult(data=traceback, is_error=True, retry_safe=True),
        tool_name="bash",
        task_id=TASK,
        call_key="k2",
    )

    assert "AssertionError: add(2, 3) != 5" in first.data
    assert "Traceback (most recent call last)" in first.data
    assert "exit code 1" in first.data
    diagnostics = first.data.split("Diagnostics (excerpt from the original result):", 1)[1]
    assert len(diagnostics.split("Full output available", 1)[0].strip()) <= 400
    # Deterministic: the same input yields the same placeholder body.
    assert first.data.replace(_ref_of(first.data), "REF") == second.data.replace(
        _ref_of(second.data), "REF"
    )


# ---------------------------------------------------------------- integration seams


def test_conversation_snapshot_keeps_a_resolvable_reference(tmp_path: Path) -> None:
    store = _store(tmp_path)
    offloader = _offloader(store)
    content = _big(9000)
    out = offloader.offload(ToolResult(data=content), tool_name="bash", task_id=TASK, call_key="k")
    snapshot = ConversationStore(tmp_path / "conversation.json", run_id="run-g32")
    message = LLMMessage(role="user", content=[TextBlock(text=out.data)])
    snapshot.save(TASK, [message])

    restored = snapshot.load(TASK)
    assert restored is not None
    ref_id = _ref_of(str(restored[0]["content"][0]["text"]))
    page, _ = _read(store, ref_id)

    assert json.loads(page.data)["content"] == content[:3000]


def test_concurrent_workers_never_share_an_archive(tmp_path: Path) -> None:
    runtime = ToolResultIngressRuntime.create(
        run_dir=tmp_path, run_id="run-g32", mode=TOOL_RESULT_INGRESS_OFFLOAD
    )
    first = runtime.offloader.offload(
        ToolResult(data="a" * 9000), tool_name="bash", task_id=TASK, call_key="k1"
    )
    second = runtime.offloader.offload(
        ToolResult(data="b" * 9000), tool_name="bash", task_id=OTHER_TASK, call_key="k2"
    )

    assert _ref_of(first.data) != _ref_of(second.data)
    workers = runtime.metrics_dict()["workers"]
    assert workers[TASK]["offloads"] == 1
    assert workers[OTHER_TASK]["offloads"] == 1
    denied, _ = _read(runtime.store, _ref_of(first.data), task_id=OTHER_TASK)
    assert denied.is_error is True


def test_crlf_payload_round_trips_byte_for_byte(tmp_path: Path) -> None:
    """Windows tool output contains CRLF; the archive must not rewrite it."""

    store = _store(tmp_path)
    content = "line one\r\nline two\r\n" + "x" * 5000 + "\r\nend\r\n"

    ref = store.put(task_id=TASK, tool_use_id="k", content=content)
    _, restored = store.load(ref.ref_id, task_id=TASK)

    assert restored == content
    assert ref.char_count == len(content)


def test_unknown_mode_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        ToolResultIngressRuntime.wrap(store=_store(tmp_path), mode="summary")


def test_metrics_are_serializable(tmp_path: Path) -> None:
    runtime = ToolResultIngressRuntime.create(
        run_dir=tmp_path, run_id="run-g32", mode=TOOL_RESULT_INGRESS_OFFLOAD
    )
    runtime.offloader.offload(
        ToolResult(data=_big(9000)), tool_name="bash", task_id=TASK, call_key="k"
    )

    payload = runtime.metrics_dict()

    assert payload["mode"] == TOOL_RESULT_INGRESS_OFFLOAD
    assert payload["offloads"] == 1
    assert payload["samples"][0]["task_id"] == TASK
    assert payload["workers"][TASK]["archived_chars"] == 9000
