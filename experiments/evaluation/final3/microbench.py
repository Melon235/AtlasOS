"""Final-3 module microbenchmarks (T2): deterministic, independent of the historical workloads.

Each function returns measured numbers plus the workload it used, so the acceptance table cites a
real value instead of a historical one. No model is called and no reward is engineered: the inputs
are new, and the unfavourable cases are included on purpose.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from anycode.types import ToolDefinition
from pydantic import BaseModel

from atlasos.context import (
    InMemoryContextStore,
    NaiveExecutionOrderRenderer,
    StablePromptRenderer,
    TaskScopedSelector,
)
from atlasos.context.requirements import ContextRequirement
from atlasos.context.task_prompt import AtlasTaskPromptBuilder
from atlasos.context.transform import IdentityContextTransform
from atlasos.placement import (
    CapabilityOnlyPolicy,
    ContextLocalityPolicy,
    PlacementWeights,
    plan_workflow,
)
from atlasos.tools import AtlasToolRuntime
from atlasos.tools.catalog import DEFAULT_CORE_TOOL_NAMES
from atlasos.tools.search import (
    KeywordToolSearchIndex,
    SemanticToolSearchIndex,
    ToolSearchEntry,
)
from atlasos.workflow import AtlasAgentSpec, AtlasTaskSpec, AtlasWorkflow

MODEL_NAME = "all-MiniLM-L6-v2"


@dataclass(frozen=True)
class Measurement:
    workload_id: str
    sample_count: int
    baseline_name: str
    baseline_value: float
    treatment_name: str
    treatment_value: float
    unit: str
    overhead: str
    detail: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------- context plane


def _spec(task_id: str, depends_on: tuple[str, ...] = ()) -> AtlasTaskSpec:
    return AtlasTaskSpec(
        task_id=task_id,
        title=task_id.upper(),
        description=f"do {task_id}",
        assignee=f"worker-{task_id}",
        depends_on=depends_on,
    )


def _done(spec: AtlasTaskSpec, result: str, minute: int) -> Any:
    return SimpleNamespace(
        title=spec.title,
        assignee=spec.assignee,
        status="completed",
        result=result,
        updated_at=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=minute),
    )


def _builder(specs: list[AtlasTaskSpec], renderer: Any) -> AtlasTaskPromptBuilder:
    return AtlasTaskPromptBuilder(
        store=InMemoryContextStore(),
        selector=TaskScopedSelector(),
        renderer=renderer,
        transform=IdentityContextTransform(),
        specs_by_title={spec.title: spec for spec in specs},
        agents_by_name={spec.assignee: ("SYS", "role") for spec in specs},
    )


def _render(builder: AtlasTaskPromptBuilder, spec: AtlasTaskSpec, completed: list[Any]) -> str:
    return builder(
        SimpleNamespace(title=spec.title, assignee=spec.assignee),
        SimpleNamespace(list=lambda: list(completed)),
    )


def stable_renderer() -> Measurement:
    """Same dependency set, inverted completion order: does the layout stay identical?

    Completion order is expressed through the completion timestamps, because that is what the
    projection sorts on; varying only the list order would not change anything.
    """

    a, b, leaf = _spec("a"), _spec("b"), _spec("c", ("a", "b"))

    def _layout(renderer_kind: str, *, a_first: bool) -> str:
        renderer = (
            StablePromptRenderer(preserve_input_order=True)
            if renderer_kind == "stable"
            else NaiveExecutionOrderRenderer()
        )
        builder = _builder([a, b, leaf], renderer)
        first_minute, second_minute = (1, 2) if a_first else (2, 1)
        _render(
            builder,
            leaf,
            [_done(a, "A", first_minute), _done(b, "B", second_minute)],
        )
        return builder.last("c").context_layout_hash

    stable_same = _layout("stable", a_first=True) == _layout("stable", a_first=False)
    naive_same = _layout("naive", a_first=True) == _layout("naive", a_first=False)
    return Measurement(
        workload_id="final3-stable-order",
        sample_count=2,
        baseline_name="naive_execution_order",
        baseline_value=1.0 if naive_same else 0.0,
        treatment_name="stable_deterministic",
        treatment_value=1.0 if stable_same else 0.0,
        unit="layout_equality_rate",
        overhead="one sort per render (microseconds)",
        detail={"trials": 4},
    )


def selector_scoping() -> Measurement:
    """A task with one dependency, three completed tasks: how many irrelevant blocks enter?"""

    dep, other1, other2 = _spec("a"), _spec("x"), _spec("y")
    leaf = _spec("b", ("a",))
    builder = _builder([dep, other1, other2, leaf], StablePromptRenderer(preserve_input_order=True))
    prompt = _render(
        builder,
        leaf,
        [_done(dep, "DEP", 1), _done(other1, "IRRELEVANT-1", 2), _done(other2, "IRRELEVANT-2", 3)],
    )
    irrelevant = prompt.count("IRRELEVANT")
    return Measurement(
        workload_id="final3-selector-scope",
        sample_count=1,
        baseline_name="all-completed-outputs",
        baseline_value=2.0,
        treatment_name="task_scoped",
        treatment_value=float(irrelevant),
        unit="irrelevant_blocks_injected",
        overhead="O(blocks) membership check",
        detail={"dependency_present": "DEP" in prompt},
    )


def placement_handoff() -> Measurement:
    """A new 4-node chain: capability-only placement versus context locality."""

    tasks = []
    previous: str | None = None
    for index in range(4):
        task_id = f"t{index}"
        tasks.append(
            AtlasTaskSpec(
                task_id=task_id,
                title=f"T{index}",
                description=f"step {index}",
                assignee=f"w{index % 2}",
                depends_on=(previous,) if previous else (),
                required_context=(
                    (ContextRequirement(f"dep:{previous}", source_task_id=previous),)
                    if previous
                    else ()
                ),
            )
        )
        previous = task_id
    workflow = AtlasWorkflow(
        name="final3-chain4",
        agents=(
            AtlasAgentSpec(name="w0", system_prompt="s", role="r", capabilities=frozenset({"r"})),
            AtlasAgentSpec(name="w1", system_prompt="s", role="r", capabilities=frozenset({"r"})),
        ),
        tasks=tuple(tasks),
    )
    weights = PlacementWeights(locality=1.0, load=0.2)
    baseline = plan_workflow(workflow, policy=CapabilityOnlyPolicy(), weights=weights)
    treatment = plan_workflow(workflow, policy=ContextLocalityPolicy(), weights=weights)
    return Measurement(
        workload_id="final3-placement-chain4",
        sample_count=1,
        baseline_name="capability_only",
        baseline_value=float(baseline.summary.total_logical_handoff_refs),
        treatment_name="context_locality",
        treatment_value=float(treatment.summary.total_logical_handoff_refs),
        unit="logical_handoff_refs",
        overhead="pre-execution planning, milliseconds",
        detail={"locality": treatment.summary.mean_assignment_locality},
    )


# --------------------------------------------------------------------------- tools


class _EchoInput(BaseModel):
    query: str = ""


def _make_tool(name: str, description: str) -> ToolDefinition:
    async def execute(validated: _EchoInput, context: Any) -> Any:
        del context
        from anycode.types import ToolResult

        return ToolResult(data=f"{name}:{validated.query}", is_error=False)

    return ToolDefinition(
        name=name,
        description=description,
        input_model=_EchoInput,
        execute=execute,
        side_effecting=False,
    )


TOOL_CATALOG: tuple[tuple[str, str], ...] = (
    ("github.issue.get", "Get a GitHub issue by number, including its comments."),
    ("github.issue.comment", "Post a comment on a GitHub issue."),
    ("policy.search", "Search internal policy documents for a clause."),
    ("policy.read", "Read one policy document by its identifier."),
    ("ci.pipeline.logs", "Fetch the build log of a continuous integration pipeline run."),
    ("jira.ticket.search", "Find a Jira ticket by free-text query."),
    ("docs.page.update", "Update a documentation page's body."),
    ("db.migration.run", "Run the pending database migrations."),
    ("metrics.query", "Query latency and error-rate metrics for a service."),
    ("release.publish", "Publish a tagged release to the artifact registry."),
    ("slack.notify", "Send a message to a chat channel."),
    ("grafana.dashboard.get", "Read an observability dashboard definition."),
    ("k8s.pod.restart", "Restart a Kubernetes pod by name."),
    ("s3.object.list", "List objects under an object-storage prefix."),
    ("email.send", "Send an email to a recipient."),
    ("calendar.create", "Create a calendar event."),
    ("crm.contact.find", "Look up a customer contact record."),
    ("billing.invoice.get", "Fetch an invoice by id."),
    ("translate.text", "Translate text between two languages."),
    ("image.resize", "Resize an image to the given dimensions."),
    ("pdf.extract", "Extract text from a PDF document."),
    ("sql.query", "Run a read-only SQL query against the analytics warehouse."),
    ("feature.flag.toggle", "Enable or disable a feature flag."),
    ("incident.open", "Open an incident with a severity and summary."),
)


def tool_surface() -> Measurement:
    """24 searchable tools: native schema footprint versus full exposure."""

    runtime = AtlasToolRuntime(
        "final3-surface",
        external_tools=tuple(_make_tool(name, desc) for name, desc in TOOL_CATALOG),
    )
    native_chars = runtime.native_schema_chars()
    full_chars = sum(
        len(
            json.dumps(
                {
                    "name": definition.name,
                    "description": definition.description,
                    "input_schema": definition.input_schema,
                },
                sort_keys=True,
            )
        )
        for definition in runtime.registry.to_tool_defs()
    )
    return Measurement(
        workload_id="final3-tool-surface-24",
        sample_count=len(TOOL_CATALOG),
        baseline_name="full_exposure",
        baseline_value=float(full_chars),
        treatment_name="progressive_disclosure",
        treatment_value=float(native_chars),
        unit="schema_chars",
        overhead="one tool_search call per discovery",
        detail={
            "native_tools": len(runtime.native_tool_names),
            "core_tools": len(DEFAULT_CORE_TOOL_NAMES),
        },
    )


_QUERIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("get a github issue by number", ("github.issue.get",)),
    ("post a comment on an issue", ("github.issue.comment",)),
    ("find the policy clause about retention", ("policy.search",)),
    ("why did the continuous integration build fail", ("ci.pipeline.logs",)),
    ("look up a support ticket", ("jira.ticket.search",)),
    ("change the body of a documentation page", ("docs.page.update",)),
    ("apply pending database schema changes", ("db.migration.run",)),
    ("check service latency and error rate", ("metrics.query",)),
    ("ship a tagged build to the registry", ("release.publish",)),
    ("tell the team channel about the deploy", ("slack.notify",)),
)


def retrieval() -> Measurement:
    """Ten labelled queries over the 24-tool catalog: keyword versus real embeddings."""

    entries = tuple(
        ToolSearchEntry(tool_id=name, retrieval_text=f"{name} {description}")
        for name, description in TOOL_CATALOG
    )
    keyword = KeywordToolSearchIndex(entries)
    semantic = SemanticToolSearchIndex(entries, model_name=MODEL_NAME)
    total = len(_QUERIES)
    keyword_stats = {"hit1": 0, "hit3": 0, "mrr": 0.0}
    semantic_stats = {"hit1": 0, "hit3": 0, "mrr": 0.0}
    misses: list[str] = []

    for query, expected in _QUERIES:
        for index, stats in ((keyword, keyword_stats), (semantic, semantic_stats)):
            ranked = [match.tool_id for match in index.search(query, top_k=3)]
            rank = next(
                (
                    position
                    for position, tool_id in enumerate(ranked, start=1)
                    if tool_id in expected
                ),
                None,
            )
            if rank == 1:
                stats["hit1"] += 1
            if rank is not None:
                stats["hit3"] += 1
                stats["mrr"] += 1.0 / rank
            elif stats is semantic_stats:
                misses.append(query)

    return Measurement(
        workload_id="final3-retrieval-24x10",
        sample_count=total,
        baseline_name="keyword_recall_at_1",
        baseline_value=keyword_stats["hit1"] / total,
        treatment_name="semantic_recall_at_1",
        treatment_value=semantic_stats["hit1"] / total,
        unit="recall_at_1",
        overhead="~10 s cold model load (cached weights), milliseconds per query",
        detail={
            "keyword_recall_at_3": keyword_stats["hit3"] / total,
            "semantic_recall_at_3": semantic_stats["hit3"] / total,
            "keyword_mrr": keyword_stats["mrr"] / total,
            "semantic_mrr": semantic_stats["mrr"] / total,
            "semantic_misses": misses,
        },
    )


def run_all() -> dict[str, Measurement]:
    return {
        "stable_renderer": stable_renderer(),
        "selector_scoping": selector_scoping(),
        "placement_handoff": placement_handoff(),
        "tool_surface": tool_surface(),
        "retrieval": retrieval(),
    }
