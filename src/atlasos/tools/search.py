"""Capability retrieval for searchable tools.

The index stores vectors, tool ids, and retrieval text only; full ToolDefinitions always come
from ``ToolRegistry.get``. Retrieval text is ``tool_id + description``; schemas are deliberately
excluded because search answers "which capability do I need", not "how do I fill arguments".
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Protocol

from anycode.tools.registry import ToolRegistry

from atlasos.tools.catalog import ToolCatalog

_TOKEN_RE = re.compile(r"[a-z0-9_]+")


class ToolSearchUnavailable(RuntimeError):
    """The configured semantic search backend is not installed or usable."""


@dataclass(frozen=True)
class ToolSearchMatch:
    tool_id: str
    score: float


@dataclass(frozen=True)
class ToolSearchEntry:
    tool_id: str
    retrieval_text: str


class ToolSearchIndex(Protocol):
    def search(self, query: str, *, top_k: int = 3) -> list[ToolSearchMatch]: ...

    @property
    def size(self) -> int: ...


def _tokens(text: str) -> set[str]:
    return set(_TOKEN_RE.findall(text.lower()))


def build_entries(registry: ToolRegistry, catalog: ToolCatalog) -> tuple[ToolSearchEntry, ...]:
    entries: list[ToolSearchEntry] = []
    for name in catalog.searchable_names:
        tool = registry.get(name)
        if tool is None:
            continue
        entries.append(
            ToolSearchEntry(
                tool_id=name,
                retrieval_text=f"{name}\n{tool.description}",
            )
        )
    return tuple(entries)


class KeywordToolSearchIndex:
    """Small deterministic lexical index used for tests and as a safe fallback."""

    def __init__(self, entries: tuple[ToolSearchEntry, ...]) -> None:
        self._entries = entries
        self._tokens = {entry.tool_id: _tokens(entry.retrieval_text) for entry in entries}

    @property
    def size(self) -> int:
        return len(self._entries)

    def search(self, query: str, *, top_k: int = 3) -> list[ToolSearchMatch]:
        query_tokens = _tokens(query)
        if not query_tokens:
            return []
        scored: list[ToolSearchMatch] = []
        for entry in self._entries:
            entry_tokens = self._tokens[entry.tool_id]
            overlap = query_tokens & entry_tokens
            if not overlap:
                continue
            # Description terms dominate; tool-id matches are a smaller deterministic bonus.
            id_tokens = _tokens(entry.tool_id)
            score = len(overlap) + 0.25 * len(overlap & id_tokens)
            scored.append(ToolSearchMatch(tool_id=entry.tool_id, score=score))
        scored.sort(key=lambda match: (-match.score, match.tool_id))
        return scored[:top_k]


class MappingToolSearchIndex:
    """Explicit deterministic query -> tool ids mapping for M1.2 experiments/tests."""

    def __init__(
        self,
        mapping: dict[str, tuple[str, ...]],
        entries: tuple[ToolSearchEntry, ...],
    ) -> None:
        self._mapping = {key.lower(): value for key, value in mapping.items()}
        self._entries = entries
        self._fallback = KeywordToolSearchIndex(entries)

    @property
    def size(self) -> int:
        return len(self._entries)

    def search(self, query: str, *, top_k: int = 3) -> list[ToolSearchMatch]:
        matched = self._mapping.get(query.strip().lower())
        if matched is None:
            return self._fallback.search(query, top_k=top_k)
        return [ToolSearchMatch(tool_id=tool_id, score=1.0) for tool_id in matched[:top_k]]


_MODEL_CACHE: dict[str, Any] = {}


class SemanticToolSearchIndex:
    """SentenceTransformers embeddings with a FAISS inner-product index."""

    def __init__(
        self,
        entries: tuple[ToolSearchEntry, ...],
        *,
        model_name: str = "all-MiniLM-L6-v2",
    ) -> None:
        if not entries:
            self._entries = ()
            self._model_name = model_name
            self._model = None
            self._index = None
            self._normalized = None
            return
        self._entries = entries
        self._model_name = model_name
        self._model: Any = None
        self._index: Any = None
        self._normalized: Any = None
        self._load()

    @property
    def size(self) -> int:
        return len(self._entries)

    def _load(self) -> None:
        try:
            # Optional 'tools' extra: unresolved on purpose when the extra is absent.
            import faiss  # pyright: ignore
            import numpy as np  # pyright: ignore
            from sentence_transformers import SentenceTransformer  # pyright: ignore
        except ImportError as error:  # pragma: no cover - optional extra
            raise ToolSearchUnavailable(
                "semantic tool search requires the 'toolsearch' extra "
                "(sentence-transformers and faiss-cpu)"
            ) from error

        model = _MODEL_CACHE.get(self._model_name)
        if model is None:
            try:
                model = SentenceTransformer(self._model_name, device="cpu")
            except Exception as error:  # pragma: no cover - model/network dependent
                raise ToolSearchUnavailable(
                    f"cannot load embedding model {self._model_name!r}: {error}"
                ) from error
            model.eval()
            _MODEL_CACHE[self._model_name] = model
        self._model = model
        texts = [entry.retrieval_text for entry in self._entries]
        embeddings = np.asarray(
            model.encode(
                texts,
                normalize_embeddings=True,
                convert_to_numpy=True,
                show_progress_bar=False,
            ),
            dtype="float32",
        )
        self._normalized = embeddings
        index = faiss.IndexFlatIP(embeddings.shape[1])
        index.add(embeddings)
        self._index = index

    def search(self, query: str, *, top_k: int = 3) -> list[ToolSearchMatch]:
        import numpy as np  # pyright: ignore

        if self._index is None or self._model is None:  # pragma: no cover - defensive
            raise ToolSearchUnavailable("semantic tool index is not initialized")
        query_vector = np.asarray(
            self._model.encode(
                [query],
                normalize_embeddings=True,
                convert_to_numpy=True,
                show_progress_bar=False,
            ),
            dtype="float32",
        )
        scores, indices = self._index.search(query_vector, min(top_k, len(self._entries)))
        matches: list[ToolSearchMatch] = []
        for score, index in zip(scores[0], indices[0], strict=True):
            if index < 0:
                continue
            matches.append(
                ToolSearchMatch(tool_id=self._entries[int(index)].tool_id, score=float(score))
            )
        matches.sort(key=lambda match: (-match.score, match.tool_id))
        return matches[:top_k]


def default_model_name() -> str:
    return os.environ.get("ATLASOS_TOOL_EMBEDDING_MODEL", "all-MiniLM-L6-v2")


def build_search_index(
    registry: ToolRegistry,
    catalog: ToolCatalog,
    *,
    backend: str = "keyword",
    model_name: str | None = None,
) -> ToolSearchIndex:
    entries = build_entries(registry, catalog)
    if backend == "keyword":
        return KeywordToolSearchIndex(entries)
    if backend == "semantic":
        return SemanticToolSearchIndex(entries, model_name=model_name or default_model_name())
    raise ValueError(f"unknown tool search backend {backend!r}")
