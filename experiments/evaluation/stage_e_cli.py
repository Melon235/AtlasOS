"""Run one Stage E1 paired sample: A0 single worker or A1 static multi-worker DAG."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from experiments.evaluation.runner import RunnerSettings, run_sample
from experiments.evaluation.stage_e_tasks import definition_by_id, variants

REPO_ROOT = Path(__file__).resolve().parents[2]


def _task_for(sample_id: str):
    definition = definition_by_id(sample_id)
    a0, a1 = variants(definition)
    if sample_id.endswith("-a0"):
        return a0
    if sample_id.endswith("-a1"):
        return a1
    raise ValueError(f"Stage E sample id must end in -a0 or -a1: {sample_id}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="atlas-stage-e1")
    parser.add_argument("--sample-id", required=True)
    parser.add_argument("--repetition", type=int, default=0)
    parser.add_argument("--runs-root", required=True)
    parser.add_argument("--max-turns", type=int, default=14)
    parser.add_argument("--timeout", type=int, default=150)
    parser.add_argument("--max-tool-calls", type=int, default=40)
    args = parser.parse_args(argv)
    task = _task_for(args.sample_id)
    variant = "A0" if args.sample_id.endswith("-a0") else "A1"
    settings = RunnerSettings(
        benchmark_id="atlasos-stage-e1",
        dataset_version="1.0.0",
        scorer_version="1.0.0",
        runs_root=Path(args.runs_root).resolve(),
        timeout_seconds=args.timeout,
        max_turns=args.max_turns,
        max_tool_calls=max(args.max_tool_calls, 60),
        variant=variant,
        capture_evidence=True,
    )
    result = asyncio.run(run_sample(task, settings=settings, repetition_id=args.repetition))
    print(
        json.dumps(
            {
                "sample_id": result.task.sample_id,
                "variant": variant,
                "repetition_id": args.repetition,
                "success": result.success,
                "failure_class": result.failure_class,
                "run_id": result.identity.run_id,
                "run_dir": result.run_dir,
                "workers": len(result.task.workers),
                "tool_calls": result.metrics.tool_calls,
                "prompt_tokens": result.metrics.prompt_tokens,
                "completion_tokens": result.metrics.completion_tokens,
                "cost": result.metrics.estimated_cost_usd,
                "latency_ms": result.metrics.latency_ms,
            },
            sort_keys=True,
        )
    )
    return 0 if result.success else 1


if __name__ == "__main__":
    sys.exit(main())
