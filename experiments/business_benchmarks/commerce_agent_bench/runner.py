"""First native business agent run: one Atlas worker on a Commerce Agent Bench task.

The chain reuses the existing runtime end to end — ``ScopedWorkerFactory`` for identity,
``AtlasToolRuntime`` for tool registration, the M3 ``ActionSafetyGate`` for authorization, the M4
ledger and M7 recorder for evidence, the shared budget state, and the native grader for scoring.
Nothing here re-implements scheduling, tool execution or the agent loop.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from anycode import OrchestratorConfig, TaskSpec, TeamConfig
from anycode.identity import ExecutionContext
from anycode.plugins.registry import register_provider_factory
from anycode.runstore.store import FilesystemRunStore
from experiments.business_benchmarks.commerce_agent_bench.container import WslDockerBackend
from experiments.business_benchmarks.commerce_agent_bench.environment import (
    EnvironmentConfig,
    NativeEnvironment,
)
from experiments.business_benchmarks.commerce_agent_bench.grader import NativeGrader, NativeScore
from experiments.business_benchmarks.commerce_agent_bench.tool_bridge import (
    SLACK_PORT,
    SlackTransport,
    build_slack_toolset,
)
from experiments.business_benchmarks.commerce_agent_bench.upstream import (
    load_task,
    task_digests,
    upstream_root,
    verify_commit,
)
from experiments.evaluation.runner import (
    AllToolsAtlasRuntime,
    ModelRecordingAdapter,
    build_deepseek_adapter,
)

from atlasos.actions import ActionCatalog, ActionPolicy, ActionRule
from atlasos.actions.catalog import ActionMapping
from atlasos.budget import BUDGET_MODE_GUARDED, RunBudgetState
from atlasos.integrations.anycode_engine import AtlasContextEngine
from atlasos.ledger import SQLiteActionLedgerStore
from atlasos.trajectory.recorder import TrajectoryRecorder
from atlasos.workers import (
    DEFAULT_PROFILES,
    ScopedWorkerFactory,
    WorkerAssignment,
    WorkerCatalog,
    WorkerProfile,
    WorkerStore,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
EVIDENCE_ROOT = REPO_ROOT / "experiments" / "evidence" / "stage-b0-r2"
DEFAULT_TASK = "api-slack-supplier-quote-thread-consolidation"
DELIVERABLES = ("outputs/answer.json", "outputs/quote_consolidation.md")


@dataclass(frozen=True)
class RunConfig:
    task_id: str = DEFAULT_TASK
    model: str = "deepseek-chat"
    max_turns: int = 40
    token_budget: int = 220_000
    max_tool_calls: int = 60
    budget_mode: str = BUDGET_MODE_GUARDED
    provider_timeout_seconds: float = 90.0


@dataclass
class RunOutcome:
    record: dict[str, Any] = field(default_factory=dict)
    score: NativeScore | None = None
    run_dir: Path | None = None


def _business_catalog(toolset: Any) -> ActionCatalog:
    return ActionCatalog(
        mappings=tuple(ActionMapping(name, action) for name, action in toolset.actions.items())
    )


def _business_policy() -> ActionPolicy:
    allow = ("filesystem.read", "filesystem.write", "external.read")
    return ActionPolicy(
        name="commerce-bench-business",
        default="DENY",
        rules=tuple(
            ActionRule(id=f"allow-{action}", decision="ALLOW", action=action) for action in allow
        ),
    )


def _business_profile(toolset: Any) -> WorkerProfile:
    return WorkerProfile(
        profile_id="business_worker",
        role="commerce operations analyst",
        instructions=(
            "You complete a business task with the provided tools only. Read the task materials, "
            "gather evidence from the task's own services, and write the required deliverables. "
            "Never invent a fact you have not read, and never claim work you did not do."
        ),
        base_tools=toolset.agent_tools(),
        operating_scope=("general", "communication"),
        max_turns=20,
    )


def _context(run_id: str, task_id: str) -> ExecutionContext:
    return ExecutionContext(
        principal="user:b0-r2",
        tenant_scope="atlas-b0-r2",
        classification="internal",
        attributes={"run_id": run_id, "task_id": task_id, "benchmark": "commerce-agent-bench"},
    )


async def _probe_host_port(port: int) -> bool:
    import urllib.request

    def _get() -> bool:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as response:
                return response.status == 200
        except Exception:  # noqa: BLE001 - reachability probe
            return False

    return await asyncio.to_thread(_get)


async def _resolve_transport(backend: Any, environment: Any) -> SlackTransport:
    published = environment.published_ports.get(SLACK_PORT)
    if published is not None and await _probe_host_port(published):
        return SlackTransport(backend=backend, container=environment.container, host_port=published)
    return SlackTransport(backend=backend, container=environment.container)


def _collect_tool_calls(agent_results: dict[str, Any]) -> list[dict[str, Any]]:
    collected: list[dict[str, Any]] = []
    for worker, payload in agent_results.items():
        for call in getattr(payload, "tool_calls", None) or ():
            entry = call if isinstance(call, dict) else getattr(call, "__dict__", {})
            collected.append({"worker": worker, **dict(entry)})
    return collected


async def run_business_task(
    *,
    config: RunConfig | None = None,
    upstream: Path | None = None,
    evidence_root: Path = EVIDENCE_ROOT,
    fake_provider: Any = None,
) -> RunOutcome:
    settings = config or RunConfig()
    # The provider key lives in the repository .env; the evaluation runner loads it the same way.
    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env", override=False)
    root = upstream or upstream_root()
    commit = verify_commit(root)
    task = load_task(settings.task_id, root=root)
    digests = task_digests(task)

    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    run_id = f"b0r2-{uuid.uuid4().hex[:12]}"
    run_dir = evidence_root / "runs" / f"{stamp}-{run_id}-{settings.task_id}"
    run_dir.mkdir(parents=True, exist_ok=True)

    backend = WslDockerBackend()
    environment = NativeEnvironment(
        task,
        run_dir / "native",
        backend,
        config=EnvironmentConfig(platform="linux/amd64", extra_ports=(SLACK_PORT,)),
    )
    startup_started = time.perf_counter()
    handle = environment.create()
    startup_ms = round((time.perf_counter() - startup_started) * 1000.0, 3)
    record: dict[str, Any] = {
        "stage": "b0-r2",
        "run_id": run_id,
        "task_id": task.task_id,
        "upstream": {
            "commit": commit,
            "image": task.image,
            "task_digests": digests.as_dict(),
        },
        "config": {
            "model": settings.model,
            "max_turns": settings.max_turns,
            "token_budget": settings.token_budget,
            "max_tool_calls": settings.max_tool_calls,
            "budget_mode": settings.budget_mode,
            "ownership_mode": "off",
            "execution_integrity_mode": "record",
            "compaction": "off",
            "tool_result_offload": "off",
        },
        "environment": handle.as_dict(),
        "timings_ms": {"environment_startup": startup_ms},
    }
    if not handle.usable:
        record["status"] = "ENVIRONMENT_FAILURE"
        record["failure"] = handle.environment_failure
        environment.teardown(handle)
        _write(run_dir / "run.json", record)
        return RunOutcome(record=record, run_dir=run_dir)

    try:
        return await _execute_and_grade(
            task=task,
            handle=handle,
            environment=environment,
            backend=backend,
            run_dir=run_dir,
            record=record,
            settings=settings,
            fake_provider=fake_provider,
            eval_config_path=REPO_ROOT / "experiments" / "configs" / "eval-benchmark.yaml",
            run_id=run_id,
        )
    except BaseException:
        # A crashed run must never leave a container behind.
        environment.teardown(handle)
        raise


async def _execute_and_grade(
    *,
    task: Any,
    handle: Any,
    environment: NativeEnvironment,
    backend: Any,
    run_dir: Path,
    record: dict[str, Any],
    settings: RunConfig,
    fake_provider: Any,
    eval_config_path: Path,
    run_id: str,
) -> RunOutcome:
    transport = await _resolve_transport(backend, handle)
    record["environment"]["slack_transport"] = (
        f"host:{transport.host_port}" if transport.host_port else "container-exec"
    )
    toolset = build_slack_toolset(
        transport, workspace=handle.workspace_dir, allowed_deliverables=DELIVERABLES
    )
    record["tools"] = {
        "names": list(toolset.agent_tools()),
        "schema_digest": toolset.schema_digest,
        "actions": toolset.actions,
    }

    observations: list[Any] = []
    resolved_models: list[str] = []
    provider_name = f"b0r2-{uuid.uuid4().hex[:10]}"

    async def factory(**_: Any) -> Any:
        if fake_provider is not None:
            return fake_provider
        inner = build_deepseek_adapter(
            settings.model, observations.append, timeout_seconds=settings.provider_timeout_seconds
        )
        return ModelRecordingAdapter(inner, resolved_models)

    register_provider_factory(provider_name, factory)

    recorder = TrajectoryRecorder(
        FilesystemRunStore(run_dir / "trajectory"),
        run_id,
        agent_name="atlas-b0-r2",
        model=settings.model,
    )
    ledger = SQLiteActionLedgerStore(run_dir / "state" / "ledger.db")

    # Business tools are registered as external tools, so they become native provider tools only
    # through the "expose every searchable tool" runtime the evaluation harness uses for its
    # ablations. ``tool.search`` / ``dispatch_action`` stay hidden, and the coding core tools are
    # removed: a commerce worker has no shell, no file system beyond the task workspace and no git.
    class BusinessAtlasRuntime(AllToolsAtlasRuntime):
        @property
        def agent_tools(self) -> tuple[str, ...]:
            allowed = set(toolset.agent_tools())
            return tuple(name for name in super().agent_tools if name in allowed)

    runtime = BusinessAtlasRuntime(
        run_id,
        state_path=run_dir / "state" / "tool_state.json",
        conversation_path=run_dir / "state" / "tool_conversations.json",
        external_tools=toolset.tools,
        action_policy=_business_policy(),
        action_catalog=_business_catalog(toolset),
        action_ledger=ledger,
        trajectory_recorder=recorder,
        runtime_instance_id=f"b0r2-{run_id}",
        execution_integrity_mode="record",
    )
    budget_state = (
        RunBudgetState(limit_tokens=settings.token_budget)
        if settings.budget_mode == BUDGET_MODE_GUARDED and settings.token_budget > 0
        else None
    )
    worker_factory = ScopedWorkerFactory(
        model=settings.model,
        provider=provider_name,
        store=WorkerStore(run_dir / "state" / "workers.json"),
        catalog=WorkerCatalog(profiles=(*DEFAULT_PROFILES, _business_profile(toolset))),
        available_tools=toolset.agent_tools(),
        trajectory_recorder=recorder,
    )
    task_text = (
        f"{task.prompt}\n\n"
        "Tools available: slack_list_messages, slack_get_message, read_task_file, "
        "write_deliverable.\n"
        "Deliverables must be written with write_deliverable to exactly: "
        + ", ".join(DELIVERABLES)
        + ". The mock audit directory is not writable and must not be touched."
    )
    worker = worker_factory.create(
        parent_context=_context(run_id, task.task_id),
        parent_agent="atlas-b0-r2",
        assignment=WorkerAssignment(profile_id="business_worker", task=task_text),
        worker_id=f"{run_id}-analyst",
        task_id=f"{run_id}-analyst",
    )
    engine = AtlasContextEngine(
        OrchestratorConfig(),
        tool_runtime=runtime,
        budget_state=budget_state,
    )
    agent_config = worker.agent_config.model_copy(
        update={
            "provider": provider_name,
            "max_turns": settings.max_turns,
            "max_tokens": 4096,
            "temperature": 0.0,
        }
    )
    team = engine.create_team("b0r2", TeamConfig(name="b0r2", agents=[agent_config]))
    spec = TaskSpec(
        title=f"{task.task_id}-analysis",
        description=task_text,
        assignee=worker.worker_name,
    )

    agent_started = time.perf_counter()
    execution_status = "EXECUTION_OK"
    stop_reason: str | None = None
    agent_result: Any = None
    try:
        agent_result = await asyncio.wait_for(
            engine.run_tasks(team, [spec]), timeout=task.agent_timeout_seconds
        )
    except TimeoutError:
        execution_status = "TIMEOUT"
        stop_reason = f"task timeout after {task.agent_timeout_seconds}s"
    except Exception as error:  # noqa: BLE001 - recorded, never hidden
        execution_status = "RUNTIME_FAILURE"
        stop_reason = f"{type(error).__name__}: {error}"
    agent_ms = round((time.perf_counter() - agent_started) * 1000.0, 3)

    tool_calls = _collect_tool_calls(getattr(agent_result, "agent_results", {}) or {})
    prompt_tokens = 0
    completion_tokens = 0
    cache_hit = 0
    cache_miss = 0
    for item in observations:
        usage = getattr(item, "usage", None)
        prompt_tokens += _usage(usage, "prompt_tokens")
        completion_tokens += _usage(usage, "completion_tokens")
        cache_hit += _usage(usage, "cache_hit_tokens")
        cache_miss += _usage(usage, "cache_miss_tokens")
    over_budget = (
        budget_state is not None
        and settings.token_budget > 0
        and prompt_tokens + completion_tokens > settings.token_budget
    )
    if over_budget:
        execution_status = "BUDGET_EXCEEDED"
    if not observations and execution_status == "EXECUTION_OK":
        # No provider call was observed: the model was never reached, so this is not a model
        # failure and must not be reported as one.
        execution_status = "PROVIDER_UNAVAILABLE"
        stop_reason = stop_reason or "no provider call was observed"

    record["agent"] = {
        "execution_status": execution_status,
        "stop_reason": stop_reason,
        "worker_count": 1,
        "runtime_success": bool(getattr(agent_result, "success", False)),
        "tool_calls": len(tool_calls),
        "tool_call_names": sorted({str(call.get("tool_name")) for call in tool_calls}),
        "denied_actions": sum(
            1
            for call in tool_calls
            if "deny" in str(call.get("output", "")).lower()
            or "denied" in str(call.get("output", "")).lower()
        ),
        "model_turns": len(observations),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cache_hit_tokens": cache_hit or None,
        "cache_miss_tokens": cache_miss or None,
        "budget_exceeded": over_budget,
        "resolved_model": (resolved_models or [settings.model])[-1],
        "observations": len(observations),
    }
    record["timings_ms"]["agent_execution"] = agent_ms

    grade_started = time.perf_counter()
    score = NativeGrader().evaluate(
        task, outputs_dir=handle.outputs_dir, reward_path=run_dir / "reward.json"
    )
    record["timings_ms"]["grader"] = round((time.perf_counter() - grade_started) * 1000.0, 3)
    record["native"] = score.as_dict()
    record["deliverables"] = sorted(
        path.relative_to(handle.outputs_dir).as_posix()
        for path in handle.outputs_dir.rglob("*")
        if path.is_file()
    )
    record["ledger"] = _ledger_summary(ledger)
    record["trajectory"] = _trajectory_summary(recorder)
    record["environment"]["timings_ms"] = handle.timings_ms

    record["status"] = (
        "ENVIRONMENT_FAILURE"
        if score.environment_failure
        else ("NATIVE_PASS" if score.passed else "NATIVE_FAIL")
    )
    cleanup_error = environment.teardown(handle)
    record["cleanup_failure"] = cleanup_error
    record["timings_ms"]["cleanup"] = handle.timings_ms.get("cleanup_ms")
    record["timings_ms"]["end_to_end"] = (
        record["timings_ms"].get("environment_startup", 0.0)
        + agent_ms
        + record["timings_ms"]["grader"]
        + (record["timings_ms"]["cleanup"] or 0)
    )
    _write(run_dir / "run.json", record)
    _write(run_dir / "tool_calls.json", tool_calls)
    return RunOutcome(record=record, score=score, run_dir=run_dir)


def _usage(usage: Any, field: str) -> int:
    """Read a token field from either the pydantic usage model or a plain mapping."""

    if usage is None:
        return 0
    if isinstance(usage, dict):
        return int(usage.get(field) or 0)
    return int(getattr(usage, field, 0) or 0)


def _ledger_summary(ledger: Any) -> dict[str, Any]:
    try:
        records = list(ledger.list_records())  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - ledger API differences must not break the report
        return {"records": None}
    return {
        "records": len(records),
        "tool_names": sorted({getattr(item, "tool_name", "?") for item in records}),
    }


def _trajectory_summary(recorder: Any) -> dict[str, Any]:
    failures = getattr(recorder, "recording_failures", 0)
    return {"recording_failures": failures, "complete": failures == 0}


def _write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> dict[str, Any]:
    import argparse

    parser = argparse.ArgumentParser(description="B0-R2 first native business agent run")
    parser.add_argument("--task-id", default=DEFAULT_TASK)
    parser.add_argument("--model", default="deepseek-chat")
    parser.add_argument("--max-turns", type=int, default=40)
    parser.add_argument("--token-budget", type=int, default=220_000)
    args = parser.parse_args(argv)
    outcome = asyncio.run(
        run_business_task(
            config=replace(
                RunConfig(),
                task_id=args.task_id,
                model=args.model,
                max_turns=args.max_turns,
                token_budget=args.token_budget,
            )
        )
    )
    print(json.dumps(outcome.record, indent=2, default=str)[:6000])
    return outcome.record


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    main()


__all__ = ["DELIVERABLES", "RunConfig", "RunOutcome", "run_business_task", "main"]
