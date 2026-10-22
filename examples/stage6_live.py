"""Stage 6 live DeepSeek subset: E2 dedup scaling and E4 placement x dedup.

Formal runs require a clean worktree and ``ATLASOS_RUN_LIVE_TESTS=1``. Provider cache state is
warm-prone and uncontrolled; this script reports it honestly rather than claiming cold starts.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path

from anycode.types import LLMChatOptions, LLMMessage, TextBlock
from pydantic import BaseModel, ConfigDict
from stage6_workloads import (
    QUERY,
    SYSTEM,
    dedup_workload,
    parallel_chain_workflow,
    record_meta,
)

from atlasos.context import StablePromptRenderer
from atlasos.context.transform import ContextPilotDedupTransform, IdentityContextTransform
from atlasos.experiments import ExperimentStore, git_commit, git_dirty
from atlasos.integrations.deepseek import DeepSeekAdapter
from atlasos.metrics import InferenceObservation
from atlasos.placement import (
    CapabilityOnlyPolicy,
    ContextLocalityPolicy,
    PlacementWeights,
    plan_workflow,
)
from atlasos.workflow import AtlasTaskSpec

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNS = REPO_ROOT / "experiments" / "runs"
E2_LIVE_STORE = RUNS / "stage6-e2-live.jsonl"
E4_LIVE_STORE = RUNS / "stage6-e4-live.jsonl"
MODEL = "deepseek-chat"
REPETITIONS = 5
E4_VARIANTS = (
    ("A0", "capability_only", "identity"),
    ("A1", "context_locality", "identity"),
    ("A2", "capability_only", "contextpilot_dedup"),
    ("A3", "context_locality", "contextpilot_dedup"),
)


class LiveRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    experiment: str
    variant: str
    workload: str
    scale: str
    duplicate_ratio: float
    repetition: int
    run_id: str
    git_commit: str | None
    git_dirty: bool
    model: str
    provider: str
    placement_policy: str
    context_policy: str
    renderer: str
    context_transform: str
    prompt_tokens: int
    completion_tokens: int
    cache_hit_tokens: int | None
    cache_miss_tokens: int | None
    cache_hit_ratio: float | None
    provider_latency_ms: float
    transform_latency_ms: float
    original_context_chars: int
    optimized_context_chars: int
    blocks_before: int
    blocks_after: int
    deduplicated: bool
    rendered_chars: int
    dependency_agent_switches: int | None = None
    total_logical_handoff_refs: int | None = None
    mean_assignment_locality: float | None = None
    assignment_distribution: dict[str, int] | None = None
    success: bool
    error: str | None = None


def _render(blocks, query: str = QUERY):
    task = AtlasTaskSpec(
        task_id="stage6-live",
        title="Cache Stress",
        description=query,
        assignee="stress-agent",
    )
    return StablePromptRenderer(preserve_input_order=True).render(
        stable_system_prefix=SYSTEM,
        agent_role="cache-stress analyst",
        task=task,
        context_blocks=blocks,
    )


def _transform(name: str):
    return IdentityContextTransform() if name == "identity" else ContextPilotDedupTransform()


async def _call(
    adapter: DeepSeekAdapter,
    observations: list[InferenceObservation],
    rendered,
    *,
    repetitions: int = 2,
) -> InferenceObservation:
    last_error: Exception | None = None
    for _ in range(repetitions):
        before = len(observations)
        try:
            response = await adapter.chat(
                [
                    LLMMessage(
                        role="user",
                        content=[TextBlock(text=rendered.messages[1]["content"])],
                    )
                ],
                LLMChatOptions(
                    model=MODEL,
                    system_prompt=rendered.messages[0]["content"],
                    max_tokens=160,
                    temperature=0,
                ),
            )
        except Exception as exc:  # network/provider failures are retried once
            last_error = exc
            continue
        if not response.content:
            raise RuntimeError("provider returned an empty response")
        if len(observations) == before:
            raise RuntimeError("provider call produced no usage observation")
        return observations[-1]
    raise RuntimeError(f"provider call failed after retries: {last_error}")


def _record(
    *,
    experiment: str,
    variant: str,
    scale: str,
    duplicate_ratio: float,
    repetition: int,
    transform_name: str,
    transformed,
    rendered,
    observation: InferenceObservation,
    placement_policy: str,
    placement_metrics: dict[str, object] | None = None,
) -> LiveRecord:
    usage = observation.usage
    return LiveRecord(
        **record_meta(
            experiment=experiment,
            variant=variant,
            workload="dedup-stress",
            scale=scale,
            duplicate_ratio=duplicate_ratio,
            run_id=uuid.uuid4().hex,
            git_commit=git_commit(REPO_ROOT),
            git_dirty=git_dirty(REPO_ROOT),
            model=MODEL,
            provider="deepseek",
            placement_policy=placement_policy,
            context_policy="task_scoped",
            renderer="stable_deterministic",
            context_transform=transform_name,
            success=True,
        ),
        repetition=repetition,
        prompt_tokens=usage.prompt_tokens,
        completion_tokens=usage.completion_tokens,
        cache_hit_tokens=usage.cache_hit_tokens,
        cache_miss_tokens=usage.cache_miss_tokens,
        cache_hit_ratio=usage.cache_hit_ratio,
        provider_latency_ms=usage.latency_ms,
        transform_latency_ms=transformed.transform_latency_ms,
        original_context_chars=transformed.original_context_chars,
        optimized_context_chars=transformed.optimized_context_chars,
        blocks_before=transformed.original_block_count,
        blocks_after=transformed.optimized_block_count,
        deduplicated=transformed.deduplicated,
        rendered_chars=len(rendered.messages[1]["content"]),
        **(placement_metrics or {}),
    )


async def run_e2_live(
    adapter: DeepSeekAdapter,
    observations: list[InferenceObservation],
    store: ExperimentStore | None,
    *,
    quick: bool = False,
) -> None:
    scales = ("small",) if quick else ("small", "medium", "large")
    duplicate_labels = ("D0",) if quick else ("D0", "D2", "D3")
    repetitions = 1 if quick else REPETITIONS
    for scale in scales:
        for duplicate_label in duplicate_labels:
            workload = dedup_workload(scale, duplicate_label)
            for transform_label, transform_name in (
                ("T0", "identity"),
                ("T1", "contextpilot_dedup"),
            ):
                transform = _transform(transform_name)
                for repetition in range(1, repetitions + 1):
                    transformed = transform.transform(blocks=workload.blocks, query=QUERY)
                    rendered = _render(transformed.blocks)
                    observation = await _call(adapter, observations, rendered)
                    record = _record(
                        experiment="E2-live",
                        variant=f"{transform_label}-{scale}-{duplicate_label}",
                        scale=scale,
                        duplicate_ratio=workload.actual_duplicate_ratio,
                        repetition=repetition,
                        transform_name=transform_name,
                        transformed=transformed,
                        rendered=rendered,
                        observation=observation,
                        placement_policy="bypass",
                    )
                    if store is not None:
                        store.append(record)
                    print(
                        f"E2-live {scale} {duplicate_label} {transform_label} "
                        f"rep={repetition} prompt={record.prompt_tokens} "
                        f"hit={record.cache_hit_tokens} miss={record.cache_miss_tokens} "
                        f"transform_ms={record.transform_latency_ms:.2f} "
                        f"provider_ms={record.provider_latency_ms:.0f}"
                    )


def _placement_metrics(policy: str) -> dict[str, object]:
    workflow = parallel_chain_workflow(3)
    plan = plan_workflow(
        workflow,
        policy=CapabilityOnlyPolicy() if policy == "capability_only" else ContextLocalityPolicy(),
        weights=PlacementWeights(),
    )
    summary = plan.summary
    return {
        "dependency_agent_switches": summary.dependency_agent_switches,
        "total_logical_handoff_refs": summary.total_logical_handoff_refs,
        "mean_assignment_locality": summary.mean_assignment_locality,
        "assignment_distribution": dict(summary.assignment_distribution),
    }


async def run_e4_live(
    adapter: DeepSeekAdapter,
    observations: list[InferenceObservation],
    store: ExperimentStore | None,
    *,
    quick: bool = False,
) -> None:
    workload = dedup_workload("medium", "D2")
    variants = E4_VARIANTS[:1] if quick else E4_VARIANTS
    repetitions = 1 if quick else REPETITIONS
    for variant, policy, transform_name in variants:
        transform = _transform(transform_name)
        placement = _placement_metrics(policy)
        for repetition in range(1, repetitions + 1):
            transformed = transform.transform(blocks=workload.blocks, query=QUERY)
            rendered = _render(transformed.blocks)
            observation = await _call(adapter, observations, rendered)
            record = _record(
                experiment="E4-live",
                variant=f"{variant}-P3-{policy}",
                scale="medium",
                duplicate_ratio=workload.actual_duplicate_ratio,
                repetition=repetition,
                transform_name=transform_name,
                transformed=transformed,
                rendered=rendered,
                observation=observation,
                placement_policy=policy,
                placement_metrics=placement,
            )
            if store is not None:
                store.append(record)
            print(
                f"E4-live {variant} {policy} rep={repetition} "
                f"prompt={record.prompt_tokens} miss={record.cache_miss_tokens} "
                f"handoff={record.total_logical_handoff_refs} "
                f"switches={record.dependency_agent_switches} "
                f"locality={record.mean_assignment_locality}"
            )


async def main() -> None:
    if os.environ.get("ATLASOS_RUN_LIVE_TESTS") != "1":
        raise SystemExit("set ATLASOS_RUN_LIVE_TESTS=1 to run live Stage 6 experiments")
    dry_run = os.environ.get("ATLASOS_STAGE6_DRY_RUN") == "1"
    if git_dirty(REPO_ROOT) and not dry_run:
        raise SystemExit("Stage 6 formal live records require a clean worktree; commit first")

    observations: list[InferenceObservation] = []
    adapter = DeepSeekAdapter.from_env(on_call=observations.append)
    quick = os.environ.get("ATLASOS_STAGE6_LIVE_QUICK") == "1"
    e2_store = None if dry_run else ExperimentStore(E2_LIVE_STORE)
    e4_store = None if dry_run else ExperimentStore(E4_LIVE_STORE)
    await run_e2_live(adapter, observations, e2_store, quick=quick)
    await run_e4_live(adapter, observations, e4_store, quick=quick)


if __name__ == "__main__":
    asyncio.run(main())
