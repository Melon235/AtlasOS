"""Recoverable compaction of a worker's own historical tool results (Stage G2b-1).

Audit context — where this plugs into AnyCode 0.9.0 (verified against the installed package, not
the upstream repository):

* ``AgentRunner.stream`` runs ``self._hook_runner.run_before_turn(conversation, ctx_info)`` and
  **assigns the result back to ``conversation``** (``core/runner.py:322``). A hook that returns a
  modified list therefore changes the persistent conversation, not just one request.
* ``HookRunner.run_before_turn`` applies hooks in registration order (``guardrails/hooks.py:21``),
  so the engine can control which hook sees which shape of the conversation.
* ``AgentConfig.context_policy`` is ``None`` in AtlasOS, so ``AgentRunner._context_manager`` is
  ``None`` and ``prepared_messages is conversation`` (``core/runner.py:346-361``). The hook output
  is exactly what the provider receives — there is no later assembly step to account for.
* ``ConversationSnapshotHook.before_turn`` persists the conversation for M6 resume
  (``atlasos/tools/conversation.py``), and ``AtlasToolAgent.run`` replays that snapshot on resume.

Design consequences:

1. Compaction runs *before* the snapshot hook, so a resumed conversation replays the same
   placeholders and `history.read` still resolves them from disk.
2. Compaction runs *before* the G2a budget hook, so admission estimates the compacted context.
3. Archives are content-addressed and written before any message is rewritten; a failed write
   leaves the original tool result untouched.

Only ``ToolResultBlock.content`` of the *same worker* is ever replaced. System prompt, task
description, assistant messages, tool-use blocks, handoff context and the two most recent tool
rounds are never touched. No LLM summarisation is involved.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from anycode.types import (
    AgentInfo,
    LLMMessage,
    LLMResponse,
    ToolDefinition,
    ToolResult,
    ToolResultBlock,
)
from pydantic import BaseModel, ConfigDict, Field

from atlasos.actions.catalog import ActionMapping

#: Compaction disabled: the conversation is passed through unchanged.
COMPACTION_OFF = "off"
#: Keep the newest two tool rounds verbatim, archive older tool results.
COMPACTION_ROLLING_2 = "rolling_2"
COMPACTION_MODES = (COMPACTION_OFF, COMPACTION_ROLLING_2)
#: Logical tool name registered in the shared M1 tool registry.
HISTORY_READ_TOOL = "history.read"
#: M3 action catalog entry. Reading an archived tool result is a read-only external action.
HISTORY_READ_ACTION_MAPPING = ActionMapping(HISTORY_READ_TOOL, "external.read")
#: Marker that identifies an already-compacted tool result.
OFFLOAD_MARKER = "[HISTORY_RESULT_OFFLOADED]"
#: Results at or below this size are not worth a reference.
MIN_COMPACT_CHARS = 800
#: Tool rounds kept verbatim by ``rolling_2``.
KEEP_ROUNDS = 2
#: Page size limits for ``history.read``.
READ_DEFAULT_LIMIT = 3000
READ_MAX_LIMIT = 4000
#: Characters of the original error kept visible when an error result is archived.
ERROR_HEAD_CHARS = 220
REF_ID_PREFIX = "hc-"
REF_ID_HEX_CHARS = 24
MAX_SAMPLES = 64
_REF_ID_RE = re.compile(r"^hc-[0-9a-f]{24}$")


class HistoryCompactionError(RuntimeError):
    """Base class for recoverable-compaction failures."""


class HistoryNotFound(HistoryCompactionError):
    """The reference is unknown to this run."""


class HistoryIntegrityError(HistoryCompactionError):
    """The stored payload does not match its metadata."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _atomic_write_text(path: Path, content: str) -> None:
    """Write *content* byte-for-byte.

    ``newline=""`` disables the platform newline translation: without it a Windows host turns
    every ``\\n`` into ``\\r\\n`` on write while the reader translates back, so any payload that
    already contains ``\\r\\n`` (tool output on Windows) would fail its own digest check.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        handle.write(content)
    os.replace(temporary, path)


def _read_text_exact(path: Path) -> str:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return handle.read()


@dataclass(frozen=True)
class HistoryRef:
    """Opaque, run-scoped reference to one archived tool result.

    ``task_id`` is the isolation key: it is written at archive time and re-checked on every read,
    so permissions survive a process restart without an in-memory registry.
    """

    ref_id: str
    run_id: str
    task_id: str
    tool_use_id: str
    sha256: str
    char_count: int
    byte_count: int
    created_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "ref_id": self.ref_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "tool_use_id": self.tool_use_id,
            "sha256": self.sha256,
            "char_count": self.char_count,
            "byte_count": self.byte_count,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> HistoryRef:
        return cls(
            ref_id=str(payload["ref_id"]),
            run_id=str(payload["run_id"]),
            task_id=str(payload["task_id"]),
            tool_use_id=str(payload["tool_use_id"]),
            sha256=str(payload["sha256"]),
            char_count=int(payload["char_count"]),
            byte_count=int(payload["byte_count"]),
            created_at=str(payload["created_at"]),
        )


class HistoryStore:
    """Run-scoped, task-isolated storage for archived tool results.

    Files live under ``<run_dir>/history``. The identifier is derived from the trusted
    ``(run_id, task_id, tool_use_id, content)`` tuple, so re-compacting the same block is
    idempotent and the reference handed to the model is an opaque ``hc-<24 hex>`` token that is
    pattern-validated before any filesystem access.
    """

    def __init__(self, root: str | Path, *, run_id: str) -> None:
        if not run_id.strip():
            raise ValueError("history store requires a trusted run id")
        self.root = Path(root)
        self.run_id = run_id

    def _payload_path(self, ref_id: str) -> Path:
        return self.root / f"{ref_id}.txt"

    def _metadata_path(self, ref_id: str) -> Path:
        return self.root / f"{ref_id}.json"

    @staticmethod
    def is_ref_id(value: str) -> bool:
        return bool(_REF_ID_RE.match(value))

    @staticmethod
    def ref_id_for(*, run_id: str, task_id: str, tool_use_id: str, content: str) -> str:
        seed = "\x00".join((run_id, task_id, tool_use_id, _digest(content)))
        return (
            f"{REF_ID_PREFIX}{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:REF_ID_HEX_CHARS]}"
        )

    def put(self, *, task_id: str, tool_use_id: str, content: str) -> HistoryRef:
        """Store *content* verbatim and return a stable reference."""

        ref_id = self.ref_id_for(
            run_id=self.run_id, task_id=task_id, tool_use_id=tool_use_id, content=content
        )
        digest = _digest(content)
        payload_path = self._payload_path(ref_id)
        metadata_path = self._metadata_path(ref_id)
        if payload_path.is_file() and metadata_path.is_file():
            existing = self.metadata(ref_id)
            if existing.sha256 != digest:
                raise HistoryIntegrityError(f"history {ref_id} already stores different content")
            return existing
        ref = HistoryRef(
            ref_id=ref_id,
            run_id=self.run_id,
            task_id=task_id,
            tool_use_id=tool_use_id,
            sha256=digest,
            char_count=len(content),
            byte_count=len(content.encode("utf-8")),
            created_at=_now(),
        )
        # Payload first: a metadata file without a payload would look readable but fail.
        _atomic_write_text(payload_path, content)
        _atomic_write_text(metadata_path, json.dumps(ref.as_dict(), indent=2, sort_keys=True))
        stored, _ = self.load(ref_id, task_id=task_id)
        return stored

    def metadata(self, ref_id: str) -> HistoryRef:
        if not self.is_ref_id(ref_id):
            raise HistoryNotFound(f"invalid history reference {ref_id!r}")
        metadata_path = self._metadata_path(ref_id)
        if not metadata_path.is_file():
            raise HistoryNotFound(f"history {ref_id} is not stored in this run")
        try:
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise HistoryIntegrityError(
                f"history {ref_id} metadata is unreadable: {error}"
            ) from error
        if not isinstance(payload, dict):
            raise HistoryIntegrityError(f"history {ref_id} metadata is not an object")
        ref = HistoryRef.from_dict(payload)
        if ref.ref_id != ref_id:
            raise HistoryIntegrityError(f"history {ref_id} metadata is inconsistent")
        if ref.run_id != self.run_id:
            raise HistoryIntegrityError(f"history {ref_id} belongs to a different run")
        return ref

    def load(self, ref_id: str, *, task_id: str | None = None) -> tuple[HistoryRef, str]:
        """Return the reference and its verified content.

        When *task_id* is provided the archive must belong to that task; the check is repeated
        here (not only in the tool layer) so no caller can bypass isolation.
        """

        ref = self.metadata(ref_id)
        if task_id is not None and ref.task_id != task_id:
            raise HistoryIntegrityError(f"history {ref_id} belongs to a different task")
        payload_path = self._payload_path(ref_id)
        if not payload_path.is_file():
            raise HistoryNotFound(f"history {ref_id} payload is missing")
        content = _read_text_exact(payload_path)
        if len(content) != ref.char_count or _digest(content) != ref.sha256:
            raise HistoryIntegrityError(f"history {ref_id} failed digest verification")
        return ref, content


@dataclass
class CompactionMetrics:
    """Counters for one run. Never empty: ``mode`` records whether compaction was active."""

    mode: str = COMPACTION_OFF
    keep_rounds: int = KEEP_ROUNDS
    min_chars: int = MIN_COMPACT_CHARS
    compaction_events: int = 0
    results_archived: int = 0
    original_chars_archived: int = 0
    placeholder_chars: int = 0
    history_bytes_stored: int = 0
    archive_failures: int = 0
    read_calls: int = 0
    read_success: int = 0
    read_failures: int = 0
    read_denied: int = 0
    _workers: dict[str, dict[str, int]] = field(default_factory=dict)
    _samples: list[dict[str, Any]] = field(default_factory=list)

    def worker(self, task_id: str) -> dict[str, int]:
        return self._workers.setdefault(
            task_id,
            {"compaction_events": 0, "results_archived": 0, "archived_chars": 0},
        )

    def record_archive(
        self,
        *,
        task_id: str,
        ref: HistoryRef,
        placeholder_chars: int,
        tool_use_id: str,
    ) -> None:
        self.results_archived += 1
        self.original_chars_archived += ref.char_count
        self.placeholder_chars += placeholder_chars
        self.history_bytes_stored += ref.byte_count
        counters = self.worker(task_id)
        counters["results_archived"] += 1
        counters["archived_chars"] += ref.char_count
        if len(self._samples) < MAX_SAMPLES:
            self._samples.append(
                {
                    "ref_id": ref.ref_id,
                    "task_id": task_id,
                    "tool_use_id": tool_use_id,
                    "original_chars": ref.char_count,
                    "placeholder_chars": placeholder_chars,
                }
            )

    def record_event(self, task_id: str) -> None:
        self.compaction_events += 1
        self.worker(task_id)["compaction_events"] += 1

    def record_archive_failure(self) -> None:
        self.archive_failures += 1

    def record_read(self, *, success: bool, denied: bool = False) -> None:
        self.read_calls += 1
        if denied:
            self.read_denied += 1
            self.read_failures += 1
        elif success:
            self.read_success += 1
        else:
            self.read_failures += 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "keep_rounds": self.keep_rounds,
            "min_chars": self.min_chars,
            "compaction_events": self.compaction_events,
            "results_archived": self.results_archived,
            "original_chars_archived": self.original_chars_archived,
            "placeholder_chars": self.placeholder_chars,
            "history_bytes_stored": self.history_bytes_stored,
            "archive_failures": self.archive_failures,
            "read_calls": self.read_calls,
            "read_success": self.read_success,
            "read_failures": self.read_failures,
            "read_denied": self.read_denied,
            "workers": {name: dict(item) for name, item in sorted(self._workers.items())},
            "samples": list(self._samples),
        }


def _placeholder(*, ref: HistoryRef, is_error: bool, error_head: str) -> str:
    lines = [
        OFFLOAD_MARKER,
        "Tool result archived.",
        f"Reference: {ref.ref_id}",
        f"Original length: {ref.char_count} chars",
        f"SHA256: {ref.sha256}",
    ]
    if is_error:
        lines.append(f"Error result (archived). Visible error excerpt: {error_head}")
    lines.append(f'Use history.read with {{"ref_id": "{ref.ref_id}"}} to retrieve the full result.')
    return "\n".join(lines)


def _round_of_message(messages: list[LLMMessage]) -> list[int]:
    """Tool-result round index per message, derived from the conversation itself.

    A round starts at each assistant message; the tool results that follow belong to it. Deriving
    this from the message list (instead of a hook-local counter) keeps the decision stable across
    process restarts and M6 resume.
    """

    rounds: list[int] = []
    current = -1
    for message in messages:
        if message.role == "assistant":
            current += 1
        rounds.append(current)
    return rounds


class ToolResultCompactionHook:
    """AnyCode ``TurnHook`` that archives and elides old tool results of one worker."""

    def __init__(
        self,
        *,
        store: HistoryStore,
        metrics: CompactionMetrics,
        task_id: str,
        mode: str = COMPACTION_ROLLING_2,
        keep_rounds: int = KEEP_ROUNDS,
        min_chars: int = MIN_COMPACT_CHARS,
    ) -> None:
        if mode not in COMPACTION_MODES:
            raise ValueError(f"unknown compaction mode {mode!r}")
        if not task_id.strip():
            raise ValueError("compaction hook requires a trusted task id")
        self._store = store
        self._metrics = metrics
        self._task_id = task_id
        self._mode = mode
        self._keep_rounds = max(0, keep_rounds)
        self._min_chars = max(0, min_chars)

    @property
    def task_id(self) -> str:
        return self._task_id

    @property
    def mode(self) -> str:
        return self._mode

    def compact(self, messages: list[LLMMessage]) -> list[LLMMessage]:
        """Return a copy-on-write conversation with old tool results elided."""

        if self._mode == COMPACTION_OFF or not messages:
            return messages
        completed = sum(1 for message in messages if message.role == "assistant")
        cutoff = completed - self._keep_rounds
        if cutoff <= 0:
            return messages
        rounds = _round_of_message(messages)
        updated: list[LLMMessage] = []
        changed = 0
        for index, message in enumerate(messages):
            if message.role == "assistant" or rounds[index] < 0 or rounds[index] >= cutoff:
                updated.append(message)
                continue
            new_blocks = []
            touched = False
            for block in message.content:
                replacement = self._compact_block(block)
                if replacement is None:
                    new_blocks.append(block)
                else:
                    new_blocks.append(replacement)
                    touched = True
                    changed += 1
            updated.append(
                message.model_copy(update={"content": new_blocks}) if touched else message
            )
        if not changed:
            return messages
        self._metrics.record_event(self._task_id)
        return updated

    def _compact_block(self, block: Any) -> ToolResultBlock | None:
        if not isinstance(block, ToolResultBlock):
            return None
        content = block.content
        if OFFLOAD_MARKER in content or len(content) <= self._min_chars:
            return None
        is_error = bool(block.is_error)
        error_head = " ".join(content.split())[:ERROR_HEAD_CHARS] if is_error else ""
        try:
            ref = self._store.put(
                task_id=self._task_id, tool_use_id=block.tool_use_id, content=content
            )
        except (HistoryCompactionError, OSError, ValueError):
            # Archiving failed: keep the original content so nothing is lost.
            self._metrics.record_archive_failure()
            return None
        placeholder = _placeholder(ref=ref, is_error=is_error, error_head=error_head)
        if len(placeholder) >= len(content):
            return None
        self._metrics.record_archive(
            task_id=self._task_id,
            ref=ref,
            placeholder_chars=len(placeholder),
            tool_use_id=block.tool_use_id,
        )
        return block.model_copy(update={"content": placeholder})

    async def before_turn(self, messages: list[LLMMessage], context: AgentInfo) -> list[LLMMessage]:
        del context
        return await asyncio.to_thread(self.compact, messages)

    async def after_turn(self, response: LLMResponse, context: AgentInfo) -> LLMResponse:
        del context
        return response


class HistoryReadInput(BaseModel):
    """Wire schema for ``history.read``. No field accepts a filesystem path."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ref_id: str = Field(min_length=1, max_length=64)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=READ_DEFAULT_LIMIT, ge=1, le=READ_MAX_LIMIT)


