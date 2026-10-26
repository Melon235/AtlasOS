"""V2 M1 semantic tool retrieval evaluation on a fixed catalog."""

from __future__ import annotations

import pytest

from atlasos.experimental.tool_search_eval import catalog_entries, evaluate
from atlasos.tools import SemanticToolSearchIndex, ToolSearchUnavailable


def test_semantic_search_recall_and_determinism() -> None:
    pytest.importorskip("sentence_transformers")
    pytest.importorskip("faiss")
    entries = catalog_entries()
    try:
        index = SemanticToolSearchIndex(entries)
    except ToolSearchUnavailable as error:  # pragma: no cover - model download unavailable
        pytest.skip(str(error))

    first = evaluate(index)
    second = evaluate(index)

    assert first.cases == 24
    assert first.recall_at_3 == 1.0
    assert first.recall_at_1 >= 0.80
    assert first.mrr >= 0.90
    assert first == second
    assert index.size == 24
