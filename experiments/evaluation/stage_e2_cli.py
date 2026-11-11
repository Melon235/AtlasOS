"""Run one Stage E2 Track A workflow through the real AtlasRuntime and DeepSeek."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from experiments.evaluation.runner import build_deepseek_adapter
from experiments.evaluation.stage_e2_tasks import (
    DEFINITIONS,
    build_placement_workflow,
    build_workflow,
    check_output,
)

from atlasos.config import (
    AtlasConfig,
    ContextConfig,
    ContextTransformConfig,
    PlacementConfig,
    Pricing,
    ProviderConfig,
)
from atlasos.integrations.deepseek import DeepSeekAdapter
from atlasos.runtime import AtlasRuntime

REPO_ROOT = Path(__file__).resolve().parents[2]


class PromptRecordingAdapter:
    def __init__(self, inner: DeepSeekAdapter, agent: str, prompts: list[dict[str, Any]]) -> None:
        self._inner = inner
        self._agent = agent
        self._prompts = prompts

    @property
    def name(self) -> str:
        return self._inner.name

    async def chat(self, messages: Any, options: Any) -> Any:
        self._prompts.append(
            {
                "agent": self._agent,
                "messages": [item.model_dump(mode="json") for item in messages],
                "options": options.model_dump(mode="json")
                if hasattr(options, "model_dump")
                else str(options),
            }
        )
        return await self._inner.chat(messages, options)

    def stream(self, messages: Any, options: Any) -> Any:
        async def _generate() -> Any:
            self._prompts.append(
                {
                    "agent": self._agent,
                    "messages": [item.model_dump(mode="json") for item in messages],
                    "options": options.model_dump(mode="json")
                    if hasattr(options, "model_dump")
                    else str(options),
                }
            )
            async for event in self._inner.stream(messages, options):
                yield event

        return _generate()


def _config(context: str, placement: str) -> AtlasConfig:
    if context == "C0":
        context_config = ContextConfig(
            policy="upstream_default",
            renderer="upstream_default",
            transform=ContextTransformConfig(name="identity"),
        )
    elif context == "C1":
        context_config = ContextConfig(
            policy="task_scoped",
            renderer="stable_deterministic",
            transform=ContextTransformConfig(name="identity"),
        )
    elif context == "C2":
        context_config = ContextConfig(
            policy="task_scoped",
            renderer="stable_deterministic",
            transform=ContextTransformConfig(name="contextpilot_dedup"),
        )
    else:
        raise ValueError(context)
    return AtlasConfig(
        provider=ProviderConfig(model="deepseek-chat"),
        context=context_config,
        placement=PlacementConfig(policy=placement),
        pricing=Pricing(
            cache_hit_input_per_million=0.006,
            cache_miss_input_per_million=0.300,
            output_per_million=1.200,
            snapshot_date=datetime(2026, 11, 9).date(),
        ),
    )


def _definition(task_id: str):
    for item in DEFINITIONS:
        if item.task_id == task_id:
            return item
    raise KeyError(task_id)


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    load_dotenv(REPO_ROOT / ".env", override=False)
    definition = _definition(args.task)
    prompts: list[dict[str, Any]] = []

    def adapter_factory(agent: str, on_call: Any) -> PromptRecordingAdapter:
        inner = build_deepseek_adapter(
            "deepseek-chat",
            on_call,
            timeout_seconds=args.call_timeout,
        )
        return PromptRecordingAdapter(inner, agent, prompts)

    run_root = Path(args.runs_root).resolve() / args.context / args.placement  # noqa: ASYNC240
    runtime = AtlasRuntime(
        _config(args.context, args.placement),
        runs_root=run_root,
        adapter_factory=adapter_factory,
        repo_root=REPO_ROOT,
    )
    workflow = (
        build_placement_workflow(definition)
        if args.workflow == "placement"
        else build_workflow(definition)
    )
    result = await runtime.run(
        workflow, experiment=f"stage-e2-{args.context}-{args.placement}", persist=False
    )
    correct = check_output(definition, result.final_output)
    record = {
        "task_id": definition.task_id,
        "profile": definition.profile,
        "workflow": args.workflow,
        "context": args.context,
        "placement": args.placement,
        "repetition": args.repetition,
        "sys.argv": sys.argv,
        "correct": correct,
        "expected": definition.expected,
        "final_output": result.final_output,
        "run_id": result.run_id,
        "git_commit": result.git_commit,
        "git_dirty": result.git_dirty,
        "configuration_hash": result.configuration_hash,
        "model": result.model,
        "status": result.status,
        "prompt_tokens": result.metrics.total_prompt_tokens,
        "completion_tokens": result.metrics.total_completion_tokens,
        "cache_hit_tokens": result.metrics.total_cache_hit_tokens,
        "cache_miss_tokens": result.metrics.total_cache_miss_tokens,
        "latency_ms": result.metrics.total_latency_ms,
        "estimated_cost": result.metrics.estimated_cost,
        "transform_latency_ms": result.total_transform_latency_ms,
        "inference_records": [item.model_dump(mode="json") for item in result.inference_records],
        "placement_decisions": result.placement_decisions,
        "mean_assignment_locality": result.mean_assignment_locality,
        "dependency_agent_switches": result.dependency_agent_switches,
        "total_logical_handoff_refs": result.total_logical_handoff_refs,
        "prompts": prompts,
        "started_at": result.started_at.isoformat(),
        "finished_at": result.finished_at.isoformat(),
    }
    output = Path(args.output)  # noqa: ASYNC240
    output.parent.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240
    output.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")  # noqa: ASYNC240
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="atlas-stage-e2")
    parser.add_argument("--task", required=True)
    parser.add_argument("--context", choices=("C0", "C1", "C2"), required=True)
    parser.add_argument(
        "--placement", choices=("bypass", "capability_only", "context_locality"), default="bypass"
    )
    parser.add_argument("--workflow", choices=("context", "placement"), default="context")
    parser.add_argument("--repetition", type=int, default=0)
    parser.add_argument("--runs-root", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--call-timeout", type=float, default=45.0)
    args = parser.parse_args(argv)
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    record = asyncio.run(_run(args))
    print(
        json.dumps(
            {
                "task_id": record["task_id"],
                "context": record["context"],
                "placement": record["placement"],
                "repetition": record["repetition"],
                "correct": record["correct"],
                "prompt_tokens": record["prompt_tokens"],
                "cache_hit_tokens": record["cache_hit_tokens"],
                "cache_miss_tokens": record["cache_miss_tokens"],
                "latency_ms": record["latency_ms"],
                "status": record["status"],
            },
            sort_keys=True,
        )
    )
    return 0 if record["correct"] else 1


if __name__ == "__main__":
    sys.exit(main())
