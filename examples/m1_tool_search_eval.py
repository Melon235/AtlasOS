"""M1 semantic tool-retrieval evaluation (keyword baseline + SentenceTransformers/FAISS)."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from atlasos.experimental.tool_search_eval import catalog_entries, evaluate
from atlasos.experiments import ExperimentStore, git_commit, git_dirty
from atlasos.tools.search import (
    KeywordToolSearchIndex,
    SemanticToolSearchIndex,
    ToolSearchUnavailable,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
STORE = REPO_ROOT / "experiments" / "runs" / "v2-m1-tool-search.jsonl"


class ToolSearchEvalRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    experiment: str
    backend: str
    embedding_model: str | None
    catalog_size: int
    query_count: int
    recall_at_1: float
    recall_at_3: float
    mrr: float
    per_query_rank: list[tuple[str, str, int | None]]
    git_commit: str | None
    git_dirty: bool
    timestamp: datetime


def main() -> None:
    dry_run = os.environ.get("ATLASOS_M1_DRY_RUN") == "1"
    if git_dirty(REPO_ROOT) and not dry_run:
        raise SystemExit("M1 formal records require a clean worktree; commit first")

    entries = catalog_entries()
    records: list[ToolSearchEvalRecord] = []
    indexes = [
        ("keyword", KeywordToolSearchIndex(entries), None),
    ]
    try:
        indexes.append(("semantic", SemanticToolSearchIndex(entries), "all-MiniLM-L6-v2"))
    except ToolSearchUnavailable as error:
        print(f"semantic backend unavailable: {error}")

    for backend, index, model_name in indexes:
        metrics = evaluate(index)
        record = ToolSearchEvalRecord(
            experiment="M1-tool-search",
            backend=backend,
            embedding_model=model_name,
            catalog_size=len(entries),
            query_count=metrics.cases,
            recall_at_1=metrics.recall_at_1,
            recall_at_3=metrics.recall_at_3,
            mrr=metrics.mrr,
            per_query_rank=list(metrics.per_query_rank),
            git_commit=git_commit(REPO_ROOT),
            git_dirty=git_dirty(REPO_ROOT),
            timestamp=datetime.now(UTC),
        )
        records.append(record)
        print(
            f"{backend}: recall@1={metrics.recall_at_1:.3f} "
            f"recall@3={metrics.recall_at_3:.3f} mrr={metrics.mrr:.3f} "
            f"catalog={len(entries)} queries={metrics.cases}"
        )
        for query, expected, rank in metrics.per_query_rank:
            if rank != 1:
                print(f"  rank={rank} {query!r} -> {expected}")

    if not dry_run:
        store = ExperimentStore(STORE)
        for record in records:
            store.append(record)


if __name__ == "__main__":
    main()
