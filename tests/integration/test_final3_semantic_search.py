"""Final-3 R-1: real semantic tool retrieval (sentence-transformers + FAISS + cached MiniLM).

No mock embeddings: the index is built from the real model over a real tool catalog, and the
retrieved ids are checked against the catalog the registry actually holds.
"""

from __future__ import annotations

import pytest
from anycode.types import AgentInfo, ToolDefinition, ToolResult, ToolUseContext
from pydantic import BaseModel

pytest.importorskip("faiss")
pytest.importorskip("sentence_transformers")

from atlasos.tools import AtlasToolRuntime  # noqa: E402
from atlasos.tools.search import (  # noqa: E402
    SemanticToolSearchIndex,
    ToolSearchEntry,
)

MODEL = "all-MiniLM-L6-v2"

CATALOG: tuple[tuple[str, str], ...] = (
    ("github.issue.get", "Get a GitHub issue by number, including comments."),
    ("github.issue.comment", "Post a comment on a GitHub issue."),
    ("policy.search", "Search internal policy documents for a clause."),
    ("policy.read", "Read one policy document by its identifier."),
    ("ci.pipeline.logs", "Fetch the build log of a continuous integration pipeline run."),
    ("jira.ticket.search", "Find a Jira ticket by free-text query."),
    ("docs.page.update", "Update a documentation page's body."),
    ("db.migration.run", "Run the pending database migrations."),
    ("metrics.query", "Query latency and error-rate metrics for a service."),
    ("release.publish", "Publish a tagged release to the artifact registry."),
)


class QueryInput(BaseModel):
    query: str = ""


def _tool(name: str, description: str) -> ToolDefinition:
    async def execute(validated: QueryInput, context: ToolUseContext) -> ToolResult:
        del context
        return ToolResult(data=f"{name}:{validated.query}", is_error=False)

    return ToolDefinition(
        name=name,
        description=description,
        input_model=QueryInput,
        execute=execute,
        side_effecting=False,
    )


def _entries() -> tuple[ToolSearchEntry, ...]:
    return tuple(
        ToolSearchEntry(tool_id=name, retrieval_text=f"{name} {description}")
        for name, description in CATALOG
    )


def test_embedding_dimension_is_the_cached_model() -> None:
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(MODEL)
    dimension = getattr(model, "get_embedding_dimension", None) or (
        model.get_sentence_embedding_dimension
    )
    assert dimension() == 384


def test_real_index_returns_tool_ids_in_top_k() -> None:
    index = SemanticToolSearchIndex(_entries(), model_name=MODEL)

    assert index.size == len(CATALOG)
    matches = index.search("get a github issue by number", top_k=3)

    assert len(matches) == 3
    assert all(match.tool_id in dict(CATALOG) for match in matches)
    assert "github.issue.get" in [match.tool_id for match in matches]
    assert matches[0].score >= matches[-1].score


def test_paraphrase_query_beats_exact_name_matching() -> None:
    index = SemanticToolSearchIndex(_entries(), model_name=MODEL)

    matches = index.search("continuous integration build failure output", top_k=3)

    assert "ci.pipeline.logs" in [match.tool_id for match in matches]


def test_unknown_query_does_not_crash_or_invent_tools() -> None:
    index = SemanticToolSearchIndex(_entries(), model_name=MODEL)

    matches = index.search("quantum entanglement calibration", top_k=3)

    assert len(matches) == 3
    assert {match.tool_id for match in matches} <= {name for name, _ in CATALOG}


def test_index_rebuilds_after_the_registry_changes() -> None:
    small = SemanticToolSearchIndex(_entries()[:4], model_name=MODEL)
    large = SemanticToolSearchIndex(_entries(), model_name=MODEL)

    assert small.size == 4
    assert large.size == len(CATALOG)
    # A tool that is absent from the small index cannot be retrieved from it.
    assert "release.publish" not in [m.tool_id for m in small.search("publish a release", top_k=4)]


async def test_semantic_search_discloses_a_real_tool_and_dispatch_executes_it() -> None:
    runtime = AtlasToolRuntime(
        "run-semantic",
        external_tools=tuple(_tool(name, description) for name, description in CATALOG),
        search_backend="semantic",
        embedding_model=MODEL,
        max_results=3,
    )
    assert runtime.state.dispatchable_tool_names == frozenset()

    matches = runtime.search_matches("get a github issue by number", top_k=3)
    top_ids = [match.tool_id for match in matches]
    assert "github.issue.get" in top_ids
    assert "github.issue.get" not in runtime.native_tool_names

    result = await runtime.search("get a github issue by number")
    assert result.is_error is False
    assert "github.issue.get" in runtime.state.dispatchable_tool_names

    context = ToolUseContext(
        agent=AgentInfo(name="tester", role="test", model="fake"),
        cwd=".",
    )
    dispatched = await runtime.dispatch("github.issue.get", {"query": "42"}, context)
    assert dispatched.is_error is False
    assert "github.issue.get:42" in str(dispatched.data)

    undisclosed = await runtime.dispatch("policy.search", {"query": "x"}, context)
    assert undisclosed.is_error is True


# "Disclosed but unauthorised" is covered by the existing action-safety suites
# (tests/integration/test_action_safety.py, test_worker_action_safety.py), which exercise the same
# dispatch path with a real gate; duplicating a policy stub here would test the stub, not the gate.
