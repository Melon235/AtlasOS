"""Stage 3 closure: transform path, stable prefix, and batch policy separation."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from anycode.providers.fake import FakeAdapter, FakeResponse
from anycode.types import LLMChatOptions, LLMMessage

from atlasos.config import AtlasConfig
from atlasos.metrics import InferenceObservation, InferenceUsage
from atlasos.metrics.fingerprint import full_prompt_hash, stable_prefix_hash
from atlasos.runtime import AtlasRunResult, AtlasRuntime
from atlasos.workflow import baseline_workflow

#: The optional ContextPilot backend is not installed in CI; the sibling smoke test skips the same
#: way. When the extra is present the module runs unchanged.
pytest.importorskip("contextpilot")

REPLIES = {
    "researcher-a": "out-a",
    "researcher-b": "out-b",
    "reviewer": "review-out",
}


class _TelemetryFake(FakeAdapter):
    def __init__(self, text: str, on_call: Any) -> None:
        super().__init__(responses=[FakeResponse(text=text)])
        self._on_call = on_call

    async def chat(self, messages: list[LLMMessage], options: LLMChatOptions):  # type: ignore[override]
        response = await super().chat(messages, options)
        self._on_call(
            InferenceObservation(
                usage=InferenceUsage(prompt_tokens=100, completion_tokens=10, latency_ms=1.0),
                stable_prefix_hash=stable_prefix_hash(options.system_prompt),
                full_prompt_hash=full_prompt_hash(options.system_prompt, messages),
            )
        )
        return response


def _factory(agent_name: str, on_call: Any) -> FakeAdapter:
    return _TelemetryFake(REPLIES[agent_name], on_call)


def _config(transform: str) -> AtlasConfig:
    return AtlasConfig.model_validate(
        {
            "context": {
                "policy": "task_scoped",
                "renderer": "stable_deterministic",
                "transform": {"name": transform},
            }
        }
    )


async def _run(
    tmp_path: Path, transform: str, delays: dict[str, float] | None = None
) -> AtlasRunResult:
    delays = delays or {}

    def factory(agent_name: str, on_call: Any) -> FakeAdapter:
        adapter = _TelemetryFake(REPLIES[agent_name], on_call)
        delay = delays.get(agent_name)
        if delay is not None:
            original = adapter.chat

            async def delayed(messages: list[LLMMessage], options: LLMChatOptions):
                await asyncio.sleep(delay)
                return await original(messages, options)

            adapter.chat = delayed  # type: ignore[method-assign]
        return adapter

    runtime = AtlasRuntime(
        _config(transform),
        runs_root=tmp_path,
        adapter_factory=factory,
        repo_root=tmp_path,
    )
    return await runtime.run(baseline_workflow(), experiment=f"stage3-{transform}", persist=False)


def _review(result: AtlasRunResult):
    return next(record for record in result.inference_records if record.task_id == "review")


async def test_t0_and_t1_keep_task_semantics_and_prefix(tmp_path: Path) -> None:
    baseline = await _run(tmp_path, "identity")
    deduped = await _run(tmp_path, "contextpilot_dedup")

    for result in (baseline, deduped):
        assert result.status == "succeeded"
        assert [task.output for task in result.tasks] == ["out-a", "out-b", "review-out"]

    assert _review(baseline).stable_prefix_hash == _review(deduped).stable_prefix_hash
    assert _review(baseline).transform_name == "identity"
    assert _review(deduped).transform_name == "contextpilot_dedup"
    baseline_review = _review(baseline)
    assert baseline_review.pre_transform_layout_hash == baseline_review.post_transform_layout_hash


async def test_pre_transform_layout_is_stable_under_inverted_completion(tmp_path: Path) -> None:
    first = await _run(tmp_path, "identity", {"researcher-b": 0.08})
    second = await _run(tmp_path, "identity", {"researcher-a": 0.08})

    assert _review(first).pre_transform_layout_hash == _review(second).pre_transform_layout_hash
    assert _review(first).dependency_completion_order != _review(second).dependency_completion_order


async def test_batch_reorder_is_not_on_the_transform_path(tmp_path: Path) -> None:
    result = await _run(tmp_path, "identity")

    # A transform only reports content/count metrics; there is no request-order field at all.
    record = _review(result)
    assert record.transform_name == "identity"
    assert record.deduplicated is False
    assert not hasattr(record, "batch_order")
