"""Fair AnyCode baseline for the AtlasOS Agent Benchmark.

The baseline uses AnyCode 0.9.0's native agent loop and built-in tools on a disposable Host copy
of the same visible fixture. It uses the same model, temperature, turn/token budget, task text,
and independent oracle. It does not claim AtlasOS sandbox isolation or M1 progressive discovery;
those are evaluated separately in the reliability matrix and the tool-discovery category.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from anycode import AgentConfig, AnyCode, OrchestratorConfig, TaskSpec, TeamConfig
from anycode.plugins.registry import register_provider_factory
from anycode.types import ToolSecurityPolicy
from dotenv import load_dotenv
from experiments.evaluation.fixtures import materialize_fixture, workspace_hashes
from experiments.evaluation.models import (
    BenchmarkTask,
    EvalRunResult,
    FailureClass,
    RunIdentity,
    RuntimeMetrics,
    canonical_digest,
)
from experiments.evaluation.oracle import evaluate_oracle
from experiments.evaluation.runner import (
    EvaluationEnvironmentError,
    ModelRecordingAdapter,
    RunnerSettings,
    _aggregate_observations,
    _estimate_cost,
    _jsonable,
    build_deepseek_adapter,
    evaluation_resilience,
)

from atlasos.config import load_config
from atlasos.experiments import git_commit, git_dirty

REPO_ROOT = Path(__file__).resolve().parents[2]
BASELINE_TOOLS = ["bash", "file_read", "file_write", "file_edit", "grep", "list_files"]
BASELINE_SYSTEM_PROMPT = (
    "You are a coding agent. Work only inside the provided workspace. Use the available tools "
    "to inspect, edit, and test the repository. Do not ask questions. Do not modify tests. "
    "This baseline runs on Windows: use relative paths and Windows-compatible shell commands "
    "(`dir`, `type`, `python`). Do not assume a Linux `/workspace/project` path exists."
)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(_jsonable(payload), indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def _baseline_supported(task: BenchmarkTask) -> bool:
    # AnyCode has no equivalent of AtlasOS M1 progressive MCP discovery. Tool-discovery tasks
    # are therefore excluded from the baseline comparison and reported as NOT_EVALUATED.
    return task.category != "tool_discovery"


async def run_baseline_sample(
    task: BenchmarkTask,
    *,
    settings: RunnerSettings,
    repetition_id: int,
    baseline_root: Path,
) -> EvalRunResult:
    if not _baseline_supported(task):
        raise EvaluationEnvironmentError(
            f"{task.sample_id}: tool-discovery tasks have no fair AnyCode baseline"
        )
    load_dotenv(REPO_ROOT / ".env", override=False)
    config = load_config(settings.config_path)
    started_at = datetime.now(UTC)
    started = time.perf_counter()
    run_id = f"base-{uuid.uuid4().hex[:12]}"
    run_dir = (baseline_root / task.sample_id / f"r{repetition_id}-{run_id}").resolve()
    run_dir.mkdir(parents=True, exist_ok=False)
    fixture_dir = run_dir / "fixture"
    workspace = run_dir / "workspace"
    materialize_fixture(task, fixture_dir)
    materialize_fixture(task, workspace)
    baseline_hashes = workspace_hashes(fixture_dir)
    identity = RunIdentity(
        benchmark_id=settings.benchmark_id,
        dataset_version=settings.dataset_version,
        scorer_version=settings.scorer_version,
        sample_id=task.sample_id,
        repetition_id=repetition_id,
        run_id=run_id,
        git_commit=git_commit(REPO_ROOT),
        git_dirty=git_dirty(REPO_ROOT),
        config_digest=canonical_digest({"engine": "anycode-0.9.0", "settings": settings.digest()}),
        fixture_digest=task.fixture_digest,
        model=settings.model_override or "deepseek-chat",
        provider="deepseek",
        started_at=started_at,
    )
    observations: list[Any] = []
    resolved_models: list[str] = []
    metrics = RuntimeMetrics()
    failure_class: FailureClass | None = None
    failure_reason: str | None = None
    final_output = ""
    agent_success = False
    tool_calls: list[dict[str, Any]] = []
    worker_results: dict[str, Any] = {}
    environment: dict[str, Any] = {
        "engine": "anycode-0.9.0",
        "workspace_isolation": "disposable_host_copy",
        "real_sandbox_provider": False,
        "temperature": settings.temperature,
        "max_turns": settings.max_turns,
        "max_tokens": settings.max_tokens,
        "token_budget": settings.token_budget,
    }
    engine: AnyCode | None = None
    final_result: EvalRunResult | None = None
    try:
        if not os.environ.get("DEEPSEEK_API_KEY"):
            raise EvaluationEnvironmentError("DEEPSEEK_API_KEY is not configured")
        provider_name = f"atlas-baseline-{uuid.uuid4().hex[:12]}"

        async def factory(**_: Any) -> ModelRecordingAdapter:
            inner = build_deepseek_adapter(
                settings.model_override or "deepseek-chat",
                observations.append,
            )
            return ModelRecordingAdapter(inner, resolved_models)

        register_provider_factory(provider_name, factory)
        engine = AnyCode(OrchestratorConfig())
        agent_configs = [
            AgentConfig(
                name=f"baseline:{step.task_id}",
                model=settings.model_override or "deepseek-chat",
                provider=provider_name,
                system_prompt=BASELINE_SYSTEM_PROMPT,
                tools=BASELINE_TOOLS,
                max_turns=step.max_turns or settings.max_turns,
                max_tokens=settings.max_tokens,
                temperature=settings.temperature,
                provider_resilience=evaluation_resilience(),
                tool_security=ToolSecurityPolicy(workspace_root=str(workspace)),
            )
            for step in task.workers
        ]
        team = engine.create_team(
            "baseline",
            TeamConfig(name="baseline", agents=agent_configs),
        )
        title_by_id = {step.task_id: f"{task.sample_id}-{step.task_id}" for step in task.workers}
        environment_note = (
            f"The disposable workspace root is {workspace}. Use relative paths and "
            "Windows-compatible shell commands (`dir`, `type`, `python`)."
        )
        specs = [
            TaskSpec(
                title=title_by_id[step.task_id],
                description=f"{environment_note}\n\n{step.prompt}",
                assignee=f"baseline:{step.task_id}",
                depends_on=[title_by_id[item] for item in step.depends_on],
            )
            for step in task.workers
        ]
        result = await asyncio.wait_for(
            engine.run_tasks(team, specs),
            timeout=settings.timeout_seconds,
        )
        agent_success = bool(result.success)
        for agent, agent_result in result.agent_results.items():
            calls = [
                {
                    "agent": agent,
                    "tool_name": record.tool_name,
                    "input": _jsonable(record.input),
                    "output": str(record.output)[:4000],
                    "duration_ms": round(float(record.duration) * 1000.0, 3),
                }
                for record in agent_result.tool_calls
            ]
            tool_calls.extend(calls)
            worker_results[agent] = {
                "success": bool(agent_result.success),
                "output": str(agent_result.output or ""),
                "tool_calls": len(calls),
                "token_usage": _jsonable(agent_result.token_usage),
            }
        final_output = "\n".join(
            str(value.get("output", "")) for value in worker_results.values() if value.get("output")
        )
    except TimeoutError:
        failure_class = "TIMEOUT"
        failure_reason = f"baseline sample exceeded {settings.timeout_seconds}s"
    except BaseException as error:  # noqa: BLE001
        if not isinstance(error, Exception):
            raise
        name = type(error).__name__
        module = type(error).__module__
        if isinstance(error, EvaluationEnvironmentError):
            failure_class = "ENVIRONMENT_FAILURE"
        elif "openai" in module or "API" in name or "RateLimit" in name:
            failure_class = "MODEL_FAILURE"
        else:
            failure_class = "RUNTIME_FAILURE"
        failure_reason = f"{name}: {error}"
    finally:
        if engine is not None:
            with contextlib.suppress(Exception):
                await engine.close()
        aggregate = _aggregate_observations(observations)
        metrics.model_turns = len(observations)
        metrics.tool_calls = len(tool_calls)
        metrics.prompt_tokens = int(aggregate["prompt_tokens"]) if observations else None
        metrics.completion_tokens = int(aggregate["completion_tokens"]) if observations else None
        metrics.cache_hit_tokens = (
            int(aggregate["cache_hit_tokens"])
            if aggregate["cache_hit_tokens"] is not None
            else None
        )
        metrics.cache_miss_tokens = (
            int(aggregate["cache_miss_tokens"])
            if aggregate["cache_miss_tokens"] is not None
            else None
        )
        metrics.estimated_cost_usd = _estimate_cost(observations, config.pricing)
        metrics.extra["cache_metrics_complete"] = bool(aggregate["cache_metrics_complete"])
        metrics.extra["inference_latency_ms"] = aggregate["inference_latency_ms"]
        metrics.worker_selection = sorted(worker_results)
        metrics.sandbox_created = False
        metrics.sandbox_destroyed = False
        environment["resolved_models"] = sorted(set(resolved_models))
        environment["pricing_basis"] = (
            "DeepSeek Flash peak rates snapshot 2026-11-06; deepseek-chat resolves to "
            "deepseek-flash; upper-bound estimate"
        )
        metrics.latency_ms = round((time.perf_counter() - started) * 1000.0, 3)

        oracle = evaluate_oracle(
            task,
            workspace=workspace,
            baseline_hashes=baseline_hashes,
            tool_calls=tool_calls,
            worker_results=worker_results,
            run_dir=run_dir,
            host_fixture_unchanged=workspace_hashes(fixture_dir) == baseline_hashes,
        )
        if failure_class is None and not oracle.passed:
            failure_class = "ORACLE_FAILURE"
            failure_reason = "; ".join(oracle.failure_reasons)[:2000] or "oracle failed"
        if failure_class is None:
            failure_class = "TASK_SUCCESS"
        success = failure_class == "TASK_SUCCESS" and oracle.passed
        finished_at = datetime.now(UTC)
        final_result = EvalRunResult(
            identity=identity,
            task=task,
            success=success,
            failure_class=failure_class,
            failure_reason=failure_reason,
            final_output=final_output,
            agent_success=agent_success,
            tool_calls=tool_calls,
            worker_results=worker_results,
            oracle=oracle,
            metrics=metrics,
            run_dir=str(run_dir),
            fixture_dir=str(fixture_dir),
            workspace_export=str(workspace),
            patch_digest=None,
            started_at=started_at,
            finished_at=finished_at,
            environment=environment,
            safety={
                "host_fixture_unchanged": workspace_hashes(fixture_dir) == baseline_hashes,
                "sandbox_destroyed": False,
                "real_sandbox_provider": False,
                "workspace_isolation": "disposable_host_copy",
            },
        )
        _write_json(run_dir / "result.json", final_result.as_dict())
        ledger_path = baseline_root / settings.benchmark_id / "runs.jsonl"
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with ledger_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(_jsonable(final_result.as_dict()), sort_keys=True) + "\n")

    if final_result is None:
        raise RuntimeError("baseline run finished without a persisted result")
    return final_result


async def run_baseline_suite(
    *,
    settings: RunnerSettings,
    tasks: tuple[BenchmarkTask, ...],
    repetition_id: int,
    baseline_root: Path,
) -> list[EvalRunResult]:
    results: list[EvalRunResult] = []
    for task in tasks:
        if not _baseline_supported(task):
            continue
        results.append(
            await run_baseline_sample(
                task,
                settings=settings,
                repetition_id=repetition_id,
                baseline_root=baseline_root,
            )
        )
    return results


def summarize_baseline(baseline_root: Path) -> dict[str, Any]:
    results = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(baseline_root.rglob("result.json"))
    ]
    success = sum(1 for item in results if item.get("success"))
    by_category: dict[str, dict[str, int]] = {}
    for item in results:
        category = str(item.get("category"))
        entry = by_category.setdefault(category, {"success": 0, "total": 0})
        entry["total"] += 1
        entry["success"] += 1 if item.get("success") else 0
    return {
        "engine": "anycode-0.9.0",
        "runs": len(results),
        "task_success_rate": success / len(results) if results else None,
        "by_category": {
            category: {
                "task_success_rate": value["success"] / value["total"] if value["total"] else None,
                "success": value["success"],
                "total": value["total"],
            }
            for category, value in by_category.items()
        },
        "tool_discovery": "NOT_EVALUATED (no fair AnyCode M1 progressive-discovery equivalent)",
    }
