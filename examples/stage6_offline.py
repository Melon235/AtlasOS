"""Stage 6 offline evaluation: E1 stable layout, E2 dedup matrix, E3 placement scaling.

Deterministic; no provider is called. Formal records require a clean worktree.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict
from stage6_workloads import (
    QUERY,
    SYSTEM,
    DedupWorkload,
    dedup_workload,
    describe_stats,
    layout_hash,
    parallel_chain_workflow,
    record_meta,
)

from atlasos.context import (
    ContextBlock,
    ContextKind,
    ContextRef,
    InMemoryContextStore,
    NaiveExecutionOrderRenderer,
    StablePromptRenderer,
    TaskScopedSelector,
    stable_block_key,
)
from atlasos.context.transform import ContextPilotDedupTransform, IdentityContextTransform
from atlasos.experiments import ExperimentStore, git_commit, git_dirty
from atlasos.placement import (
    CapabilityOnlyPolicy,
    ContextLocalityPolicy,
    PlacementWeights,
    plan_workflow,
)
from atlasos.workflow import AtlasTaskSpec, baseline_workflow

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNS = REPO_ROOT / "experiments" / "runs"
E1_STORE = RUNS / "stage6-e1.jsonl"
E2_OFFLINE_STORE = RUNS / "stage6-e2-offline.jsonl"
E3_STORE = RUNS / "stage6-e3.jsonl"
MODEL = "deterministic-fake"
PROVIDER = "none"
REPETITIONS = 5


class E1Record(BaseModel):
    model_config = ConfigDict(extra="forbid")

    experiment: str
    variant: str
    workload: str
    scale: str | None = None
    duplicate_ratio: float | None = None
    run_id: str
    git_commit: str | None
    git_dirty: bool
    model: str
    provider: str
    placement_policy: str
    context_policy: str
    renderer: str
    context_transform: str
    completion_order: str
    rendered_block_order: list[str]
    completion_order_observed: list[str]
    layout_hash: str | None
    stable_prefix_hash: str | None
    full_prompt_hash: str | None
    success: bool


class E2OfflineRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    experiment: str
    variant: str
    workload: str
    scale: str
    duplicate_ratio: float
    target_duplicate_ratio: float
    run_id: str
    git_commit: str | None
    git_dirty: bool
    model: str
    provider: str
    placement_policy: str
    context_policy: str
    renderer: str
    context_transform: str
    transform_name: str
    pre_transform_chars: int
    post_transform_chars: int
    reduction_ratio: float
    blocks_before: int
    blocks_after: int
    deduplicated: bool
    blocks_deduped: int
    chars_saved: int
    layout_hash_before: str
    layout_hash_after: str
    content_digest_before: str
    content_digest_after: str
    rendered_chars: int
    latency_reps_ms: list[float]
    latency_mean_ms: float
    latency_median_ms: float
    latency_min_ms: float
    latency_max_ms: float
    latency_std_ms: float
    success: bool


class E3Record(BaseModel):
    model_config = ConfigDict(extra="forbid")

    experiment: str
    variant: str
    workload: str
    scale: str | None = None
    duplicate_ratio: float | None = None
    chain_length: int
    policy: str
    run_id: str
    git_commit: str | None
    git_dirty: bool
    model: str
    provider: str
    placement_policy: str
    context_policy: str
    renderer: str
    context_transform: str
    dependency_agent_switches: int
    total_logical_handoff_refs: int
    total_logical_handoff_chars: int
    mean_assignment_locality: float
    assignment_distribution: dict[str, int]
    total_tasks: int
    success: bool


def _e1_material() -> tuple[AtlasTaskSpec, Any, dict[str, ContextBlock]]:
    workflow = baseline_workflow()
    review = workflow.task("review")
    agent = next(candidate for candidate in workflow.agents if candidate.name == "reviewer")
    blocks = {
        "research-a": ContextBlock(
            block_id="task-output:research-a",
            kind=ContextKind.DEPENDENCY_OUTPUT,
            content="out-a",
            version=1,
            source="anycode_task:research-a",
            task_id="research-a",
            owner_agent_id="researcher-a",
        ),
        "research-b": ContextBlock(
            block_id="task-output:research-b",
            kind=ContextKind.DEPENDENCY_OUTPUT,
            content="out-b",
            version=1,
            source="anycode_task:research-b",
            task_id="research-b",
            owner_agent_id="researcher-b",
        ),
    }
    return review, agent, blocks


def run_e1(store: ExperimentStore | None) -> list[E1Record]:
    """Render the same Review context under both completion orders.

    Completion order is an explicit input to the selector; no clock, delay, or concurrent
    scheduling is involved, so the layout property is tested deterministically.
    """

    review, agent, blocks = _e1_material()
    records: list[E1Record] = []
    renderers = (
        ("C1", "naive_execution_order", NaiveExecutionOrderRenderer()),
        ("C2", "stable_deterministic", StablePromptRenderer(preserve_input_order=True)),
    )
    for label, renderer_name, renderer in renderers:
        for order in ("a_first", "b_first"):
            ordered = (
                [blocks["research-a"], blocks["research-b"]]
                if order == "a_first"
                else [blocks["research-b"], blocks["research-a"]]
            )
            context_store = InMemoryContextStore()
            for block in ordered:
                context_store.put(block)
            refs = [ContextRef(block_id=block.block_id, version=block.version) for block in ordered]
            selected = TaskScopedSelector().select(
                task=review,
                agent_id="reviewer",
                store=context_store,
                available_refs=refs,
            )
            if label == "C2":
                # Mirrors the frozen runtime contract: stable order is imposed before render.
                selected = sorted(selected, key=stable_block_key)
            rendered = renderer.render(
                stable_system_prefix=agent.system_prompt,
                agent_role=agent.role,
                task=review,
                context_blocks=selected,
            )
            rendered_order = [
                ref.block_id.removeprefix("task-output:")
                for ref in rendered.context_layout
                if ref.block_id.startswith("task-output:")
            ]
            record = E1Record(
                **record_meta(
                    experiment="E1",
                    variant=label,
                    workload="fork-join",
                    run_id=uuid.uuid4().hex,
                    git_commit=git_commit(REPO_ROOT),
                    git_dirty=git_dirty(REPO_ROOT),
                    model=MODEL,
                    provider=PROVIDER,
                    placement_policy="bypass",
                    context_policy="task_scoped",
                    renderer=renderer_name,
                    context_transform="identity",
                    success=True,
                ),
                completion_order=order,
                rendered_block_order=rendered_order,
                completion_order_observed=[block.task_id or "" for block in ordered],
                layout_hash=rendered.context_layout_hash,
                stable_prefix_hash=rendered.stable_prefix_hash,
                full_prompt_hash=rendered.full_prompt_hash,
            )
            records.append(record)
            print(
                f"E1 {label} {order}: rendered={record.rendered_block_order} "
                f"input={record.completion_order_observed} layout={record.layout_hash}"
            )
    if store is not None:
        for record in records:
            store.append(record)
    return records


def _content_digest(blocks) -> str:
    digest = hashlib.sha256()
    for block in blocks:
        digest.update(block.content.encode("utf-8"))
    return digest.hexdigest()


def _render_preview(workload: DedupWorkload, blocks) -> str:
    task = AtlasTaskSpec(
        task_id=f"stress-{workload.scale}-{workload.duplicate_label}",
        title="Cache Stress",
        description=QUERY,
        assignee="stress-agent",
    )
    rendered = StablePromptRenderer(preserve_input_order=True).render(
        stable_system_prefix=SYSTEM,
        agent_role="cache-stress analyst",
        task=task,
        context_blocks=blocks,
    )
    return rendered.messages[1]["content"]


def run_e2_offline(store: ExperimentStore | None) -> list[E2OfflineRecord]:
    records: list[E2OfflineRecord] = []
    for scale in ("small", "medium", "large"):
        for duplicate_label in ("D0", "D1", "D2", "D3"):
            workload = dedup_workload(scale, duplicate_label)
            for transform_name, transform in (
                ("T0", IdentityContextTransform()),
                ("T1", ContextPilotDedupTransform()),
            ):
                latencies: list[float] = []
                first = None
                for _ in range(REPETITIONS):
                    transformed = transform.transform(blocks=workload.blocks, query=QUERY)
                    latencies.append(transformed.transform_latency_ms)
                    if first is None:
                        first = transformed
                assert first is not None
                stats = describe_stats(latencies)
                pre_chars = first.original_context_chars
                post_chars = first.optimized_context_chars
                record = E2OfflineRecord(
                    **record_meta(
                        experiment="E2-offline",
                        variant=f"{transform_name}-{scale}-{duplicate_label}",
                        workload="dedup-stress",
                        scale=scale,
                        duplicate_ratio=workload.actual_duplicate_ratio,
                        run_id=uuid.uuid4().hex,
                        git_commit=git_commit(REPO_ROOT),
                        git_dirty=git_dirty(REPO_ROOT),
                        model=MODEL,
                        provider=PROVIDER,
                        placement_policy="bypass",
                        context_policy="task_scoped",
                        renderer="stable_deterministic",
                        context_transform=first.transform_name,
                        success=first.optimized_context_chars <= first.original_context_chars,
                    ),
                    transform_name=first.transform_name,
                    pre_transform_chars=pre_chars,
                    post_transform_chars=post_chars,
                    target_duplicate_ratio=workload.target_duplicate_ratio,
                    reduction_ratio=((pre_chars - post_chars) / pre_chars if pre_chars else 0.0),
                    blocks_before=first.original_block_count,
                    blocks_after=first.optimized_block_count,
                    deduplicated=first.deduplicated,
                    blocks_deduped=int(first.metadata.get("blocks_deduped", 0)),
                    chars_saved=int(first.metadata.get("chars_saved", 0)),
                    layout_hash_before=layout_hash(workload.blocks),
                    layout_hash_after=layout_hash(list(first.blocks)),
                    content_digest_before=_content_digest(workload.blocks),
                    content_digest_after=_content_digest(first.blocks),
                    rendered_chars=len(_render_preview(workload, first.blocks)),
                    latency_reps_ms=latencies,
                    latency_mean_ms=stats["mean"],
                    latency_median_ms=stats["median"],
                    latency_min_ms=stats["min"],
                    latency_max_ms=stats["max"],
                    latency_std_ms=stats["std"],
                )
                records.append(record)
                print(
                    f"E2-offline {scale} {duplicate_label} {transform_name}: "
                    f"{pre_chars}->{post_chars} ({record.reduction_ratio:.1%}) "
                    f"dedup={record.deduplicated} ms={record.latency_mean_ms:.2f}"
                )
    if store is not None:
        for record in records:
            store.append(record)
    return records


def _e3_record(
    chain_length: int,
    policy_name: str,
    policy: Any,
    weights: PlacementWeights,
    *,
    experiment: str,
    variant: str,
) -> E3Record:
    workflow = parallel_chain_workflow(chain_length)
    plan = plan_workflow(workflow, policy=policy, weights=weights)
    summary = plan.summary
    return E3Record(
        **record_meta(
            experiment=experiment,
            variant=variant,
            workload=workflow.name,
            run_id=uuid.uuid4().hex,
            git_commit=git_commit(REPO_ROOT),
            git_dirty=git_dirty(REPO_ROOT),
            model=MODEL,
            provider=PROVIDER,
            placement_policy=policy_name,
            context_policy="not_executed",
            renderer="not_executed",
            context_transform="not_executed",
            success=True,
        ),
        chain_length=chain_length,
        policy=policy_name,
        dependency_agent_switches=summary.dependency_agent_switches,
        total_logical_handoff_refs=summary.total_logical_handoff_refs,
        total_logical_handoff_chars=summary.total_logical_handoff_chars,
        mean_assignment_locality=summary.mean_assignment_locality,
        assignment_distribution=dict(summary.assignment_distribution),
        total_tasks=summary.total_tasks,
    )


def run_e3(store: ExperimentStore | None) -> list[E3Record]:
    records: list[E3Record] = []
    for chain_length, topology in ((2, "P2"), (3, "P3"), (5, "P5")):
        for label, policy_name, policy in (
            ("S1", "capability_only", CapabilityOnlyPolicy()),
            ("S2", "context_locality", ContextLocalityPolicy()),
        ):
            record = _e3_record(
                chain_length,
                policy_name,
                policy,
                PlacementWeights(),
                experiment="E3",
                variant=f"{topology}-{label}",
            )
            records.append(record)
            print(
                f"E3 {topology} {label}: switches={record.dependency_agent_switches} "
                f"handoff={record.total_logical_handoff_refs} "
                f"locality={record.mean_assignment_locality:.3f} "
                f"distribution={record.assignment_distribution}"
            )

    # Optional load-weight diagnostic on one topology (P3).
    for load in (0.0, 0.2, 1.0):
        record = _e3_record(
            3,
            "context_locality",
            ContextLocalityPolicy(),
            PlacementWeights(locality=1.0, load=load),
            experiment="E3-load",
            variant=f"P3-S2-load{load:.1f}",
        )
        records.append(record)
        concentration = (
            max(record.assignment_distribution.values()) / record.total_tasks
            if record.assignment_distribution
            else 0.0
        )
        print(
            f"E3-load P3 load={load:.1f}: handoff={record.total_logical_handoff_refs} "
            f"switches={record.dependency_agent_switches} concentration={concentration:.3f}"
        )
    if store is not None:
        for record in records:
            store.append(record)
    return records


def main() -> None:
    dry_run = os.environ.get("ATLASOS_STAGE6_DRY_RUN") == "1"
    if git_dirty(REPO_ROOT) and not dry_run:
        raise SystemExit("Stage 6 formal records require a clean worktree; commit first")

    e1_store = None if dry_run else ExperimentStore(E1_STORE)
    e2_store = None if dry_run else ExperimentStore(E2_OFFLINE_STORE)
    e3_store = None if dry_run else ExperimentStore(E3_STORE)

    run_e1(e1_store)
    run_e2_offline(e2_store)
    run_e3(e3_store)


if __name__ == "__main__":
    main()
