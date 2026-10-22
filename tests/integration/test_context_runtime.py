"""Stage 2 integration: completion order must not leak into stable prompt layout."""

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

FAST = 0.01
SLOW = 0.08


class _TelemetryFake(FakeAdapter):
    """Deterministic adapter that also emits a normalized observation and sleeps."""

    def __init__(self, text: str, delay: float, on_call: Any) -> None:
        super().__init__(responses=[FakeResponse(text=text)])
        self._delay = delay
        self._on_call = on_call

    async def chat(self, messages: list[LLMMessage], options: LLMChatOptions):  # type: ignore[override]
        await asyncio.sleep(self._delay)
        response = await super().chat(messages, options)
        self._on_call(
            InferenceObservation(
                usage=InferenceUsage(
                    prompt_tokens=100,
                    completion_tokens=10,
                    latency_ms=self._delay * 1000.0,
                ),
                stable_prefix_hash=stable_prefix_hash(options.system_prompt),
                full_prompt_hash=full_prompt_hash(options.system_prompt, messages),
            )
        )
        return response


def _factory(delays: dict[str, float]):
    def factory(agent_name: str, on_call: Any) -> FakeAdapter:
        return _TelemetryFake(REPLIES[agent_name], delays.get(agent_name, 0.0), on_call)

    return factory


def _config(policy: str, renderer: str) -> AtlasConfig:
    return AtlasConfig.model_validate({"context": {"policy": policy, "renderer": renderer}})


async def _run(
    tmp_path: Path,
    *,
    policy: str,
    renderer: str,
    a_delay: float,
    name: str,
) -> AtlasRunResult:
    runtime = AtlasRuntime(
        _config(policy, renderer),
        runs_root=tmp_path,
        adapter_factory=_factory({"researcher-a": a_delay, "researcher-b": SLOW}),
        repo_root=tmp_path,
    )
    return await runtime.run(
        baseline_workflow(),
        experiment=name,
        input_text="compare two approaches",
        persist=False,
    )


def _review(result: AtlasRunResult):
    return next(record for record in result.inference_records if record.task_id == "review")


async def test_naive_renderer_tracks_completion_order(tmp_path: Path) -> None:
    first = await _run(
        tmp_path,
        policy="task_scoped",
        renderer="naive_execution_order",
        a_delay=FAST,
        name="c1-a-first",
    )
    second = await _run(
        tmp_path,
        policy="task_scoped",
        renderer="naive_execution_order",
        a_delay=SLOW + 0.05,
        name="c1-b-first",
    )

    assert first.status == second.status == "succeeded"
    assert _review(first).dependency_completion_order == ["research-a", "research-b"]
    assert _review(second).dependency_completion_order == ["research-b", "research-a"]
    assert _review(first).rendered_dependency_order == ["research-a", "research-b"]
    assert _review(second).rendered_dependency_order == ["research-b", "research-a"]
    assert _review(first).context_layout_hash != _review(second).context_layout_hash


async def test_stable_renderer_ignores_completion_order(tmp_path: Path) -> None:
    first = await _run(
        tmp_path,
        policy="task_scoped",
        renderer="stable_deterministic",
        a_delay=FAST,
        name="c2-a-first",
    )
    second = await _run(
        tmp_path,
        policy="task_scoped",
        renderer="stable_deterministic",
        a_delay=SLOW + 0.05,
        name="c2-b-first",
    )

    assert first.status == second.status == "succeeded"
    # Completion order really was inverted...
    assert _review(first).dependency_completion_order == ["research-a", "research-b"]
    assert _review(second).dependency_completion_order == ["research-b", "research-a"]
    # ...but rendering was not.
    assert _review(first).rendered_dependency_order == ["research-a", "research-b"]
    assert _review(second).rendered_dependency_order == ["research-a", "research-b"]
    assert _review(first).context_layout_hash == _review(second).context_layout_hash
    assert _review(first).full_prompt_hash == _review(second).full_prompt_hash
    assert _review(first).stable_prefix_hash == _review(second).stable_prefix_hash


async def test_task_semantics_match_across_variants(tmp_path: Path) -> None:
    upstream = await _run(
        tmp_path,
        policy="upstream_default",
        renderer="upstream_default",
        a_delay=FAST,
        name="u0",
    )
    naive = await _run(
        tmp_path,
        policy="task_scoped",
        renderer="naive_execution_order",
        a_delay=FAST,
        name="c1",
    )
    stable = await _run(
        tmp_path,
        policy="task_scoped",
        renderer="stable_deterministic",
        a_delay=FAST,
        name="c2",
    )

    for result in (upstream, naive, stable):
        assert result.status == "succeeded"
        assert [task.output for task in result.tasks] == ["out-a", "out-b", "review-out"]
        assert result.final_output == "review-out"


async def test_upstream_mode_keeps_stage1_labels_and_no_layout_metadata(tmp_path: Path) -> None:
    result = await _run(
        tmp_path,
        policy="upstream_default",
        renderer="upstream_default",
        a_delay=FAST,
        name="u0-labels",
    )

    assert result.context_policy == "upstream_default"
    assert result.renderer_policy == "upstream_default"
    record = _review(result)
    assert record.renderer_policy == "upstream_default"
    assert record.context_layout_hash is None
    assert record.rendered_dependency_order is None
    assert record.dependency_completion_order is None


async def test_atlas_modes_record_renderer_labels(tmp_path: Path) -> None:
    result = await _run(
        tmp_path,
        policy="task_scoped",
        renderer="stable_deterministic",
        a_delay=FAST,
        name="c2-labels",
    )

    assert result.context_policy == "task_scoped"
    assert result.renderer_policy == "stable_deterministic"
    record = _review(result)
    assert record.renderer_policy == "stable_deterministic"
    assert record.context_layout_hash is not None
    assert record.stable_prefix_tokens is None  # not measured, never fabricated
