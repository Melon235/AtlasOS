"""Stage 3 functional integration: P0-P3 optimizers keep the Stage 2 contract intact."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from anycode.providers.fake import FakeAdapter, FakeResponse
from anycode.types import LLMChatOptions, LLMMessage

from atlasos.config import AtlasConfig
from atlasos.metrics import InferenceObservation, InferenceUsage
from atlasos.metrics.fingerprint import full_prompt_hash, stable_prefix_hash
from atlasos.runtime import AtlasRunResult, AtlasRuntime
from atlasos.workflow import baseline_workflow

REPLIES = {
    "researcher-a": "out-a",
    "researcher-b": "out-b",
    "reviewer": "review-out",
}

VARIANTS = (
    ("p0", "identity", False, False),
    ("p1", "contextpilot", True, False),
    ("p2", "contextpilot", False, True),
    ("p3", "contextpilot", True, True),
)


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


def _config(name: str, reorder: bool, dedup: bool) -> AtlasConfig:
    return AtlasConfig.model_validate(
        {
            "context": {
                "policy": "task_scoped",
                "renderer": "stable_deterministic",
                "optimizer": {
                    "name": name,
                    "reorder": reorder,
                    "deduplicate": dedup,
                },
            }
        }
    )


async def _run(tmp_path: Path, variant: tuple[str, str, bool, bool]) -> AtlasRunResult:
    label, name, reorder, dedup = variant
    runtime = AtlasRuntime(
        _config(name, reorder, dedup),
        runs_root=tmp_path,
        adapter_factory=_factory,
        repo_root=tmp_path,
    )
    return await runtime.run(
        baseline_workflow(),
        experiment=f"stage3-{label}",
        input_text="compare two approaches",
        persist=False,
    )


async def test_p0_to_p3_run_and_keep_the_stable_prefix(tmp_path: Path) -> None:
    results = {}
    for variant in VARIANTS:
        result = await _run(tmp_path, variant)
        assert result.status == "succeeded"
        assert [task.output for task in result.tasks] == ["out-a", "out-b", "review-out"]
        results[variant[0]] = result

    review_prefixes = {
        label: next(
            record.stable_prefix_hash
            for record in result.inference_records
            if record.task_id == "review"
        )
        for label, result in results.items()
    }
    assert len(set(review_prefixes.values())) == 1

    for label, result in results.items():
        review = next(record for record in result.inference_records if record.task_id == "review")
        assert review.optimizer_name == ("identity" if label == "p0" else "contextpilot")
        assert review.reorder_enabled is (label in {"p1", "p3"})
        assert review.deduplicate_enabled is (label in {"p2", "p3"})
        assert review.pre_optimizer_layout_hash is not None
        assert review.post_optimizer_layout_hash is not None
        if label == "p0":
            assert review.pre_optimizer_layout_hash == review.post_optimizer_layout_hash
        assert result.total_optimizer_latency_ms >= 0


async def test_stable_pre_optimizer_layout_ignores_completion_order(tmp_path: Path) -> None:
    async def run_with_delays(delays: dict[str, float]) -> AtlasRunResult:
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
            _config("identity", False, False),
            runs_root=tmp_path,
            adapter_factory=factory,
            repo_root=tmp_path,
        )
        return await runtime.run(baseline_workflow(), experiment="p0-order", persist=False)

    first = await run_with_delays({"researcher-b": 0.08})
    second = await run_with_delays({"researcher-a": 0.08})

    def pre(result: AtlasRunResult) -> str:
        value = next(
            record.pre_optimizer_layout_hash
            for record in result.inference_records
            if record.task_id == "review"
        )
        assert value is not None
        return value

    assert pre(first) == pre(second)
    assert next(
        record.dependency_completion_order
        for record in first.inference_records
        if record.task_id == "review"
    ) != next(
        record.dependency_completion_order
        for record in second.inference_records
        if record.task_id == "review"
    )
