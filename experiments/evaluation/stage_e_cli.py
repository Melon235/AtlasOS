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


def _handoff_summary(payload: dict | None) -> dict | None:
    """Compact Stage G1 handoff counters; the full record stays in result.json."""

    if not payload:
        return None
    return {
        key: payload.get(key)
        for key in (
            "mode",
            "result_chars",
            "inline_chars",
            "offloaded_chars",
            "inline_handoffs",
            "offloaded_handoffs",
            "refs_issued",
            "read_calls",
            "read_success",
            "read_failures",
            "consumers_read_complete",
        )
    }


def _budget_summary(payload: dict | None) -> dict | None:
    """Compact Stage G2a budget counters; the full record stays in result.json."""

    if not payload:
        return None
    return {
        key: payload.get(key)
        for key in (
            "mode",
            "limit_tokens",
            "used_tokens",
            "reserved_tokens",
            "peak_reserved_tokens",
            "requests_started",
            "requests_completed",
            "requests_written_off",
            "admission_rejections",
            "budget_overrun_tokens",
            "stop_reason",
        )
    }


def _compaction_summary(payload: dict | None) -> dict | None:
    """Compact Stage G2b-1 counters; the full record stays in result.json."""

    if not payload:
        return None
    return {
        key: payload.get(key)
        for key in (
            "mode",
            "compaction_events",
            "results_archived",
            "original_chars_archived",
            "placeholder_chars",
            "history_bytes_stored",
            "archive_failures",
            "read_calls",
            "read_success",
            "read_denied",
        )
    }


def _tool_result_summary(payload: dict | None) -> dict | None:
    """Compact Stage G3-2 ingress counters; the full record stays in result.json."""

    if not payload:
        return None
    return {
        key: payload.get(key)
        for key in (
            "mode",
            "max_inline_chars",
            "offloads",
            "original_chars_archived",
            "placeholder_chars",
            "archive_failures",
            "skipped_no_identity",
            "skipped_recursive",
            "skipped_already_offloaded",
            "error_results_offloaded",
        )
    }


def _contract_summary(payload: dict | None) -> dict | None:
    """Compact Stage G3-3 contract record; the per-worker detail stays in result.json."""

    if not payload:
        return None
    return {key: payload.get(key) for key in ("mode", "overhead_chars", "task_text_digest")}


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
    parser.add_argument("--handoff-mode", choices=("legacy", "dual"), default="legacy")
    parser.add_argument("--budget-mode", choices=("legacy", "guarded"), default="legacy")
    parser.add_argument("--compaction-mode", choices=("off", "rolling_2"), default="off")
    parser.add_argument("--tool-error-mode", choices=("legacy", "corrected"), default="legacy")
    parser.add_argument("--tool-result-mode", choices=("off", "ingress_offload"), default="off")
    parser.add_argument(
        "--worker-contract-mode", choices=("legacy", "structured"), default="legacy"
    )
    parser.add_argument(
        "--delegation-route-mode", choices=("off", "shadow", "selective"), default="off"
    )
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
        handoff_mode=args.handoff_mode,
        budget_mode=args.budget_mode,
        compaction_mode=args.compaction_mode,
        tool_error_mode=args.tool_error_mode,
        tool_result_mode=args.tool_result_mode,
        worker_contract_mode=args.worker_contract_mode,
        delegation_route_mode=args.delegation_route_mode,
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
                "handoff_mode": args.handoff_mode,
                "handoff": _handoff_summary(result.metrics.extra.get("handoff")),
                "budget_mode": args.budget_mode,
                "budget": _budget_summary(result.metrics.extra.get("budget")),
                "compaction_mode": args.compaction_mode,
                "tool_error_mode": args.tool_error_mode,
                "tool_result_mode": args.tool_result_mode,
                "worker_contract_mode": args.worker_contract_mode,
                "delegation_route_mode": args.delegation_route_mode,
                "routing": result.metrics.extra.get("routing"),
                "worker_contract": _contract_summary(result.metrics.extra.get("worker_contract")),
                "tool_result": _tool_result_summary(result.metrics.extra.get("tool_result")),
                "compaction": _compaction_summary(result.metrics.extra.get("compaction")),
            },
            sort_keys=True,
        )
    )
    return 0 if result.success else 1


if __name__ == "__main__":
    sys.exit(main())
