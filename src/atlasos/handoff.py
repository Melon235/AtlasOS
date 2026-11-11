"""Dual-channel worker handoff for the AnyCode static-DAG runtime.

AnyCode 0.9.0 forwards each dependency's ``Task.result`` into the downstream task prompt truncated
to ``DEPENDENCY_CONTEXT_MAX_LENGTH`` (500) characters inside the private
``AnyCode._build_task_prompt``. AtlasOS does not patch AnyCode: it replaces only the prompt
construction seam that ``AtlasContextEngine`` already exposes (``prompt_builder``) and adds one
read-only tool to the shared M1 registry.

Two channels:

``inline``
    Short results are injected into the downstream prompt in full.

``offload``
    Long results are written verbatim under the trusted run directory; the prompt keeps a bounded
    preview plus an opaque reference the consumer can page through with ``handoff.read``.

This module owns no scheduling, no TaskQueue mutation, no checkpoint, no model call, and no
side-effect semantics. The read tool is registered as an ordinary searchable tool so it passes
through the existing M1 disclosure/dispatch path and the M3 action safety gate.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from anycode.constants import DEPENDENCY_CONTEXT_MAX_LENGTH
from anycode.types import ToolDefinition, ToolResult
from pydantic import BaseModel, ConfigDict, Field

from atlasos.actions.catalog import ActionMapping

HandoffMode = Literal["legacy", "dual"]
HandoffChannel = Literal["inline", "offload"]

#: AnyCode's own dependency projection limit; the legacy channel reproduces it exactly.
LEGACY_MAX_CHARS = DEPENDENCY_CONTEXT_MAX_LENGTH
#: A single dependency result at or below this size is inlined in full.
INLINE_MAX_CHARS = 1_200
#: Total characters of inlined dependency results across one consumer prompt.
INLINE_TOTAL_BUDGET_CHARS = 3_000
#: Prompt preview length for an offloaded result.
PREVIEW_MAX_CHARS = 300
#: Default and maximum page size returned by ``handoff.read``.
READ_DEFAULT_LIMIT = 3_000
READ_MAX_LIMIT = 4_000
#: Hard ceiling on a single returned page, enforced regardless of ``limit``.
READ_MAX_RETURN_CHARS = 4_000

REF_ID_PREFIX = "ho-"
REF_ID_HEX_CHARS = 24
_REF_ID_RE = re.compile(rf"^{REF_ID_PREFIX}[0-9a-f]{{{REF_ID_HEX_CHARS}}}$")
_HANDOFFS_DIRNAME = "handoffs"

#: Logical tool name registered in the shared M1 tool registry.
HANDOFF_READ_TOOL = "handoff.read"
#: M3 action catalog entry. Reading a stored result is a read-only external action.
HANDOFF_READ_ACTION_MAPPING = ActionMapping(HANDOFF_READ_TOOL, "external.read")


class HandoffError(RuntimeError):
    """Base class for handoff failures that must never be silently swallowed."""


class HandoffNotFound(HandoffError):
    """The reference does not exist in this run's handoff store."""


class HandoffIntegrityError(HandoffError):
    """Stored content no longer matches its recorded digest or run identity."""


