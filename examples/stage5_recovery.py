"""Stage 5 formal recovery experiments R0/R1/R2 (deterministic, clean-worktree).

R0 is an uninterrupted reference run. R1 interrupts while a sibling task is in flight. R2
interrupts after both root tasks are durable and before the dependent Review executes. Every
interruption is raised at a verified durable boundary, never after an arbitrary delay.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from collections.abc import AsyncIterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from anycode import CheckpointData
from anycode.types import (
    LLMChatOptions,
    LLMMessage,
    LLMResponse,
    LLMStreamOptions,
    StreamEvent,
    TextBlock,
    TokenUsage,
)
from pydantic import BaseModel, ConfigDict

from atlasos.config import AtlasConfig
from atlasos.durability import InjectedCrash
from atlasos.experiments import ExperimentStore, git_commit, git_dirty, package_version
from atlasos.metrics import InferenceObservation, InferenceUsage
from atlasos.metrics.fingerprint import full_prompt_hash, stable_prefix_hash
from atlasos.runtime import AtlasRunResult, AtlasRuntime
from atlasos.workflow import AtlasWorkflow, baseline_workflow

REPO_ROOT = Path(__file__).resolve().parents[1]
RUN_ROOT = REPO_ROOT / "experiments" / "runs" / "stage5"
RESULT_STORE = REPO_ROOT / "experiments" / "runs" / "stage5-formal.jsonl"

OUTPUTS = {
    "research-a": "out-a",
    "research-b": "out-b",
    "review": "review-out",
}
TASK_BY_AGENT = {
    "researcher-a": "research-a",
    "researcher-b": "research-b",
    "reviewer": "review",
}


class _Recorder:
    def __init__(self) -> None:
        self.calls = {task_id: 0 for task_id in OUTPUTS}
        self.crash_on_b = False
        self.r1_sync = False
        self.a_completed = asyncio.Event()
        self.b_started = asyncio.Event()


class _RecoveryAdapter:
    def __init__(self, task_id: str, recorder: _Recorder, on_call: Any) -> None:
        self._task_id = task_id
        self._recorder = recorder
        self._on_call = on_call

    @property
    def name(self) -> str:
        return "stage5-fake"

    def _observe(self, messages: list[LLMMessage], options: LLMChatOptions) -> None:
        self._on_call(
            InferenceObservation(
                usage=InferenceUsage(prompt_tokens=100, completion_tokens=10, latency_ms=0.0),
                stable_prefix_hash=stable_prefix_hash(options.system_prompt),
                full_prompt_hash=full_prompt_hash(options.system_prompt, messages),
            )
        )

    async def chat(self, messages: list[LLMMessage], options: LLMChatOptions) -> LLMResponse:
        self._recorder.calls[self._task_id] += 1
        first_call = self._recorder.calls[self._task_id] == 1

        if self._task_id == "research-b" and self._recorder.crash_on_b and first_call:
            self._recorder.b_started.set()
            # B is in flight; crash only once A's durable completion checkpoint exists.
            await self._recorder.a_completed.wait()
            self._observe(messages, options)
            raise InjectedCrash("R1: interrupted while research-b was in flight")

        if self._task_id == "research-a" and self._recorder.r1_sync and first_call:
            await self._recorder.b_started.wait()

        self._observe(messages, options)
        return LLMResponse(
            id=f"fake-{self._task_id}",
            content=[TextBlock(text=OUTPUTS[self._task_id])],
            model=options.model or "fake-model",
            stop_reason="end_turn",
            usage=TokenUsage(input_tokens=100, output_tokens=10),
        )

    def stream(
        self,
        messages: list[LLMMessage],
        options: LLMStreamOptions,
    ) -> AsyncIterable[StreamEvent]:
        async def _generate() -> AsyncIterable[StreamEvent]:
            yield StreamEvent(type="done", data=await self.chat(messages, options))

        return _generate()


class RecoveryRunRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    variant: str
    run_id: str
    timestamp: datetime

    git_commit: str | None
    git_dirty: bool
    anycode_version: str | None

    workflow_name: str
    placement_policy: str
    recovery_success: bool

    checkpoint_state_before_crash: dict[str, Any] | None
    execution_count_by_task: dict[str, int]
    inference_records_by_generation: dict[str, int]

    completed_tasks_before_resume: int
    completed_tasks_reexecuted: int
    inflight_tasks_before_resume: int
    inflight_tasks_reexecuted: int
    unfinished_tasks_executed_after_resume: int
    context_blocks_rebuilt: int

    context_ref_equivalent: bool | None
    context_layout_equivalent: bool | None
    prompt_equivalent: bool | None
    final_output_equivalent: bool | None

    recovery_latency_ms: float | None
    final_output: str
    task_outputs: dict[str, str | None]
    context_refs: list[dict[str, Any]]
    review_stable_prefix_hash: str | None
    review_context_layout_hash: str | None
    review_full_prompt_hash: str | None


def _runtime(
    runs_root: Path,
    recorder: _Recorder,
    *,
    event_hook: Any = None,
) -> AtlasRuntime:
    config = AtlasConfig.model_validate(
        {
            "context": {"policy": "task_scoped", "renderer": "stable_deterministic"},
            "placement": {"policy": "capability_only"},
        }
    )

    def factory(agent_name: str, on_call: Any) -> _RecoveryAdapter:
        return _RecoveryAdapter(TASK_BY_AGENT[agent_name], recorder, on_call)

    return AtlasRuntime(
        config,
        runs_root=runs_root,
        adapter_factory=factory,
        event_hook=event_hook,
        repo_root=REPO_ROOT,
    )


def _review(result: AtlasRunResult) -> Any:
    return next(record for record in result.inference_records if record.task_id == "review")


def _latest_checkpoint(run_dir: Path) -> CheckpointData:
    files = sorted((run_dir / "checkpoints").rglob("*.json"))
    if not files:
        raise RuntimeError(f"no checkpoint under {run_dir}")
    return CheckpointData.model_validate_json(files[-1].read_text(encoding="utf-8"))


def _checkpoint_state(checkpoint: CheckpointData) -> dict[str, Any]:
    return {
        "checkpoint_id": checkpoint.id,
        "wave_index": checkpoint.wave_index,
        "task_statuses": {task.title: task.status for task in checkpoint.tasks},
    }


def _run_dir(runs_root: Path) -> Path:
    candidates = [
        path
        for path in runs_root.iterdir()
        if path.is_dir() and (path / "atlas_manifest.json").exists()
    ]
    if len(candidates) != 1:
        raise RuntimeError(f"expected one session under {runs_root}, found {len(candidates)}")
    return candidates[0]


def _generation_counts(run_dir: Path) -> dict[str, int]:
    path = run_dir / "inference.jsonl"
    counts: dict[str, int] = {}
    if not path.exists():
        return counts
    for line in path.read_text(encoding="utf-8").splitlines():
        if line:
            generation = json.loads(line)["resume_generation"]
            counts[str(generation)] = counts.get(str(generation), 0) + 1
    return counts


def _context_refs(runtime: AtlasRuntime) -> list[dict[str, Any]]:
    return [
        {
            "block_id": block.block_id,
            "version": block.version,
            "source_task_id": block.task_id,
            "content_sha256": hashlib.sha256(block.content.encode("utf-8")).hexdigest(),
        }
        for block in runtime.context_blocks()
    ]


def _record(
    *,
    variant: str,
    run_id: str,
    result: AtlasRunResult,
    run_dir: Path,
    runtime: AtlasRuntime,
    execution_counts: dict[str, int],
    checkpoint_state: dict[str, Any] | None,
) -> RecoveryRunRecord:
    recovery = result.recovery
    review = _review(result)
    return RecoveryRunRecord(
        variant=variant,
        run_id=run_id,
        timestamp=datetime.now(UTC),
        git_commit=git_commit(REPO_ROOT),
        git_dirty=git_dirty(REPO_ROOT),
        anycode_version=package_version("anycode-py"),
        workflow_name=result.workflow,
        placement_policy=result.placement_policy,
        recovery_success=result.status == "succeeded"
        and all(task.status == "completed" for task in result.tasks),
        checkpoint_state_before_crash=checkpoint_state,
        execution_count_by_task=dict(execution_counts),
        inference_records_by_generation=_generation_counts(run_dir),
        completed_tasks_before_resume=(recovery.completed_tasks_before_resume if recovery else 0),
        completed_tasks_reexecuted=recovery.completed_tasks_reexecuted if recovery else 0,
        inflight_tasks_before_resume=recovery.inflight_tasks_before_resume if recovery else 0,
        inflight_tasks_reexecuted=recovery.inflight_tasks_reexecuted if recovery else 0,
        unfinished_tasks_executed_after_resume=(
            recovery.unfinished_tasks_executed_after_resume if recovery else 0
        ),
        context_blocks_rebuilt=recovery.context_blocks_rebuilt if recovery else 0,
        context_ref_equivalent=None,
        context_layout_equivalent=None,
        prompt_equivalent=None,
        final_output_equivalent=None,
        recovery_latency_ms=recovery.recovery_latency_ms if recovery else None,
        final_output=result.final_output,
        task_outputs={task.task_id: task.output for task in result.tasks},
        context_refs=_context_refs(runtime),
        review_stable_prefix_hash=review.stable_prefix_hash,
        review_context_layout_hash=review.context_layout_hash,
        review_full_prompt_hash=review.full_prompt_hash,
    )


def _compare(reference: RecoveryRunRecord, recovered: RecoveryRunRecord) -> RecoveryRunRecord:
    return recovered.model_copy(
        update={
            "context_ref_equivalent": reference.context_refs == recovered.context_refs,
            "context_layout_equivalent": (
                reference.review_context_layout_hash == recovered.review_context_layout_hash
            ),
            "prompt_equivalent": (
                reference.review_full_prompt_hash == recovered.review_full_prompt_hash
            ),
            "final_output_equivalent": reference.final_output == recovered.final_output,
        }
    )


async def _r0(workflow: AtlasWorkflow) -> RecoveryRunRecord:
    recorder = _Recorder()
    runs_root = RUN_ROOT / "R0" / uuid.uuid4().hex
    runtime = _runtime(runs_root, recorder)
    result = await runtime.run(workflow, experiment="R0")
    run_dir = runs_root / result.run_id
    return _record(
        variant="R0",
        run_id=result.run_id,
        result=result,
        run_dir=run_dir,
        runtime=runtime,
        execution_counts=recorder.calls,
        checkpoint_state=None,
    )


async def _recover(
    workflow: AtlasWorkflow,
    variant: str,
    *,
    recorder: _Recorder,
    event_hook: Any,
) -> tuple[RecoveryRunRecord, dict[str, Any]]:
    runs_root = RUN_ROOT / variant / uuid.uuid4().hex
    runtime = _runtime(runs_root, recorder, event_hook=event_hook)
    try:
        await runtime.run(workflow, experiment=variant)
    except InjectedCrash:
        pass
    else:  # pragma: no cover - a missing interruption invalidates the experiment
        raise RuntimeError(f"{variant}: expected an injected durable-boundary interruption")

    run_dir = _run_dir(runs_root)
    checkpoint_state = _checkpoint_state(_latest_checkpoint(run_dir))
    recorder.crash_on_b = False

    resumed_runtime = _runtime(runs_root, recorder)
    result = await resumed_runtime.resume(run_dir.name, workflow, experiment=variant)
    record = _record(
        variant=variant,
        run_id=run_dir.name,
        result=result,
        run_dir=run_dir,
        runtime=resumed_runtime,
        execution_counts=recorder.calls,
        checkpoint_state=checkpoint_state,
    )
    return record, checkpoint_state


def _print_table(records: list[RecoveryRunRecord]) -> None:
    print(
        "| Variant | success | completed before resume | completed reexecuted | "
        "in-flight reexecuted | rebuilt blocks | layout equivalent | final equivalent | "
        "recovery ms |"
    )
    print("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for record in records:
        layout = (
            "-"
            if record.context_layout_equivalent is None
            else str(record.context_layout_equivalent).lower()
        )
        final = (
            "-"
            if record.final_output_equivalent is None
            else str(record.final_output_equivalent).lower()
        )
        latency = "-" if record.recovery_latency_ms is None else f"{record.recovery_latency_ms:.1f}"
        print(
            f"| {record.variant} | {str(record.recovery_success).lower()} | "
            f"{record.completed_tasks_before_resume} | {record.completed_tasks_reexecuted} | "
            f"{record.inflight_tasks_reexecuted} | {record.context_blocks_rebuilt} | "
            f"{layout} | {final} | {latency} |"
        )
    print()
    for record in records:
        print(
            f"{record.variant} execution_count_by_task="
            f"{record.execution_count_by_task} generations="
            f"{record.inference_records_by_generation}"
        )
        if record.context_layout_equivalent is not None:
            print(
                f"{record.variant} refs_equivalent={record.context_ref_equivalent} "
                f"layout_equivalent={record.context_layout_equivalent} "
                f"prompt_equivalent={record.prompt_equivalent} "
                f"final_equivalent={record.final_output_equivalent}"
            )
        if record.review_context_layout_hash is not None:
            print(
                f"{record.variant} review layout_hash={record.review_context_layout_hash} "
                f"prompt_hash={record.review_full_prompt_hash}"
            )
        if record.checkpoint_state_before_crash is not None:
            print(f"{record.variant} checkpoint={record.checkpoint_state_before_crash}")


async def main() -> None:
    dry_run = os.environ.get("ATLASOS_STAGE5_DRY_RUN") == "1"
    if git_dirty(REPO_ROOT) and not dry_run:
        raise SystemExit(
            "Stage 5 formal records require a clean worktree; commit implementation first"
        )

    workflow = baseline_workflow()
    reference = await _r0(workflow)

    r1_recorder = _Recorder()
    r1_recorder.crash_on_b = True
    r1_recorder.r1_sync = True

    def mark_a_completed(event: Any) -> None:
        if event.type == "task_complete" and event.agent == "researcher-a":
            r1_recorder.a_completed.set()

    r1, _ = await _recover(
        workflow,
        "R1",
        recorder=r1_recorder,
        event_hook=mark_a_completed,
    )
    r1 = _compare(reference, r1)

    r2_recorder = _Recorder()

    def crash_before_review(event: Any) -> None:
        if event.type == "task_start" and event.agent == "reviewer":
            raise InjectedCrash("R2: interrupted before Review executed")

    r2, _ = await _recover(
        workflow,
        "R2",
        recorder=r2_recorder,
        event_hook=crash_before_review,
    )
    r2 = _compare(reference, r2)

    records = [reference, r1, r2]
    if not dry_run:
        store = ExperimentStore(RESULT_STORE)
        for record in records:
            store.append(record)
    _print_table(records)


if __name__ == "__main__":
    asyncio.run(main())
