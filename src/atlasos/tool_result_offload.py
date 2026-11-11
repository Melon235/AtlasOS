"""Host-side ingress offloading of large tool results (Stage G3-2).

Where this plugs in (verified against the installed AnyCode 0.9.0, not the upstream repository):

* ``AgentRunner._run_single_tool_block`` calls ``self._executor.execute(...)`` and immediately
  builds ``ToolResultBlock(content=result.data, is_error=result.is_error)`` from the returned
  object (``core/runner.py:944-958``). The block is appended to the conversation verbatim at
  ``core/runner.py:731-737``. There is no hook between execution and appending, so the only
  AtlasOS-owned boundary that runs *after* the tool executed and *before* the block exists is the
  executor that AtlasOS hands to the runner: ``AtlasDispatchExecutor``
  (``atlasos/tools/dispatch.py``).
* M3 (action gate), M4 (durable bridge) and M7 (trajectory recorder) sit *inside* that executor
  (``atlasos/tools/runtime.py:203-234``), so they observe the raw ``ToolResult`` before this module
  runs. Offloading therefore cannot change an action decision, a ledger record or an execution
  outcome.
* The conversation produced here is what the M6 snapshot hook persists and what the G2a budget
  hook estimates (``atlasos/integrations/anycode_engine.py:105-123``), so the next request is
  admitted against the shortened prompt without touching budget code.

Design consequences:

1. Offloading happens once, at ingress, for the block that is about to be appended. Nothing in a
   conversation that the model has already seen is ever rewritten afterwards — there is no
   ``before_turn`` scan in this module.
2. ``is_error`` and ``retry_safe`` are preserved by ``model_copy``; only ``data`` changes.
3. The full text is archived *before* the reference is returned. A failed write returns the
   original result unchanged, so the model never receives an unreadable reference.
4. ``history.read`` results are never offloaded again, which makes the read loop impossible.

The archived archive is the same one Stage G2b-1 introduced: ``<run_dir>/history``.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from anycode.types import ToolResult

from atlasos.history_compaction import (
    HISTORY_READ_TOOL,
    OFFLOAD_MARKER,
    HistoryCompactionError,
    HistoryStore,
)

#: Ingress offloading disabled: every tool result is returned unchanged.
TOOL_RESULT_OFF = "off"
#: Archive results above the inline budget and return preview plus reference.
TOOL_RESULT_INGRESS_OFFLOAD = "ingress_offload"
TOOL_RESULT_MODES = (TOOL_RESULT_OFF, TOOL_RESULT_INGRESS_OFFLOAD)
#: Results at or below this size are inlined verbatim.
MAX_INLINE_CHARS = 4000
#: Characters kept verbatim at each end of an offloaded result.
HEAD_PREVIEW_CHARS = 200
TAIL_PREVIEW_CHARS = 200
#: Deterministic diagnostic excerpt budget for error results.
DIAGNOSTIC_MAX_CHARS = 400
DIAGNOSTIC_MAX_LINES = 6
#: Marker that identifies an already-offloaded tool result.
INGRESS_MARKER = "[TOOL_RESULT_OFFLOADED]"
#: Bounded per-run sample list kept for diagnostics.
MAX_SAMPLES = 32

_EXIT_CODE_RE = re.compile(r"exit code (\d+)")
_DIAGNOSTIC_RE = re.compile(
    r"(Traceback \(most recent call last\)"
    r"|^\s*FAILED\b"
    r"|AssertionError"
    r"|^\s*[A-Za-z_][\w.]*(Error|Exception):"
    r"|^\s*(fatal|error):"
    r"|command not found"
    r"|No such file or directory"
    r"|exit code \d+)"
)


@dataclass
class IngressMetrics:
    """Per-run ingress counters. ``mode`` records whether offloading was active."""

    mode: str = TOOL_RESULT_OFF
    max_inline_chars: int = MAX_INLINE_CHARS
    offloads: int = 0
    original_chars_archived: int = 0
    placeholder_chars: int = 0
    archive_failures: int = 0
    skipped_no_identity: int = 0
    skipped_recursive: int = 0
    skipped_already_offloaded: int = 0
    skipped_small: int = 0
    deduplicated: int = 0
    error_results_offloaded: int = 0
    _workers: dict[str, dict[str, int]] = field(default_factory=dict)
    _samples: list[dict[str, Any]] = field(default_factory=list)
    _refs: set[str] = field(default_factory=set)

    def worker(self, task_id: str) -> dict[str, int]:
        return self._workers.setdefault(
            task_id, {"offloads": 0, "archived_chars": 0, "placeholder_chars": 0}
        )

    def record_offload(
        self,
        *,
        task_id: str,
        tool_name: str,
        ref_id: str,
        original_chars: int,
        placeholder_chars: int,
        is_error: bool,
    ) -> None:
        self.offloads += 1
        self.original_chars_archived += original_chars
        self.placeholder_chars += placeholder_chars
        if ref_id in self._refs:
            # The same archived result reached ingress twice (e.g. a replayed batch).
            self.deduplicated += 1
        else:
            self._refs.add(ref_id)
        if is_error:
            self.error_results_offloaded += 1
        counters = self.worker(task_id)
        counters["offloads"] += 1
        counters["archived_chars"] += original_chars
        counters["placeholder_chars"] += placeholder_chars
        if len(self._samples) < MAX_SAMPLES:
            self._samples.append(
                {
                    "tool": tool_name,
                    "ref_id": ref_id,
                    "task_id": task_id,
                    "original_chars": original_chars,
                    "placeholder_chars": placeholder_chars,
                    "is_error": is_error,
                }
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "max_inline_chars": self.max_inline_chars,
            "offloads": self.offloads,
            "original_chars_archived": self.original_chars_archived,
            "placeholder_chars": self.placeholder_chars,
            "archive_failures": self.archive_failures,
            "skipped_no_identity": self.skipped_no_identity,
            "skipped_recursive": self.skipped_recursive,
            "skipped_already_offloaded": self.skipped_already_offloaded,
            "skipped_small": self.skipped_small,
            "deduplicated": self.deduplicated,
            "error_results_offloaded": self.error_results_offloaded,
            "workers": {name: dict(item) for name, item in sorted(self._workers.items())},
            "samples": list(self._samples),
        }


def exit_code_of(text: str) -> int | None:
    """Best-effort exit code of the *already rendered* result text (display only)."""

    match = _EXIT_CODE_RE.search(text)
    return int(match.group(1)) if match else None


def diagnostic_lines(text: str, *, max_lines: int = DIAGNOSTIC_MAX_LINES) -> str:
    """Deterministic excerpt of the diagnostic lines of an error result.

    Only original lines are extracted, in their original order, with a hard character budget:
    the same input always produces the same output and nothing is paraphrased.
    """

    matches = [line.strip() for line in text.splitlines() if _DIAGNOSTIC_RE.search(line)]
    if not matches:
        return ""
    selected = matches[-max_lines:]
    excerpt = "\n".join(selected)
    return excerpt[:DIAGNOSTIC_MAX_CHARS]


def _placeholder(
    *,
    ref_id: str,
    tool_name: str,
    sha256: str,
    original_chars: int,
    head: str,
    tail: str,
    is_error: bool,
    diagnostics: str,
) -> str:
    lines = [
        INGRESS_MARKER,
        f"Reference: {ref_id}",
        f"Tool: {tool_name}",
        f"Original size: {original_chars} chars",
        f"SHA256: {sha256}",
    ]
    if is_error:
        code = exit_code_of(f"{head}\n{tail}\n{diagnostics}")
        status = f"Status: error result, exit code {code}" if code is not None else "Status: error"
        lines.append(status)
    lines.extend(["", "Head:", head, "", "Tail:", tail])
    if diagnostics:
        lines.extend(["", "Diagnostics (excerpt from the original result):", diagnostics])
    lines.extend(["", "Full output available via history.read."])
    return "\n".join(lines)


class ToolResultIngressOffloader:
    """Archive large tool results and return a preview plus an opaque reference.

    ``offload`` is a pure decision over one ``ToolResult``; it performs the archive write and
    returns a *copy* of the result. It never re-executes a tool and never mutates the input.
    """

    def __init__(
        self,
        *,
        store: HistoryStore,
        metrics: IngressMetrics,
        mode: str = TOOL_RESULT_INGRESS_OFFLOAD,
        max_inline_chars: int = MAX_INLINE_CHARS,
        head_chars: int = HEAD_PREVIEW_CHARS,
        tail_chars: int = TAIL_PREVIEW_CHARS,
    ) -> None:
        if mode not in TOOL_RESULT_MODES:
            raise ValueError(f"unknown tool result mode {mode!r}")
        if max_inline_chars < 0 or head_chars < 0 or tail_chars < 0:
            raise ValueError("preview budgets must be >= 0")
        self._store = store
        self._metrics = metrics
        self._mode = mode
        self._max_inline_chars = max_inline_chars
        self._head_chars = head_chars
        self._tail_chars = tail_chars

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def metrics(self) -> IngressMetrics:
        return self._metrics

    def offload(
        self,
        result: ToolResult,
        *,
        tool_name: str,
        task_id: str | None,
        call_key: str | None = None,
    ) -> ToolResult:
        """Return the result the model may see: unchanged, or preview plus reference."""

        if self._mode == TOOL_RESULT_OFF:
            return result
        content: Any = result.data
        if not isinstance(content, str):
            self._metrics.skipped_small += 1
            return result
        if INGRESS_MARKER in content or OFFLOAD_MARKER in content:
            # Already a reference (this module or the Stage G2b-1 compactor): never archive twice.
            self._metrics.skipped_already_offloaded += 1
            return result
        if tool_name == HISTORY_READ_TOOL:
            # Anti-recursion: a paged read is already bounded and must stay self-describing.
            self._metrics.skipped_recursive += 1
            return result
        if len(content) <= self._max_inline_chars:
            self._metrics.skipped_small += 1
            return result
        if task_id is None or not task_id.strip():
            # Without a trusted task identity the reference could not be isolated on read.
            self._metrics.skipped_no_identity += 1
            return result

        head = content[: self._head_chars]
        tail = content[-self._tail_chars :] if self._tail_chars else ""
        is_error = bool(result.is_error)
        diagnostics = diagnostic_lines(content) if is_error else ""
        # ``call_key`` is the runtime's idempotency key (trusted, per call). Without one, the
        # content digest keeps the identity unique; the digest is part of the ref seed anyway.
        tool_use_id = call_key or f"{tool_name}:{hashlib.sha256(content.encode()).hexdigest()[:16]}"
        try:
            ref = self._store.put(
                task_id=task_id,
                tool_use_id=tool_use_id,
                content=content,
            )
        except (HistoryCompactionError, OSError, ValueError):
            # Archive failed: keep the original so nothing is lost and no dead reference is sent.
            self._metrics.archive_failures += 1
            return result
        if ref.char_count != len(content):
            self._metrics.archive_failures += 1
            return result
        placeholder = _placeholder(
            ref_id=ref.ref_id,
            tool_name=tool_name,
            sha256=ref.sha256,
            original_chars=ref.char_count,
            head=head,
            tail=tail,
            is_error=is_error,
            diagnostics=diagnostics,
        )
        if len(placeholder) >= len(content):
            # No benefit for the model: keep the original and drop the unused archive entry.
            self._metrics.skipped_small += 1
            return result
        self._metrics.record_offload(
            task_id=task_id,
            tool_name=tool_name,
            ref_id=ref.ref_id,
            original_chars=ref.char_count,
            placeholder_chars=len(placeholder),
            is_error=is_error,
        )
        return result.model_copy(update={"data": placeholder})


def task_id_of(context: Any) -> str | None:
    """Trusted task identity of a tool call, or ``None`` when the context carries none."""

    execution = getattr(context, "execution_context", None)
    attributes = getattr(execution, "attributes", None)
    if not isinstance(attributes, Mapping):
        return None
    value = attributes.get("task_id")
    return str(value) if value is not None else None


class ToolResultIngressRuntime:
    """Per-run ingress runtime: one store, one offloader, one metric record."""

    def __init__(
        self,
        *,
        store: HistoryStore,
        metrics: IngressMetrics,
        mode: str,
        max_inline_chars: int = MAX_INLINE_CHARS,
    ) -> None:
        self.store = store
        self.metrics = metrics
        self.mode = mode
        self.offloader = ToolResultIngressOffloader(
            store=store,
            metrics=metrics,
            mode=mode,
            max_inline_chars=max_inline_chars,
        )

    @classmethod
    def wrap(
        cls,
        *,
        store: HistoryStore,
        mode: str = TOOL_RESULT_OFF,
        max_inline_chars: int = MAX_INLINE_CHARS,
    ) -> ToolResultIngressRuntime:
        """Reuse an existing ``HistoryStore`` (the Stage G2b-1 archive) instead of a second one."""

        if mode not in TOOL_RESULT_MODES:
            raise ValueError(f"unknown tool result mode {mode!r}")
        return cls(
            store=store,
            metrics=IngressMetrics(mode=mode, max_inline_chars=max_inline_chars),
            mode=mode,
            max_inline_chars=max_inline_chars,
        )

    @classmethod
    def create(
        cls,
        *,
        run_dir: str | Path,
        run_id: str,
        mode: str = TOOL_RESULT_INGRESS_OFFLOAD,
        max_inline_chars: int = MAX_INLINE_CHARS,
    ) -> ToolResultIngressRuntime:
        return cls.wrap(
            store=HistoryStore(Path(run_dir) / "history", run_id=run_id),
            mode=mode,
            max_inline_chars=max_inline_chars,
        )

    def metrics_dict(self) -> dict[str, Any]:
        return self.metrics.as_dict()

    async def offload_async(
        self,
        result: ToolResult,
        *,
        tool_name: str,
        task_id: str | None,
        call_key: str | None = None,
    ) -> ToolResult:
        """Offload without blocking the event loop (the archive write is real file I/O)."""

        if self.mode == TOOL_RESULT_OFF:
            return result
        return await asyncio.to_thread(
            self.offloader.offload,
            result,
            tool_name=tool_name,
            task_id=task_id,
            call_key=call_key,
        )
