"""Stage 1 runtime tests: normalization, attribution, persistence, thin-wrapper behavior."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from anycode import AnyCode, TaskSpec, Team, TeamRunResult
from anycode.providers.fake import FakeAdapter, FakeResponse
from anycode.types import LLMChatOptions, LLMMessage, TextBlock

from atlasos.config import AtlasConfig
from atlasos.experiments import ExperimentStore
from atlasos.integrations.deepseek import DeepSeekAdapter
from atlasos.runtime import AtlasRuntime
from atlasos.workflow import (
    AtlasAgentSpec,
    AtlasTaskSpec,
    AtlasWorkflow,
    baseline_workflow,
)

BASELINE_PROVIDERS = {"researcher-a": "pa", "researcher-b": "pb", "reviewer": "pr"}


def _fake_factory(scripts: dict[str, list[str]]):
    def factory(agent_name: str, on_call: Any) -> FakeAdapter:
        del on_call
        return FakeAdapter(
            responses=[FakeResponse(text=text) for text in scripts.get(agent_name, ["ok"])]
        )

    return factory


class _RecordingFake(FakeAdapter):
    def __init__(self, texts: list[str], sink: list[list[LLMMessage]]) -> None:
        super().__init__(responses=[FakeResponse(text=text) for text in texts])
        self._sink = sink

    async def chat(self, messages: list[LLMMessage], options: LLMChatOptions):  # type: ignore[override]
        self._sink.append(list(messages))
        return await super().chat(messages, options)


class _CountingEngine(AnyCode):
    """Counts upstream invocations and hides agent_results to prove they are unused."""

    calls = 0

    async def run_tasks(
        self,
        team: Team,
        task_specs: list[TaskSpec],
        *,
        resume_from: str | None = None,
    ) -> TeamRunResult:
        type(self).calls += 1
        result = await super().run_tasks(team, task_specs, resume_from=resume_from)
        return result.model_copy(update={"agent_results": {}})


class _Completions:
    async def create(self, **_: Any) -> Any:
        usage = {
            "prompt_tokens": 100,
            "completion_tokens": 10,
            "prompt_tokens_details": None,
            "prompt_cache_hit_tokens": 40,
            "prompt_cache_miss_tokens": 60,
        }
        return SimpleNamespace(
            id="stub",
            model="deepseek-chat",
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="stub answer", tool_calls=None),
                    finish_reason="stop",
                )
            ],
            usage=SimpleNamespace(**usage, model_dump=lambda: dict(usage)),
        )


class _StubClient:
    def __init__(self) -> None:
        self.chat = SimpleNamespace(completions=_Completions())


def _deepseek_factory(agent_name: str, on_call: Any) -> DeepSeekAdapter:
    del agent_name
    return DeepSeekAdapter(client=_StubClient(), on_call=on_call)


async def test_same_agent_multiple_tasks_are_preserved(tmp_path: Path) -> None:
    workflow = AtlasWorkflow(
        name="same-agent",
        agents=(AtlasAgentSpec("worker", "w"), AtlasAgentSpec("peer", "p")),
        tasks=(
            AtlasTaskSpec("first", "First", "one", "worker"),
            AtlasTaskSpec("second", "Second", "two", "peer"),
            AtlasTaskSpec("final", "Final", "three", "worker", ("first", "second")),
        ),
    )
    collected: dict[str, list[list[LLMMessage]]] = {}

    def factory(agent_name: str, on_call: Any) -> FakeAdapter:
        del on_call
        scripts = {"worker": ["out-first", "out-final"], "peer": ["out-second"]}
        return _RecordingFake(scripts[agent_name], collected.setdefault(agent_name, []))

    runtime = AtlasRuntime(
        AtlasConfig(),
        runs_root=tmp_path,
        adapter_factory=factory,
        repo_root=tmp_path,
    )

    result = await runtime.run(workflow)

    by_id = {task.task_id: task for task in result.tasks}
    assert len(result.tasks) == 3
    assert by_id["first"].output == "out-first"
    assert by_id["second"].output == "out-second"
    assert by_id["final"].output == "out-final"
    assert by_id["final"].status == "completed"
    assert result.final_output == "out-final"
    assert all(task.status == "completed" for task in result.tasks)

    # The merge task prompt carries both prerequisite outputs: upstream_default context.
    final_prompt = "\n".join(
        block.text
        for message in collected["worker"][-1]
        for block in message.content
        if isinstance(block, TextBlock)
    )
    assert "out-first" in final_prompt
    assert "out-second" in final_prompt


async def test_normalization_ignores_agent_results(tmp_path: Path) -> None:
    _CountingEngine.calls = 0
    runtime = AtlasRuntime(
        AtlasConfig(),
        runs_root=tmp_path,
        adapter_factory=_fake_factory(
            {"researcher-a": ["a"], "researcher-b": ["b"], "reviewer": ["r"]}
        ),
        engine_factory=_CountingEngine,
        repo_root=tmp_path,
    )

    result = await runtime.run(baseline_workflow())

    assert _CountingEngine.calls == 1
    assert len(result.tasks) == 3
    assert {task.task_id for task in result.tasks} == {"research-a", "research-b", "review"}
    assert result.status == "succeeded"


async def test_runtime_attributes_usage_to_tasks_and_agents(tmp_path: Path) -> None:
    runtime = AtlasRuntime(
        AtlasConfig(),
        runs_root=tmp_path,
        adapter_factory=_deepseek_factory,
        repo_root=tmp_path,
    )

    result = await runtime.run(baseline_workflow())

    records = result.inference_records
    assert len(records) == 3
    assert {record.task_id for record in records} == {"research-a", "research-b", "review"}
    assert {record.agent_id for record in records} == {
        "researcher-a",
        "researcher-b",
        "reviewer",
    }
    assert all(record.run_id == result.run_id for record in records)
    assert all(record.session_id == result.run_id for record in records)
    assert all(record.scheduler_policy == "upstream_default" for record in records)
    assert all(record.context_policy == "upstream_default" for record in records)
    assert all(record.contextpilot_enabled is False for record in records)

    assert result.metrics.total_prompt_tokens == 300
    assert result.metrics.total_completion_tokens == 30
    assert result.metrics.total_cache_hit_tokens == 120
    assert result.metrics.total_cache_miss_tokens == 180
    assert result.metrics.weighted_cache_hit_ratio == pytest.approx(0.4)

    for record in records:
        assert record.stable_prefix_hash is not None
        assert record.full_prompt_hash is not None
        assert len(record.stable_prefix_hash) == 64


async def test_run_is_persisted_as_jsonl(tmp_path: Path) -> None:
    runtime = AtlasRuntime(
        AtlasConfig(),
        runs_root=tmp_path,
        adapter_factory=_fake_factory(
            {"researcher-a": ["a"], "researcher-b": ["b"], "reviewer": ["r"]}
        ),
        repo_root=tmp_path,
    )

    result = await runtime.run(baseline_workflow(), experiment="stage1-offline")

    rows = ExperimentStore(tmp_path / "runs.jsonl").read_all()
    assert len(rows) == 1
    row = rows[0]
    required = {
        "run_id",
        "timestamp",
        "git_commit",
        "model",
        "configuration_hash",
        "scheduler_policy",
        "context_policy",
        "contextpilot_enabled",
        "experiment",
        "status",
        "final_output",
        "tasks",
        "inference_records",
        "metrics",
    }
    assert required <= set(row)
    assert row["run_id"] == result.run_id
    assert row["experiment"] == "stage1-offline"
    assert row["scheduler_policy"] == "upstream_default"
    assert row["context_policy"] == "upstream_default"
    assert row["contextpilot_enabled"] is False
    assert row["git_commit"] is None  # tmp_path is not a repository

    # Second run appends rather than overwrites.
    await runtime.run(baseline_workflow(), experiment="stage1-offline")
    assert len(ExperimentStore(tmp_path / "runs.jsonl").read_all()) == 2


async def test_persist_false_writes_nothing(tmp_path: Path) -> None:
    runtime = AtlasRuntime(
        AtlasConfig(),
        runs_root=tmp_path,
        adapter_factory=_fake_factory(
            {"researcher-a": ["a"], "researcher-b": ["b"], "reviewer": ["r"]}
        ),
        repo_root=tmp_path,
    )

    await runtime.run(baseline_workflow(), persist=False)

    assert not (tmp_path / "runs.jsonl").exists()


def test_stage1_rejects_contextpilot_configuration() -> None:
    config = AtlasConfig.model_validate({"context": {"contextpilot_enabled": True}})

    with pytest.raises(ValueError, match="ContextPilot"):
        AtlasRuntime(config)


def test_stage1_rejects_custom_scheduler_configuration() -> None:
    config = AtlasConfig.model_validate({"scheduler": {"policy": "capability"}})

    with pytest.raises(ValueError, match="upstream_default"):
        AtlasRuntime(config)


def test_runtime_is_a_thin_wrapper_over_anycode() -> None:
    runtime = AtlasRuntime(AtlasConfig(), repo_root=Path.cwd())

    # Configuration is validated, but AtlasOS exposes no scheduler/executor of its own.
    assert not hasattr(runtime, "execute")
    assert not hasattr(runtime, "schedule")