class HistoryReader:
    """Read-only paged access to a worker's own archived tool results.

    Identity comes from the trusted ``ExecutionContext`` of the tool call. The archive must belong
    to the same run **and** to the calling task, so concurrent workers and separate runs cannot
    cross-read.
    """

    def __init__(self, *, store: HistoryStore, metrics: CompactionMetrics) -> None:
        self.store = store
        self.metrics = metrics

    def reader_task_id(self, context: Any) -> str | None:
        return _context_attribute(context, "task_id")

    def reader_run_id(self, context: Any) -> str | None:
        return _context_attribute(context, "run_id")

    async def read(self, validated: HistoryReadInput, context: Any) -> ToolResult:
        task_id = self.reader_task_id(context)
        run_id = self.reader_run_id(context)
        if task_id is None or run_id is None:
            self.metrics.record_read(success=False, denied=True)
            return _error("history_denied: no trusted task identity for this tool call")
        if run_id != self.store.run_id:
            self.metrics.record_read(success=False, denied=True)
            return _error("history_denied: this reference belongs to a different run")
        if not self.store.is_ref_id(validated.ref_id):
            self.metrics.record_read(success=False, denied=True)
            return _error(f"history_denied: {validated.ref_id!r} is not a valid reference")
        try:
            ref, content = await asyncio.to_thread(
                self.store.load, validated.ref_id, task_id=task_id
            )
        except HistoryNotFound as error:
            self.metrics.record_read(success=False)
            return _error(f"history_not_found: {error}")
        except HistoryIntegrityError as error:
            # Covers both digest failure and cross-task access.
            self.metrics.record_read(success=False, denied=True)
            return _error(f"history_integrity: {error}")
        total = len(content)
        if validated.offset > total:
            self.metrics.record_read(success=False)
            return _error(
                f"history_invalid: offset {validated.offset} is beyond the end of "
                f"{ref.ref_id} ({total} chars)"
            )
        page = content[validated.offset : validated.offset + validated.limit][:READ_MAX_LIMIT]
        end = validated.offset + len(page)
        self.metrics.record_read(success=True)
        return ToolResult(
            data=json.dumps(
                {
                    "ref_id": ref.ref_id,
                    "tool_use_id": ref.tool_use_id,
                    "offset": validated.offset,
                    "limit": validated.limit,
                    "returned_chars": len(page),
                    "total_chars": total,
                    "next_offset": end if end < total else None,
                    "complete": end >= total and validated.offset == 0,
                    "sha256": ref.sha256,
                    "content": page,
                },
                sort_keys=True,
            ),
            is_error=False,
        )

    def tool_definition(self) -> ToolDefinition:
        async def execute(validated: HistoryReadInput, context: Any) -> ToolResult:
            return await self.read(validated, context)

        return ToolDefinition(
            name=HISTORY_READ_TOOL,
            description=(
                "Read a tool result that the runtime archived from your own earlier turns. "
                "Paged by character offset; returns the content, total length and next_offset. "
                "Only results archived for the current worker can be read."
            ),
            input_model=HistoryReadInput,
            execute=execute,  # type: ignore[arg-type]
            side_effecting=False,
        )