class HandoffAccessDenied(HandoffError):
    """The caller is not a trusted consumer with a direct dependency on the producer."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class HandoffRef:
    """Opaque, run-scoped reference to one stored producer result."""

    ref_id: str
    producer_key: str
    producer_title: str
    digest: str
    length: int
    created_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "ref_id": self.ref_id,
            "producer_key": self.producer_key,
            "producer_title": self.producer_title,
            "digest": self.digest,
            "length": self.length,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> HandoffRef:
        return cls(
            ref_id=str(payload["ref_id"]),
            producer_key=str(payload["producer_key"]),
            producer_title=str(payload["producer_title"]),
            digest=str(payload["digest"]),
            length=int(payload["length"]),
            created_at=str(payload["created_at"]),
        )


class HandoffStore:
    """Content-addressed, run-scoped storage for complete producer results.

    Files live under ``<run_dir>/handoffs``. The path is derived only from the trusted run
    directory and the content digest; the reference handed to the model is an opaque
    ``ho-<24 hex>`` token that is validated against a strict pattern before any filesystem access,
    so a model can never express a host path.
    """

    def __init__(self, root: str | Path, *, run_id: str) -> None:
        if not run_id.strip():
            raise ValueError("handoff store requires a trusted run id")
        self.root = Path(root)
        self.run_id = run_id

    # -- paths --

    def _payload_path(self, ref_id: str) -> Path:
        return self.root / f"{ref_id}.txt"

    def _metadata_path(self, ref_id: str) -> Path:
        return self.root / f"{ref_id}.json"

    @staticmethod
    def is_ref_id(value: str) -> bool:
        return bool(_REF_ID_RE.match(value))

    # -- write --

    def put(self, *, producer_key: str, producer_title: str, content: str) -> HandoffRef:
        """Store *content* verbatim and return a stable reference.

        The same producer result always maps to the same ``ref_id``, so repeated writes of an
        identical result are idempotent.
        """

        digest = _digest(content)
        ref_id = f"{REF_ID_PREFIX}{digest[:REF_ID_HEX_CHARS]}"
        payload_path = self._payload_path(ref_id)
        metadata_path = self._metadata_path(ref_id)
        if payload_path.exists() and metadata_path.exists():
            existing = self.metadata(ref_id)
            if existing.digest != digest:
                raise HandoffIntegrityError(f"handoff {ref_id} already stores different content")
            stored, _ = self.load(ref_id)
            return stored
        ref = HandoffRef(
            ref_id=ref_id,
            producer_key=producer_key,
            producer_title=producer_title,
            digest=digest,
            length=len(content),
            created_at=_now(),
        )
        self.root.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(payload_path, content)
        _atomic_write_text(
            metadata_path,
            json.dumps({"run_id": self.run_id, **ref.as_dict()}, indent=2, sort_keys=True),
        )
        return ref

    # -- read --

    def metadata(self, ref_id: str) -> HandoffRef:
        if not self.is_ref_id(ref_id):
            raise HandoffNotFound(f"invalid handoff reference {ref_id!r}")
        metadata_path = self._metadata_path(ref_id)
        if not metadata_path.is_file():
            raise HandoffNotFound(f"handoff {ref_id} is not stored in this run")
        try:
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise HandoffIntegrityError(
                f"handoff {ref_id} metadata is unreadable: {error}"
            ) from error
        if not isinstance(payload, dict):
            raise HandoffIntegrityError(f"handoff {ref_id} metadata is not an object")
        if payload.get("run_id") != self.run_id:
            raise HandoffIntegrityError(f"handoff {ref_id} belongs to a different run")
        ref = HandoffRef.from_dict(payload)
        if payload.get("ref_id") != ref_id:
            raise HandoffIntegrityError(f"handoff {ref_id} metadata is inconsistent")
        return ref

    def load(self, ref_id: str) -> tuple[HandoffRef, str]:
        """Return the stored reference and its verified full content."""

        ref = self.metadata(ref_id)
        payload_path = self._payload_path(ref_id)
        if not payload_path.is_file():
            raise HandoffNotFound(f"handoff {ref_id} payload is missing")
        content = payload_path.read_text(encoding="utf-8")
        if _digest(content) != ref.digest:
            raise HandoffIntegrityError(f"handoff {ref_id} failed digest verification")
        if len(content) != ref.length:
            raise HandoffIntegrityError(f"handoff {ref_id} length does not match its metadata")
        return ref, content


def _atomic_write_text(path: Path, content: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


@dataclass(frozen=True)
class HandoffNode:
    """One trusted workflow node: a stable key, prompt-visible title, and direct dependencies."""

    key: str
    title: str
    depends_on: tuple[str, ...] = ()


class HandoffGraph:
    """Trusted task graph used to authorize and label handoffs.

    Built once per run from the submitted task specs, so a consumer's allowed producers never
    depend on model output, prompt text, or process-global state.
    """

    def __init__(self, nodes: Sequence[HandoffNode]) -> None:
        self._by_key = {node.key: node for node in nodes}
        self._key_by_title = {node.title: node.key for node in nodes}
        if len(self._by_key) != len(nodes) or len(self._key_by_title) != len(nodes):
            raise ValueError("handoff graph requires unique node keys and titles")

    def __len__(self) -> int:
        return len(self._by_key)

    def keys(self) -> tuple[str, ...]:
        return tuple(self._by_key)

    def node(self, key: str) -> HandoffNode | None:
        return self._by_key.get(key)

    def key_for_title(self, title: str) -> str | None:
        return self._key_by_title.get(title)

    def title_of(self, key: str) -> str:
        node = self._by_key.get(key)
        return node.title if node is not None else key

    def dependencies_of(self, key: str) -> tuple[str, ...]:
        node = self._by_key.get(key)
        return node.depends_on if node is not None else ()


@dataclass(frozen=True)
class HandoffSource:
    """A dependency result as seen by the projector (already resolved from the TaskQueue)."""

    key: str
    title: str
    result: str


@dataclass(frozen=True)
class HandoffBlock:
    key: str
    title: str
    channel: HandoffChannel
    original_chars: int
    projected_chars: int
    ref_id: str | None
    text: str


@dataclass(frozen=True)
class HandoffProjection:
    consumer_key: str
    blocks: tuple[HandoffBlock, ...]
    text: str

    @property
    def inline_chars(self) -> int:
        return sum(block.projected_chars for block in self.blocks if block.channel == "inline")

    @property
    def offloaded_chars(self) -> int:
        return sum(block.original_chars for block in self.blocks if block.channel == "offload")

    def ref_ids(self) -> tuple[str, ...]:
        return tuple(block.ref_id for block in self.blocks if block.ref_id)


class HandoffMetrics:
    """Per-run handoff counters. Observation only; never gates execution."""

    def __init__(self, *, mode: HandoffMode) -> None:
        self.mode = mode
        self.projections = 0
        self.unmapped_consumers: set[str] = set()
        self.blocks: list[dict[str, Any]] = []
        self.read_calls = 0
        self.read_success = 0
        self.read_failures = 0
        self.read_denied = 0
        self._ranges: dict[tuple[str, str], list[tuple[int, int]]] = {}
        self._lengths: dict[tuple[str, str], int] = {}
        self._preview: dict[tuple[str, str], int] = {}

    # -- projection --

    def record_projection(self, projection: HandoffProjection) -> None:
        self.projections += 1
        for block in projection.blocks:
            self.blocks.append(
                {
                    "consumer": projection.consumer_key,
                    "producer": block.key,
                    "channel": block.channel,
                    "original_chars": block.original_chars,
                    "projected_chars": block.projected_chars,
                    "ref_id": block.ref_id,
                }
            )
            if block.ref_id is not None:
                # The prompt preview is delivered context: it counts towards what the consumer
                # actually obtained, alongside the pages returned by handoff.read.
                self._preview[(projection.consumer_key, block.ref_id)] = block.projected_chars

    # -- reads --

    def record_read(
        self,
        *,
        consumer_key: str,
        ref_id: str,
        total_chars: int,
        offset: int,
        returned_chars: int,
        success: bool,
    ) -> None:
        self.read_calls += 1
        if not success:
            self.read_failures += 1
            return
        self.read_success += 1
        key = (consumer_key, ref_id)
        self._lengths[key] = total_chars
        self._ranges.setdefault(key, []).append((offset, offset + returned_chars))

    def record_denied(self) -> None:
        self.read_failures += 1
        self.read_denied += 1

    def consumed_full(self) -> list[dict[str, Any]]:
        """Report which consumers obtained every character of an offloaded result.

        Coverage is the union of the prompt preview and every page returned by ``handoff.read``.
        """

        report: list[dict[str, Any]] = []
        keys = set(self._ranges) | set(self._preview)
        for consumer_key, ref_id in sorted(keys):
            total = self._lengths.get((consumer_key, ref_id), 0)
            preview = min(self._preview.get((consumer_key, ref_id), 0), total)
            ranges = [*self._ranges.get((consumer_key, ref_id), []), (0, preview)]
            covered = _covered_chars(ranges, total)
            report.append(
                {
                    "consumer": consumer_key,
                    "ref_id": ref_id,
                    "total_chars": total,
                    "preview_chars": preview,
                    "covered_chars": covered,
                    "read_complete": total > 0 and covered >= total,
                }
            )
        return report

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "projections": self.projections,
            "result_chars": sum(int(block["original_chars"]) for block in self.blocks),
            "inline_chars": sum(
                int(block["projected_chars"])
                for block in self.blocks
                if block["channel"] == "inline"
            ),
            "offloaded_chars": sum(
                int(block["original_chars"])
                for block in self.blocks
                if block["channel"] == "offload"
            ),
            "inline_handoffs": sum(1 for block in self.blocks if block["channel"] == "inline"),
            "offloaded_handoffs": sum(1 for block in self.blocks if block["channel"] == "offload"),
            "refs_issued": sum(1 for block in self.blocks if block["ref_id"]),
            "read_calls": self.read_calls,
            "read_success": self.read_success,
            "read_failures": self.read_failures,
            "read_denied": self.read_denied,
            "unmapped_consumers": sorted(self.unmapped_consumers),
            "consumers": self.consumed_full(),
            "consumers_read_complete": sum(
                1 for item in self.consumed_full() if item["read_complete"]
            ),
            "blocks": self.blocks,
        }


def _covered_chars(ranges: Iterable[tuple[int, int]], total: int) -> int:
    covered = 0
    cursor = 0
    for start, end in sorted((max(0, s), min(total, e)) for s, e in ranges):
        start = max(start, cursor)
        if end <= cursor:
            continue
        covered += end - start
        cursor = end
    return covered


class HandoffProjector:
    """Decides HOW selected dependency results reach the consumer prompt.

    ``legacy`` reproduces ``AnyCode._build_task_prompt`` byte for byte (500-character prefix per
    dependency). ``dual`` inlines short results and offloads long ones behind a reference. The
    projector never summarizes, never calls a model, and never drops a stored result.
    """

    def __init__(
        self,
        *,
        store: HandoffStore,
        graph: HandoffGraph,
        mode: HandoffMode = "dual",
        metrics: HandoffMetrics | None = None,
    ) -> None:
        if mode not in ("legacy", "dual"):
            raise ValueError(f"unsupported handoff mode {mode!r}")
        self.store = store
        self.graph = graph
        self.mode: HandoffMode = mode
        self.metrics = metrics or HandoffMetrics(mode=mode)

    # -- pure projection --

    def project(
        self,
        *,
        consumer_key: str,
        sources: Sequence[HandoffSource],
    ) -> HandoffProjection:
        renderer = self._project_legacy if self.mode == "legacy" else self._project_dual
        projection = renderer(consumer_key=consumer_key, sources=sources)
        self.metrics.record_projection(projection)
        return projection

    def _project_legacy(
        self, *, consumer_key: str, sources: Sequence[HandoffSource]
    ) -> HandoffProjection:
        blocks: list[HandoffBlock] = []
        for source in sources:
            text = f"[{source.title}]: {source.result[:LEGACY_MAX_CHARS]}"
            blocks.append(
                HandoffBlock(
                    key=source.key,
                    title=source.title,
                    channel="inline",
                    original_chars=len(source.result),
                    projected_chars=min(len(source.result), LEGACY_MAX_CHARS),
                    ref_id=None,
                    text=text,
                )
            )
        return HandoffProjection(
            consumer_key=consumer_key,
            blocks=tuple(blocks),
            text=_heading(blocks),
        )

    def _project_dual(
        self, *, consumer_key: str, sources: Sequence[HandoffSource]
    ) -> HandoffProjection:
        blocks: list[HandoffBlock] = []
        remaining = INLINE_TOTAL_BUDGET_CHARS
        for source in sources:
            size = len(source.result)
            if size <= INLINE_MAX_CHARS and size <= remaining:
                remaining -= size
                blocks.append(
                    HandoffBlock(
                        key=source.key,
                        title=source.title,
                        channel="inline",
                        original_chars=size,
                        projected_chars=size,
                        ref_id=None,
                        text=f"[{source.title}] (complete result, {size} chars):\n{source.result}",
                    )
                )
                continue
            ref = self.store.put(
                producer_key=source.key,
                producer_title=source.title,
                content=source.result,
            )
            preview = source.result[:PREVIEW_MAX_CHARS]
            blocks.append(
                HandoffBlock(
                    key=source.key,
                    title=source.title,
                    channel="offload",
                    original_chars=size,
                    projected_chars=len(preview),
                    ref_id=ref.ref_id,
                    text=_offload_text(source.title, preview, size, ref.ref_id),
                )
            )
        return HandoffProjection(
            consumer_key=consumer_key,
            blocks=tuple(blocks),
            text=_heading(blocks),
        )

    # -- AnyCode prompt seam --

    def build_prompt(self, task: Any, queue: Any) -> str:
        """Build the downstream task prompt exactly where AnyCode would have built it."""

        consumer_key = self.graph.key_for_title(str(task.title))
        if consumer_key is None:
            consumer_key = str(task.title)
            self.metrics.unmapped_consumers.add(consumer_key)
        tasks_by_id = {str(item.id): item for item in queue.list()}
        sources: list[HandoffSource] = []
        for dep_id in task.depends_on or []:
            dependency = tasks_by_id.get(str(dep_id))
            if dependency is None or not dependency.result:
                continue
            title = str(dependency.title)
            sources.append(
                HandoffSource(
                    key=self.graph.key_for_title(title) or title,
                    title=title,
                    result=str(dependency.result),
                )
            )
        projection = self.project(consumer_key=consumer_key, sources=sources)
        return f"Task: {task.title}\n\n{task.description}{projection.text}"


def _heading(blocks: Sequence[HandoffBlock]) -> str:
    if not blocks:
        return ""
    return "\n\nRelevant output from prerequisite tasks:\n" + "\n\n".join(
        block.text for block in blocks
    )


def _offload_text(title: str, preview: str, size: int, ref_id: str) -> str:
    return (
        f"[{title}] (full result stored on the host: {size} chars; reference {ref_id})\n"
        f"{preview}\n"
        f"[preview truncated at {len(preview)} of {size} chars. The complete result was not "
        f'discarded: call the read-only tool handoff.read with {{"ref_id": "{ref_id}", '
        f'"offset": {len(preview)}}} to page through it. If handoff.read is not visible yet, '
        f'run tool.search with query "handoff.read" first.]'
    )


class HandoffReadInput(BaseModel):
    """Wire schema for ``handoff.read``. No field accepts a filesystem path."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ref_id: str = Field(min_length=1, max_length=64)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=READ_DEFAULT_LIMIT, ge=1, le=READ_MAX_LIMIT)


