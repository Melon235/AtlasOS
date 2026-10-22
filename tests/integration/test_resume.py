"""Stage 5 integration: resume rebuilds context before unfinished work executes."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
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

from atlasos.config import AtlasConfig
from atlasos.context import InMemoryContextStore
from atlasos.durability import InjectedCrash, ManifestMismatch
from atlasos.metrics import InferenceObservation, InferenceUsage
from atlasos.metrics.fingerprint import full_prompt_hash, stable_prefix_hash
from atlasos.runtime import AtlasRunResult, AtlasRuntime
from atlasos.workflow import baseline_workflow

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
    """Deterministic adapter with a durable-boundary crash mode for experiment tests."""

    def __init__(self, task_id: str, recorder: _Recorder, on_call: Any) -> None:
        self._task_id = task_id
        self._recorder = recorder
        self._on_call = on_call

    @property
    def name(self) -> str:
        return "recovery-fake"

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
            assert self._recorder.r1_sync
            self._recorder.b_started.set()
            # Crash only after A is durably completed and its mid-wave checkpoint was written.
            await self._recorder.a_completed.wait()
            self._observe(messages, options)
            raise InjectedCrash("interrupted while research-b was in flight")

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


def _runtime(
    runs_root: Path,
    recorder: _Recorder,
    *,
    event_hook: Any = None,
    config: AtlasConfig | None = None,
) -> AtlasRuntime:
    resolved = config or AtlasConfig.model_validate(
        {
            "context": {"policy": "task_scoped", "renderer": "stable_deterministic"},
            "placement": {"policy": "capability_only"},
        }
    )

    def factory(agent_name: str, on_call: Any) -> _RecoveryAdapter:
        return _RecoveryAdapter(TASK_BY_AGENT[agent_name], recorder, on_call)

    return AtlasRuntime(
        resolved,
        runs_root=runs_root,
        adapter_factory=factory,
        event_hook=event_hook,
        repo_root=runs_root,
    )


def _run_dir(tmp_path: Path) -> Path:
    candidates = [
        path
        for path in tmp_path.iterdir()
        if path.is_dir() and (path / "atlas_manifest.json").exists()
    ]
    assert len(candidates) == 1
    return candidates[0]


def _latest_checkpoint(run_dir: Path) -> CheckpointData:
    files = sorted((run_dir / "checkpoints").rglob("*.json"))
    assert files
    return CheckpointData.model_validate_json(files[-1].read_text(encoding="utf-8"))


def _inference_rows(run_dir: Path) -> list[dict[str, Any]]:
    path = run_dir / "inference.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _review_record(result: AtlasRunResult) -> Any:
    return next(record for record in result.inference_records if record.task_id == "review")


async def test_resume_rebuilds_completed_context_and_skips_reexecution(tmp_path: Path) -> None:
    workflow = baseline_workflow()
    recorder = _Recorder()

    def crash_before_review(event: Any) -> None:
        if event.type == "task_start" and event.agent == "reviewer":
            raise InjectedCrash("interrupted after A/B were durable, before Review ran")

    crashed_runtime = _runtime(tmp_path, recorder, event_hook=crash_before_review)
    with pytest.raises(InjectedCrash):
        await crashed_runtime.run(workflow, persist=False)

    run_dir = _run_dir(tmp_path)
    checkpoint = _latest_checkpoint(run_dir)
    states = {task.title: task.status for task in checkpoint.tasks}
    assert checkpoint.wave_index == 0
    assert states["Research A"] == "completed"
    assert states["Research B"] == "completed"
    assert states["Review"] != "completed"
    assert not (run_dir / "result.json").exists()

    resumed_runtime = _runtime(tmp_path, recorder)
    result = await resumed_runtime.resume(run_dir.name, workflow, persist=False)

    assert result.status == "succeeded"
    assert result.recovery is not None
    assert result.recovery.resumed is True
    assert result.recovery.completed_tasks_before_resume == 2
    assert result.recovery.completed_tasks_reexecuted == 0
    assert result.recovery.inflight_tasks_before_resume == 0
    assert result.recovery.unfinished_tasks_executed_after_resume == 1
    assert result.recovery.context_blocks_rebuilt == 2
    assert recorder.calls == {"research-a": 1, "research-b": 1, "review": 1}

    # Recovered blocks are in the derived store exactly once.
    blocks = resumed_runtime.context_blocks()
    assert [block.block_id for block in blocks] == [
        "task-output:research-a",
        "task-output:research-b",
    ]
    assert [block.content for block in blocks] == ["out-a", "out-b"]
    assert len([block for block in blocks if block.task_id == "research-b"]) == 1

    rows = _inference_rows(run_dir)
    by_generation = {(row["task_id"], row["resume_generation"]) for row in rows}
    assert ("research-a", 0) in by_generation
    assert ("research-b", 0) in by_generation
    assert ("review", 1) in by_generation
    assert ("research-a", 1) not in by_generation
    assert ("research-b", 1) not in by_generation


async def test_partial_wave_resume_reexecutes_only_inflight_task(tmp_path: Path) -> None:
    workflow = baseline_workflow()
    recorder = _Recorder()
    recorder.crash_on_b = True
    recorder.r1_sync = True

    def mark_a_completed(event: Any) -> None:
        if event.type == "task_complete" and event.agent == "researcher-a":
            recorder.a_completed.set()

    crashed_runtime = _runtime(tmp_path, recorder, event_hook=mark_a_completed)
    with pytest.raises(InjectedCrash):
        await crashed_runtime.run(workflow, persist=False)

    run_dir = _run_dir(tmp_path)
    checkpoint = _latest_checkpoint(run_dir)
    states = {task.title: task.status for task in checkpoint.tasks}
    assert checkpoint.wave_index == -1
    assert states["Research A"] == "completed"
    assert states["Research B"] == "in_progress"
    assert states["Review"] != "completed"

    recorder.crash_on_b = False
    resumed_runtime = _runtime(tmp_path, recorder)
    result = await resumed_runtime.resume(run_dir.name, workflow, persist=False)

    assert result.status == "succeeded"
    assert recorder.calls == {"research-a": 1, "research-b": 2, "review": 1}
    assert result.recovery is not None
    assert result.recovery.completed_tasks_before_resume == 1
    assert result.recovery.completed_tasks_reexecuted == 0
    assert result.recovery.inflight_tasks_before_resume == 1
    assert result.recovery.inflight_tasks_reexecuted == 1
    assert result.recovery.unfinished_tasks_executed_after_resume == 2
    assert result.recovery.context_blocks_rebuilt == 1
    blocks = resumed_runtime.context_blocks()
    assert len(blocks) == 2
    assert len([block for block in blocks if block.task_id == "research-b"]) == 1

    rows = _inference_rows(run_dir)
    by_generation = {(row["task_id"], row["resume_generation"]) for row in rows}
    assert ("research-a", 0) in by_generation
    assert ("research-a", 1) not in by_generation
    assert ("research-b", 0) in by_generation
    assert ("research-b", 1) in by_generation
    assert ("review", 1) in by_generation


async def test_resume_fails_if_context_was_not_rebuilt_before_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = baseline_workflow()
    recorder = _Recorder()

    def crash_before_review(event: Any) -> None:
        if event.type == "task_start" and event.agent == "reviewer":
            raise InjectedCrash("interrupted after A/B were durable, before Review ran")

    crashed_runtime = _runtime(tmp_path, recorder, event_hook=crash_before_review)
    with pytest.raises(InjectedCrash):
        await crashed_runtime.run(workflow, persist=False)
    run_dir = _run_dir(tmp_path)

    monkeypatch.setattr(
        "atlasos.runtime.rebuild_context_store",
        lambda checkpoint, placed: InMemoryContextStore(),
    )
    resumed_runtime = _runtime(tmp_path, recorder)
    with pytest.raises(RuntimeError, match="resume context invariant violated"):
        await resumed_runtime.resume(run_dir.name, workflow, persist=False)

    # The failure happened while building Review's prompt, before its provider call.
    assert recorder.calls["review"] == 0


async def test_uninterrupted_and_recovered_review_context_are_equivalent(tmp_path: Path) -> None:
    workflow = baseline_workflow()
    r0_root = tmp_path / "r0"
    r2_root = tmp_path / "r2"

    r0_recorder = _Recorder()
    r0_runtime = _runtime(r0_root, r0_recorder)
    r0 = await r0_runtime.run(workflow, experiment="R0", persist=False)

    r2_recorder = _Recorder()

    def crash_before_review(event: Any) -> None:
        if event.type == "task_start" and event.agent == "reviewer":
            raise InjectedCrash("interrupted after A/B were durable, before Review ran")

    crashed_runtime = _runtime(r2_root, r2_recorder, event_hook=crash_before_review)
    with pytest.raises(InjectedCrash):
        await crashed_runtime.run(workflow, experiment="R2", persist=False)
    run_dir = _run_dir(r2_root)
    resumed_runtime = _runtime(r2_root, r2_recorder)
    r2 = await resumed_runtime.resume(run_dir.name, workflow, experiment="R2", persist=False)

    r0_review = _review_record(r0)
    r2_review = _review_record(r2)
    assert r0_review.context_layout_hash == r2_review.context_layout_hash
    assert r0_review.full_prompt_hash == r2_review.full_prompt_hash
    assert r0_review.stable_prefix_hash == r2_review.stable_prefix_hash
    assert r0_review.rendered_dependency_order == r2_review.rendered_dependency_order
    assert r0.final_output == r2.final_output

    r0_blocks = [
        (block.block_id, block.version, block.content) for block in r0_runtime.context_blocks()
    ]
    r2_blocks = [
        (block.block_id, block.version, block.content) for block in resumed_runtime.context_blocks()
    ]
    assert r0_blocks == r2_blocks
    assert [task.task_id for task in r0.tasks] == [task.task_id for task in r2.tasks]


async def test_resume_rejects_changed_workflow(tmp_path: Path) -> None:
    workflow = baseline_workflow()
    runtime = _runtime(tmp_path, _Recorder())
    result = await runtime.run(workflow, persist=False)

    changed = replace(
        workflow,
        tasks=(
            replace(workflow.tasks[0], description="a changed root description"),
            workflow.tasks[1],
            workflow.tasks[2],
        ),
    )
    with pytest.raises(ManifestMismatch, match="workflow_hash"):
        await runtime.resume(result.run_id, changed, persist=False)


async def test_resume_rejects_changed_semantic_config(tmp_path: Path) -> None:
    workflow = baseline_workflow()
    runtime = _runtime(tmp_path, _Recorder())
    result = await runtime.run(workflow, persist=False)

    changed_config = AtlasConfig.model_validate(
        {
            "context": {"policy": "task_scoped", "renderer": "stable_deterministic"},
            "placement": {"policy": "context_locality"},
        }
    )
    changed_runtime = _runtime(tmp_path, _Recorder(), config=changed_config)
    with pytest.raises(ManifestMismatch, match="semantic_config_hash"):
        await changed_runtime.resume(result.run_id, workflow, persist=False)


async def test_resume_accepts_operational_config_changes(tmp_path: Path) -> None:
    workflow = baseline_workflow()
    runtime = _runtime(tmp_path, _Recorder())
    result = await runtime.run(workflow, persist=False)

    operational_config = AtlasConfig.model_validate(
        {
            "context": {"policy": "task_scoped", "renderer": "stable_deterministic"},
            "placement": {"policy": "capability_only"},
            "pricing": {"provider": "deepseek"},
        }
    )
    operational_runtime = _runtime(tmp_path, _Recorder(), config=operational_config)
    resumed = await operational_runtime.resume(result.run_id, workflow, persist=False)

    assert resumed.status == "succeeded"
    assert resumed.recovery is not None
    assert resumed.recovery.completed_tasks_before_resume == 3
    assert resumed.recovery.completed_tasks_reexecuted == 0


def test_latest_checkpoint_uses_created_at_not_filename(tmp_path: Path) -> None:
    # UUIDv7 names are only millisecond-ordered; two checkpoints in the same millisecond can
    # sort in the wrong lexical order. The newest checkpoint must win by created_at.
    checkpoint_dir = tmp_path / "checkpoints"
    workflow_dir = checkpoint_dir / "workflow"
    workflow_dir.mkdir(parents=True)
    older = CheckpointData(
        id="zzz-older",
        workflow_id="wf",
        version=2,
        tasks=[],
        agent_results={},
        wave_index=-1,
        total_token_usage=TokenUsage(),
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    newer = CheckpointData(
        id="aaa-newer",
        workflow_id="wf",
        version=2,
        tasks=[],
        agent_results={},
        wave_index=0,
        total_token_usage=TokenUsage(),
        created_at=datetime(2026, 1, 2, tzinfo=UTC),
    )
    (workflow_dir / "zzz-older.json").write_text(older.model_dump_json(), encoding="utf-8")
    (workflow_dir / "aaa-newer.json").write_text(newer.model_dump_json(), encoding="utf-8")

    loaded = AtlasRuntime._load_checkpoint(checkpoint_dir)
    assert loaded.id == "aaa-newer"
    assert loaded.wave_index == 0
