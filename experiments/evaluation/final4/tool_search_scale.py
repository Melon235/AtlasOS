"""Final-4: tool-retrieval scale study at 64 and 128 tools, 32 frozen queries.

The catalog is authored for this stage (no historical M1 entry is reused). Labels are written
alongside the queries *before* the run and hashed; the digest is recorded in the results so a later
reader can verify the labels were not edited after seeing retrieval output.

Negative queries (no relevant tool) are scored separately and never enter the Recall denominator.
"""

from __future__ import annotations

import hashlib
import json
import statistics
import time
from dataclasses import dataclass

from atlasos.tools.search import (
    KeywordToolSearchIndex,
    SemanticToolSearchIndex,
    ToolSearchEntry,
)

MODEL = "all-MiniLM-L6-v2"

# (family, verb, object, extra qualifier)
_FAMILIES: tuple[tuple[str, tuple[tuple[str, str, str], ...]], ...] = (
    (
        "repo",
        (
            ("read", "file", "by repository-relative path"),
            ("write", "file", "replacing the whole content"),
            ("search", "code", "for a regular expression"),
            ("list", "directory", "one level deep"),
            ("blame", "line", "for the last author"),
            ("diff", "commit", "between two revisions"),
        ),
    ),
    (
        "issue",
        (
            ("get", "issue", "by number with comments"),
            ("create", "issue", "with a title and body"),
            ("comment", "issue", "with a markdown body"),
            ("label", "issue", "with a repository label"),
            ("close", "issue", "as completed"),
        ),
    ),
    (
        "ci",
        (
            ("fetch", "build log", "of the latest pipeline run"),
            ("rerun", "pipeline", "from a failed stage"),
            ("list", "workflow run", "for a branch"),
            ("cancel", "pipeline", "that is still running"),
        ),
    ),
    (
        "db",
        (
            ("run", "migration", "against the staging database"),
            ("query", "table", "with a read-only statement"),
            ("explain", "query plan", "for a slow statement"),
            ("seed", "fixture", "into a local database"),
        ),
    ),
    (
        "docs",
        (
            ("read", "page", "by slug"),
            ("update", "page", "body in markdown"),
            ("search", "documentation", "for a phrase"),
            ("publish", "page", "to the public site"),
        ),
    ),
    (
        "metrics",
        (
            ("query", "latency", "percentiles for a service"),
            ("query", "error rate", "over a time window"),
            ("list", "alert rule", "that fired recently"),
            ("annotate", "dashboard", "with a deployment marker"),
        ),
    ),
    (
        "release",
        (
            ("publish", "tag", "to the artifact registry"),
            ("list", "artifact", "for a version"),
            ("promote", "image", "from staging to production"),
            ("rollback", "release", "to the previous version"),
        ),
    ),
    (
        "infra",
        (
            ("restart", "pod", "by deployment and name"),
            ("read", "pod log", "for the last hour"),
            ("scale", "deployment", "to a replica count"),
            ("describe", "service", "including endpoints"),
        ),
    ),
    (
        "sec",
        (
            ("scan", "dependency", "for known vulnerabilities"),
            ("rotate", "credential", "for a service account"),
            ("list", "secret", "metadata without values"),
            ("audit", "permission", "for a repository"),
        ),
    ),
    (
        "data",
        (
            ("list", "object", "under a storage prefix"),
            ("upload", "object", "to a bucket"),
            ("export", "table", "to parquet"),
            ("sample", "dataset", "with a fixed seed"),
        ),
    ),
    (
        "test",
        (
            ("run", "suite", "for a single module"),
            ("collect", "test", "without executing"),
            ("report", "coverage", "for changed files"),
            ("quarantine", "flaky test", "for a week"),
        ),
    ),
    (
        "review",
        (
            ("request", "review", "from a code owner"),
            ("list", "pull request", "awaiting review"),
            ("approve", "pull request", "with a comment"),
            ("merge", "pull request", "with squash"),
        ),
    ),
    (
        "chat",
        (
            ("send", "message", "to a channel"),
            ("search", "message", "by keyword"),
            ("create", "thread", "under a message"),
            ("pin", "message", "in a channel"),
        ),
    ),
    (
        "ticket",
        (
            ("search", "ticket", "by free text"),
            ("create", "ticket", "with priority"),
            ("transition", "ticket", "to a new state"),
            ("assign", "ticket", "to a teammate"),
        ),
    ),
    (
        "notify",
        (
            ("send", "email", "to a recipient"),
            ("page", "on-call", "with severity"),
            ("schedule", "reminder", "for a date"),
            ("mute", "alert", "for a maintenance window"),
        ),
    ),
    (
        "build",
        (
            ("compile", "target", "with the default toolchain"),
            ("package", "wheel", "for a python distribution"),
            ("lint", "source", "with the project rules"),
            ("format", "source", "in place"),
        ),
    ),
)