class HandoffReader:
    """Read-only paged access to stored producer results.

    Authorization is derived from the trusted ``ExecutionContext`` of the calling tool use: the
    consumer must be a known workflow node, and the referenced result must have been produced by
    one of that node's direct dependencies in the same run. Nothing is inferred from prompt text
    or from process-global state, so concurrent workers cannot cross-read each other.
    """

    def __init__(
        self,
        *,
        store: HandoffStore,
        graph: HandoffGraph,
        consumer_keys: Mapping[str, str],
        metrics: HandoffMetrics,
    ) -> None:
        self.store = store
        self.graph = graph
        self._consumer_keys = {str(key): str(value) for key, value in consumer_keys.items()}
        self.metrics = metrics

    # -- identity --

    def consumer_key(self, context: Any) -> str | None:
        attribute = _context_attribute(context, "task_id")
        if attribute is None:
            return None
        return self._consumer_keys.get(attribute)

    # -- execution --

    async def read(self, validated: HandoffReadInput, context: Any) -> ToolResult:
        consumer_key = self.consumer_key(context)
        if consumer_key is None:
            self.metrics.record_denied()
            return _error("handoff_denied: no trusted consumer task identity for this tool call")
        if not self.store.is_ref_id(validated.ref_id):
            self.metrics.record_denied()
            return _error(f"handoff_denied: {validated.ref_id!r} is not a valid reference")
        try:
            ref, content = await asyncio.to_thread(self.store.load, validated.ref_id)
        except HandoffNotFound as error:
            self.metrics.record_denied()
            return _error(f"handoff_not_found: {error}")
        except HandoffIntegrityError as error:
            self.metrics.record_denied()
            return _error(f"handoff_integrity: {error}")
        allowed = set(self.graph.dependencies_of(consumer_key))
        if ref.producer_key not in allowed:
            self.metrics.record_denied()
            return _error(
                f"handoff_denied: handoff {ref.ref_id} was not produced by a direct dependency "
                f"of task {consumer_key!r}",
            )
        total = len(content)
        if validated.offset > total:
            self.metrics.record_denied()
            return _error(
                f"handoff_invalid: offset {validated.offset} is beyond the end of {ref.ref_id} "
                f"({total} chars)",
            )
        page = content[validated.offset : validated.offset + validated.limit][
            :READ_MAX_RETURN_CHARS
        ]
        end = validated.offset + len(page)
        self.metrics.record_read(
            consumer_key=consumer_key,
            ref_id=ref.ref_id,
            total_chars=total,
            offset=validated.offset,
            returned_chars=len(page),
            success=True,
        )
        return ToolResult(
            data=json.dumps(
                {
                    "ref_id": ref.ref_id,
                    "producer": ref.producer_key,
                    "producer_title": ref.producer_title,
                    "offset": validated.offset,
                    "limit": validated.limit,
                    "returned_chars": len(page),
                    "total_chars": total,
                    "next_offset": end if end < total else None,
                    "complete": end >= total and validated.offset == 0,
                    "content": page,
                },
                sort_keys=True,
            ),
            is_error=False,
        )

    def tool_definition(self) -> ToolDefinition:
        async def execute(validated: HandoffReadInput, context: Any) -> ToolResult:
            return await self.read(validated, context)

        return ToolDefinition(
            name=HANDOFF_READ_TOOL,
            description=(
                "Read a complete upstream worker result that was too long to inline in this "
                "task prompt. Pass the opaque ref_id shown in the prompt plus an offset; pages "
                "are at most 3000 characters and next_offset continues from the last page. Only "
                "results produced by this task's direct dependencies in this run are readable."
            ),
            input_model=HandoffReadInput,
            execute=execute,
            side_effecting=False,
            idempotency_key_field=None,
        )