def _error(message: str) -> ToolResult:
    return ToolResult(data=message, is_error=True)


def _context_attribute(context: Any, key: str) -> str | None:
    execution = getattr(context, "execution_context", None)
    attributes = getattr(execution, "attributes", None)
    if not isinstance(attributes, Mapping):
        return None
    value = attributes.get(key)
    return str(value) if value is not None else None


class HistoryCompactionRuntime:
    """Per-run compaction runtime: one store, one metric record, one reader, many hooks."""

    def __init__(self, *, store: HistoryStore, metrics: CompactionMetrics, mode: str) -> None:
        self.store = store
        self.metrics = metrics
        self.mode = mode
        self.reader = HistoryReader(store=store, metrics=metrics)

    @classmethod
    def create(
        cls, *, run_dir: str | Path, run_id: str, mode: str = COMPACTION_ROLLING_2
    ) -> HistoryCompactionRuntime:
        if mode not in COMPACTION_MODES:
            raise ValueError(f"unknown compaction mode {mode!r}")
        root = Path(run_dir) / "history"
        return cls(
            store=HistoryStore(root, run_id=run_id),
            metrics=CompactionMetrics(mode=mode),
            mode=mode,
        )

    def hook_for(self, *, task_id: str) -> ToolResultCompactionHook:
        return ToolResultCompactionHook(
            store=self.store, metrics=self.metrics, task_id=task_id, mode=self.mode
        )

    def external_tools(self) -> tuple[ToolDefinition, ...]:
        """Registered in both compaction arms so the tool surface never differs."""

        return (self.reader.tool_definition(),)

    def metrics_dict(self) -> dict[str, Any]:
        return self.metrics.as_dict()


def archived_refs(messages: Iterable[LLMMessage]) -> list[str]:
    """Reference ids currently present in a conversation (used by tests and diagnostics)."""

    refs: list[str] = []
    for message in messages:
        for block in message.content:
            if isinstance(block, ToolResultBlock) and OFFLOAD_MARKER in block.content:
                for line in block.content.splitlines():
                    if line.startswith("Reference: "):
                        refs.append(line.split(": ", 1)[1].strip())
    return refs