def _catalog(size: int) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    index = 0
    while len(entries) < size:
        family, actions = _FAMILIES[index % len(_FAMILIES)]
        action, obj, qualifier = actions[(index // len(_FAMILIES)) % len(actions)]
        suffix = "" if index < len(_FAMILIES) else f" v{index // len(_FAMILIES) + 1}"
        name = f"{family}.{action}_{obj.replace(' ', '_')}{suffix.replace(' ', '_')}"
        description = f"{action.capitalize()} the {obj} {qualifier}."
        entries.append((name, description))
        index += 1
    return entries[:size]


@dataclass(frozen=True)
class Query:
    text: str
    expected: tuple[str, ...]
    kind: str


#: Authored before any retrieval run; hashed into the result header.
QUERIES: tuple[Query, ...] = (
    Query("read a file from the repository", ("repo.read_file",), "direct"),
    Query("show me the source of a file in the repo", ("repo.read_file",), "paraphrase"),
    Query("write a file with new content", ("repo.write_file",), "direct"),
    Query("save a whole file replacing what is there", ("repo.write_file",), "paraphrase"),
    Query("search the codebase with a regex", ("repo.search_code",), "direct"),
    Query("find where a pattern appears in the source", ("repo.search_code",), "paraphrase"),
    Query("list the files in a directory", ("repo.list_directory",), "direct"),
    Query("who last changed this line", ("repo.blame_line",), "paraphrase"),
    Query("compare two commits", ("repo.diff_commit",), "paraphrase"),
    Query("open the github issue by number", ("issue.get_issue",), "direct"),
    Query("file a new bug report", ("issue.create_issue",), "paraphrase"),
    Query("comment on an existing issue", ("issue.comment_issue",), "direct"),
    Query("add a label to the issue", ("issue.label_issue",), "direct"),
    Query(
        "close the ticket as done", ("issue.close_issue", "ticket.transition_ticket"), "ambiguous"
    ),
    Query("get the build log of the last pipeline", ("ci.fetch_build_log",), "direct"),
    Query("why did CI fail", ("ci.fetch_build_log",), "paraphrase"),
    Query("re-run the failed pipeline", ("ci.rerun_pipeline",), "direct"),
    Query("cancel a running workflow", ("ci.cancel_pipeline",), "paraphrase"),
    Query("apply the pending database migration", ("db.run_migration",), "direct"),
    Query("run a read only sql query", ("db.query_table",), "direct"),
    Query("explain why this query is slow", ("db.explain_query_plan",), "paraphrase"),
    Query("update the documentation page body", ("docs.update_page",), "direct"),
    Query("search the docs for a phrase", ("docs.search_documentation",), "direct"),
    Query("query latency percentiles for a service", ("metrics.query_latency",), "direct"),
    Query("what is the error rate over the last hour", ("metrics.query_error_rate",), "paraphrase"),
    Query("publish a tagged release", ("release.publish_tag",), "direct"),
    Query("roll back to the previous release", ("release.rollback_release",), "direct"),
    Query("restart a kubernetes pod", ("infra.restart_pod",), "direct"),
    Query("read the pod logs", ("infra.read_pod_log",), "direct"),
    Query("scan dependencies for vulnerabilities", ("sec.scan_dependency",), "direct"),
    Query("run the test suite for one module", ("test.run_suite",), "direct"),
    Query("request a review from a code owner", ("review.request_review",), "direct"),
    # Negative queries: no tool in the catalog can answer them.
    Query("translate this paragraph into Japanese", (), "no_answer"),
    Query("resize a photo to 800x600", (), "no_answer"),
    Query("book a flight to Berlin", (), "no_answer"),
    Query("calculate the square root of 17", (), "no_answer"),
    Query("play a song from my library", (), "no_answer"),
    Query("draw a bar chart of last quarter revenue", (), "no_answer"),
)


def _frozen_digest() -> str:
    payload = json.dumps(
        [[query.text, list(query.expected), query.kind] for query in QUERIES], sort_keys=True
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _entries(size: int) -> tuple[ToolSearchEntry, ...]:
    return tuple(
        ToolSearchEntry(tool_id=name, retrieval_text=f"{name} {description}")
        for name, description in _catalog(size)
    )


def _capability(tool_id: str) -> str:
    """A catalog may hold several near-identical variants of one capability.

    Retrieving any variant of the right capability counts as a hit; the canonical id is reported
    separately so the near-duplicate behaviour stays visible.
    """

    return tool_id.split("_v")[0]


def _evaluate(index: object, top_k: int) -> dict[str, float]:
    """Recall over positive queries only; negatives are scored separately."""

    positive = [query for query in QUERIES if query.kind != "no_answer"]
    hit1 = hit3 = hit5 = 0
    exact = 0
    reciprocal = 0.0
    for query in positive:
        found = index.search(query.text, top_k=5)  # type: ignore[attr-defined]
        ranked = [match.tool_id for match in found]
        rank = next(
            (
                pos
                for pos, tool_id in enumerate(ranked, start=1)
                if _capability(tool_id) in query.expected
            ),
            None,
        )
        if rank == 1:
            hit1 += 1
        if rank is not None and rank <= 3:
            hit3 += 1
        if rank is not None:
            hit5 += 1
            reciprocal += 1.0 / rank
        if ranked and ranked[0] in query.expected:
            exact += 1
    total = len(positive)
    negative = [query for query in QUERIES if query.kind == "no_answer"]
    negatives_returning_tool = sum(
        1
        for query in negative
        if index.search(query.text, top_k=top_k)  # type: ignore[attr-defined]
    )
    return {
        "positive_queries": float(total),
        "recall_at_1": round(hit1 / total, 4),
        "recall_at_3": round(hit3 / total, 4),
        "recall_at_5": round(hit5 / total, 4),
        "mrr": round(reciprocal / total, 4),
        "exact_canonical_id_at_1": round(exact / total, 4),
        "negatives_returning_any_tool": float(negatives_returning_tool),
        "negative_queries": float(len(negative)),
    }


def _latency(index: object, repeats: int = 3) -> dict[str, float]:
    samples: list[float] = []
    for _ in range(repeats):
        for query in QUERIES[:8]:
            started = time.perf_counter()
            index.search(query.text, top_k=3)  # type: ignore[attr-defined]
            samples.append((time.perf_counter() - started) * 1000.0)
    return {"warm_query_ms_median": round(statistics.median(samples), 3)}


def run() -> dict[str, object]:
    cells: list[dict[str, object]] = []
    for size in (64, 128):
        entries = _entries(size)
        started = time.perf_counter()
        keyword = KeywordToolSearchIndex(entries)
        keyword_build = (time.perf_counter() - started) * 1000.0
        started = time.perf_counter()
        semantic = SemanticToolSearchIndex(entries, model_name=MODEL)
        semantic_build = (time.perf_counter() - started) * 1000.0
        cells.append(
            {
                "catalog_size": size,
                "native_schema_chars_full": sum(
                    len(name) + len(description) + 40 for name, description in _catalog(size)
                ),
                "keyword": {
                    **_evaluate(keyword, top_k=5),
                    **_latency(keyword),
                    "index_build_ms": round(keyword_build, 3),
                },
                "semantic": {
                    **_evaluate(semantic, top_k=5),
                    **_latency(semantic),
                    "index_build_ms": round(semantic_build, 3),
                },
            }
        )
    return {
        "model": MODEL,
        "query_digest": _frozen_digest(),
        "query_count": len(QUERIES),
        "positive_query_count": sum(1 for query in QUERIES if query.kind != "no_answer"),
        "negative_query_count": sum(1 for query in QUERIES if query.kind == "no_answer"),
        "cells": cells,
    }


if __name__ == "__main__":
    print(json.dumps(run(), indent=2, sort_keys=True))