def _error(message: str) -> ToolResult:
    return ToolResult(data=message, is_error=True)


def _context_attribute(context: Any, key: str) -> str | None:
    execution_context = getattr(context, "execution_context", None)
    if execution_context is None:
        return None
    attributes = getattr(execution_context, "attributes", None)
    if not isinstance(attributes, dict):
        return None
    value = attributes.get(key)
    if value is None:
        nested = attributes.get("atlas_worker")
        if isinstance(nested, dict):
            value = nested.get(key)
    return str(value) if value is not None else None


@dataclass
class HandoffRuntime:
    """Bundle wiring the store, graph, projector and read tool for one run."""

    store: HandoffStore
    graph: HandoffGraph
    projector: HandoffProjector
    reader: HandoffReader
    metrics: HandoffMetrics
    mode: HandoffMode
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        *,
        run_dir: str | Path,
        run_id: str,
        nodes: Sequence[HandoffNode],
        consumer_keys: Mapping[str, str],
        mode: HandoffMode,
    ) -> HandoffRuntime:
        graph = HandoffGraph(nodes)
        store = HandoffStore(Path(run_dir) / _HANDOFFS_DIRNAME, run_id=run_id)
        metrics = HandoffMetrics(mode=mode)
        projector = HandoffProjector(store=store, graph=graph, mode=mode, metrics=metrics)
        reader = HandoffReader(
            store=store, graph=graph, consumer_keys=consumer_keys, metrics=metrics
        )
        return cls(
            store=store,
            graph=graph,
            projector=projector,
            reader=reader,
            metrics=metrics,
            mode=mode,
        )

    def external_tools(self) -> tuple[Any, ...]:
        """The read tool is registered in both modes so the tool schema never changes."""

        return (self.reader.tool_definition(),)

    def build_prompt(self, task: Any, queue: Any) -> str:
        return self.projector.build_prompt(task, queue)
