"""Thin evaluation adapter over the existing AtlasOS V1/V2 runtime.

This module composes the same AnyCode engine, Atlas tool runtime, M3 policy, M4 ledger,
M5 sandbox manager, M6 suspension stores, and M7 recorder used by the acceptance harnesses.
It does not implement a second runtime, tool executor, policy, approval, idempotency, or lock
system. Each sample gets a fresh run directory, sandbox, stores, and worker state.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import platform
import sys
import time
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from anycode import OrchestratorConfig, TaskSpec, TeamConfig
from anycode.identity.context import ExecutionContext
from anycode.plugins.registry import register_provider_factory
from anycode.runstore.store import FilesystemRunStore
from anycode.tools.idempotency import SQLiteToolIdempotencyStore
from anycode.types import (
    MCPServerConfig,
    ProviderResilienceConfig,
    RetryPolicy,
    ToolSecurityPolicy,
)
from dotenv import load_dotenv
from experiments.evaluation.fixtures import (
    materialize_fixture,
    stage_git_repo,
    workspace_digest,
    workspace_hashes,
)
from experiments.evaluation.models import (
    BenchmarkTask,
    EvalRunResult,
    FailureClass,
    RunIdentity,
    RuntimeMetrics,
    canonical_digest,
)
from experiments.evaluation.oracle import evaluate_oracle
from experiments.evaluation.stage_g3_4a_integrity import (
    FIXTURE_COMMIT_LABEL,
    enforce_evaluation_isolation,
)
from experiments.evaluation.stage_g3_worker_contracts import (
    WORKER_CONTRACT_LEGACY,
    WORKER_CONTRACT_MODES,
    contract_overhead,
    worker_task_text,
)
from experiments.evaluation.stage_r0_routing import (
    DELEGATION_ROUTE_MODES,
    DELEGATION_ROUTE_OFF,
    DELEGATION_ROUTE_SELECTIVE,
    a0_steps,
    decision_digest,
    plan_route,
    routing_policy,
)
from experiments.evaluation.stage_r1_1_ownership import (
    manifest_for_task,
    manifest_public_summary,
)
from experiments.evaluation.stage_r1_1_trace import compare_with_result, summarize_trace

from atlasos.actions import ActionCatalog, ActionPolicy, ActionRule
from atlasos.actions.catalog import DEFAULT_MAPPINGS, ActionMapping
from atlasos.budget import BUDGET_MODE_GUARDED, RunBudgetState
from atlasos.config import AtlasConfig, Pricing, load_config
from atlasos.experiments import git_commit, git_dirty
from atlasos.handoff import HANDOFF_READ_ACTION_MAPPING, HandoffNode, HandoffRuntime
from atlasos.history_compaction import (
    COMPACTION_MODES,
    COMPACTION_OFF,
    HISTORY_READ_ACTION_MAPPING,
    HistoryCompactionRuntime,
)
from atlasos.integrations.anycode_engine import AtlasContextEngine
from atlasos.integrations.deepseek import DeepSeekAdapter
from atlasos.ledger import SQLiteActionLedgerStore
from atlasos.metrics.models import InferenceObservation
from atlasos.ownership import (
    OWNERSHIP_MODE_OFF,
    OWNERSHIP_MODES,
    OwnershipManifestError,
    build_ownership_enforcer,
)
from atlasos.sandbox import (
    OpenSandboxOptions,
    ProjectSandboxManager,
    SandboxProjectConfig,
    SandboxWorkspaceTools,
    SQLiteSandboxBindingStore,
    build_sandbox_provider,
    export_patch,
    export_workspace,
    is_real_sandbox_provider,
    require_capabilities,
)
from atlasos.sandbox.models import SandboxLostError
from atlasos.tool_result_offload import (
    TOOL_RESULT_MODES,
    TOOL_RESULT_OFF,
    ToolResultIngressRuntime,
)
from atlasos.tools import AtlasToolRuntime
from atlasos.tools.conversation import set_current_task_id
from atlasos.trajectory.payloads import (
    EXECUTION_INTEGRITY_MODES,
    EXECUTION_INTEGRITY_OFF,
    EXECUTION_INTEGRITY_RECORD,
    ToolPayloadCorpus,
)
from atlasos.trajectory.recorder import TrajectoryRecorder
from atlasos.workers import ScopedWorkerFactory, WorkerAssignment, WorkerCatalog, WorkerStore
from atlasos.workers.routing import capabilities_from_catalog

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPO_ROOT / "experiments" / "configs" / "eval-benchmark.yaml"
MCP_SERVER_DIR = REPO_ROOT / "experiments" / "evaluation" / "mcp_servers"
_SANDBOX_TOOLS = frozenset({"file_read", "file_write", "file_edit", "grep", "list_files", "bash"})
_CONTROL_TOOLS = frozenset({"tool.search", "tool_search", "dispatch_action"})
_EVAL_MCP_TOOLS = (
    "lookup_fare",
    "employee_clearance",
    "convert_currency",
    "isbn_check_digit",
    "shipping_quote",
    "policy_lookup",
)
_EVAL_CATALOG = ActionCatalog(
    mappings=(
        *DEFAULT_MAPPINGS,
        *(ActionMapping(f"mcp_evaltools_{name}", "external.read") for name in _EVAL_MCP_TOOLS),
        # Stage G1 dual-channel handoff reader: read-only access to a stored dependency result.
        HANDOFF_READ_ACTION_MAPPING,
        # Stage G2b-1 history reader: read-only access to a worker's own archived tool results.
        HISTORY_READ_ACTION_MAPPING,
    )
)


class EvaluationEnvironmentError(RuntimeError):
    """Infrastructure is unavailable; this is not a model-quality failure."""


@dataclass(frozen=True)
class RunnerSettings:
    benchmark_id: str = "atlasos-agentbench-v1"
    dataset_version: str = "1.0.0"
    scorer_version: str = "1.0.0"
    config_path: Path = DEFAULT_CONFIG
    runs_root: Path = REPO_ROOT / "experiments" / "runs" / "evaluation"
    timeout_seconds: int = 900
    temperature: float = 0.0
    max_turns: int = 14
    max_tokens: int = 4096
    token_budget: int = 120_000
    budget_mode: str = "legacy"
    max_tool_calls: int = 40
    progressive_tools: bool = True
    model_override: str | None = None
    retain_sandbox_on_failure: bool = False
    variant: str = "default"
    capture_evidence: bool = False
    handoff_mode: str = "legacy"
    compaction_mode: str = "off"
    tool_error_mode: str = "legacy"
    tool_result_mode: str = "off"
    worker_contract_mode: str = "legacy"
    #: Stage R1-1: Host-owned write authorization for the shared workspace.
    ownership_mode: str = "off"
    #: Stage R1-1: durable tool-event evidence (private payload corpus + worker stop events).
    execution_integrity_mode: str = "off"
    delegation_route_mode: str = "off"
    #: Stage R0.5: which frozen routing policy the selective arm must use.
    delegation_policy_version: str = "r0.1.0"

    def digest(self) -> str:
        return canonical_digest(
            {
                "benchmark_id": self.benchmark_id,
                "dataset_version": self.dataset_version,
                "scorer_version": self.scorer_version,
                "temperature": self.temperature,
                "max_turns": self.max_turns,
                "max_tokens": self.max_tokens,
                "token_budget": self.token_budget,
                "budget_mode": self.budget_mode,
                "max_tool_calls": self.max_tool_calls,
                "progressive_tools": self.progressive_tools,
                "model_override": self.model_override,
                "variant": self.variant,
                "capture_evidence": self.capture_evidence,
                "handoff_mode": self.handoff_mode,
                "compaction_mode": self.compaction_mode,
                "tool_error_mode": self.tool_error_mode,
                "tool_result_mode": self.tool_result_mode,
                "worker_contract_mode": self.worker_contract_mode,
                "ownership_mode": self.ownership_mode,
                "execution_integrity_mode": self.execution_integrity_mode,
                "delegation_route_mode": self.delegation_route_mode,
                "delegation_policy_version": self.delegation_policy_version,
                "config": _config_digest(self.config_path),
            }
        )


class ModelRecordingAdapter:
    """Observation-only wrapper recording the provider-resolved model name."""

    def __init__(self, inner: DeepSeekAdapter, models: list[str]) -> None:
        self._inner = inner
        self._models = models

    @property
    def name(self) -> str:
        return self._inner.name

    async def chat(self, messages: Any, options: Any) -> Any:
        response = await self._inner.chat(messages, options)
        self._models.append(str(response.model))
        return response

    def stream(self, messages: Any, options: Any) -> Any:
        async def _generate() -> Any:
            async for event in self._inner.stream(messages, options):
                if getattr(event, "type", None) == "done":
                    response = getattr(event, "data", None)
                    model_name = getattr(response, "model", None)
                    if model_name is not None:
                        self._models.append(str(model_name))
                yield event

        return _generate()


class AllToolsAtlasRuntime(AtlasToolRuntime):
    """Evaluation-only ablation: expose every core tool natively, without tool.search."""

    @property
    def agent_tools(self) -> tuple[str, ...]:
        names = set(self.catalog.core_tool_names) | set(self.catalog.searchable_names)
        names.discard("tool.search")
        names.discard("dispatch_action")
        return tuple(sorted(names))


def _config_digest(path: Path) -> str:
    if not path.is_file():
        return "missing"
    return canonical_digest(load_config(path).model_dump(mode="json"))


def evaluation_resilience() -> ProviderResilienceConfig:
    """One bounded attempt per model call; the outer per-sample timeout is the hard stop."""

    return ProviderResilienceConfig(
        enabled=True,
        retry=RetryPolicy(
            max_attempts=1,
            base_delay_seconds=0.1,
            max_delay_seconds=1.0,
            jitter=False,
            call_timeout_seconds=60.0,
        ),
        max_concurrency=1,
    )


def _coding_policy() -> ActionPolicy:
    allow = (
        "control.search",
        "control.dispatch",
        "filesystem.read",
        "filesystem.write",
        "shell.execute",
        "external.read",
    )
    return ActionPolicy(
        name="evaluation-coding",
        default="DENY",
        rules=tuple(
            ActionRule(id=f"allow-{action}", decision="ALLOW", action=action) for action in allow
        ),
    )


def _environment_note(workspace_root: str) -> str:
    return (
        f"The repository is at {workspace_root} inside a remote Linux sandbox. "
        "Use Python 3 and portable POSIX shell commands. Do not assume access to the Host."
    )


def _build_provider(config: AtlasConfig):
    sandbox = config.sandbox
    provider = build_sandbox_provider(
        sandbox.provider,
        opensandbox_options=OpenSandboxOptions(
            endpoint=sandbox.endpoint,
            protocol=sandbox.protocol,
            timeout_minutes=sandbox.timeout_minutes,
            ready_timeout_seconds=sandbox.ready_timeout_seconds,
            cpu=sandbox.cpu,
            memory=sandbox.memory,
            use_server_proxy=sandbox.use_server_proxy,
        ),
    )
    return provider


def _project_config(config: AtlasConfig) -> SandboxProjectConfig:
    sandbox = config.sandbox
    return SandboxProjectConfig(
        provider_name=sandbox.provider,
        image=sandbox.image,
        snapshot=sandbox.snapshot,
        workspace_root=sandbox.workspace_root,
        max_bundle_bytes=sandbox.max_bundle_bytes,
        network=sandbox.network,
    )


def build_deepseek_adapter(
    model: str,
    on_call: Any,
    *,
    timeout_seconds: float = 45.0,
) -> DeepSeekAdapter:
    """Build the existing AtlasOS adapter with a bounded HTTP timeout.

    The evaluation harness must fail a hung provider call instead of blocking an entire round.
    This still uses the AtlasOS DeepSeek adapter and the same inference path.
    """

    from openai import AsyncOpenAI

    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise EvaluationEnvironmentError("DEEPSEEK_API_KEY is not configured")
    base_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    client = AsyncOpenAI(
        api_key=api_key,
        base_url=base_url,
        timeout=timeout_seconds,
        max_retries=0,
    )
    return DeepSeekAdapter(client=client, model=model, on_call=on_call)


def _estimate_cost(
    observations: list[InferenceObservation], pricing: Pricing | None
) -> float | None:
    if not observations or pricing is None or not pricing.is_complete:
        return None
    hit_rate = pricing.cache_hit_input_per_million
    miss_rate = pricing.cache_miss_input_per_million
    output_rate = pricing.output_per_million
    if hit_rate is None or miss_rate is None or output_rate is None:
        return None
    total = 0.0
    for observation in observations:
        usage = observation.usage
        if usage.cache_hit_tokens is None or usage.cache_miss_tokens is None:
            return None
        total += usage.cache_hit_tokens * hit_rate / 1_000_000
        total += usage.cache_miss_tokens * miss_rate / 1_000_000
        total += usage.completion_tokens * output_rate / 1_000_000
    return round(total, 8)


def _aggregate_observations(
    observations: list[InferenceObservation],
) -> dict[str, int | float | bool | None]:
    prompt = sum(item.usage.prompt_tokens for item in observations)
    completion = sum(item.usage.completion_tokens for item in observations)
    cache_complete = all(
        item.usage.cache_hit_tokens is not None and item.usage.cache_miss_tokens is not None
        for item in observations
    )
    hit = sum(item.usage.cache_hit_tokens or 0 for item in observations) if cache_complete else None
    miss = (
        sum(item.usage.cache_miss_tokens or 0 for item in observations) if cache_complete else None
    )
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "cache_hit_tokens": hit,
        "cache_miss_tokens": miss,
        "cache_metrics_complete": cache_complete,
        "inference_latency_ms": round(sum(item.usage.latency_ms for item in observations), 3),
    }


UNAVAILABLE = "UNAVAILABLE"


def _provider_call_records(
    observations: list[InferenceObservation],
    *,
    run_id: str,
    model: str,
    pricing: Pricing | None,
) -> list[dict[str, Any]]:
    """One record per provider call (Stage G3-2 observability contract).

    ``on_call`` fires inside the adapter, which has no agent identity, so ``worker_id`` and
    ``task_id`` are reported as ``UNAVAILABLE`` rather than guessed from a process-global value.
    Token, cache and latency fields are exactly what the provider returned for that call.
    """

    records: list[dict[str, Any]] = []
    for index, observation in enumerate(observations):
        usage = observation.usage
        hit = usage.cache_hit_tokens
        miss = usage.cache_miss_tokens
        cost = _estimate_cost([observation], pricing)
        records.append(
            {
                "run_id": run_id,
                "request_index": index,
                "worker_id": UNAVAILABLE,
                "task_id": UNAVAILABLE,
                "attribution": UNAVAILABLE,
                "model": model,
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "cache_hit_tokens": hit if hit is not None else UNAVAILABLE,
                "cache_miss_tokens": miss if miss is not None else UNAVAILABLE,
                "cache_hit_ratio": (
                    round(usage.cache_hit_ratio, 6)
                    if usage.cache_hit_ratio is not None
                    else UNAVAILABLE
                ),
                "cost_usd": cost if cost is not None else UNAVAILABLE,
                "latency_ms": round(usage.latency_ms, 3),
                "stable_prefix_hash": observation.stable_prefix_hash,
                "full_prompt_hash": observation.full_prompt_hash,
            }
        )
    return records


def _classify_exception(error: BaseException) -> FailureClass:
    if isinstance(error, EvaluationEnvironmentError):
        return "ENVIRONMENT_FAILURE"
    if isinstance(error, SandboxLostError):
        return "SANDBOX_FAILURE"
    if isinstance(error, TimeoutError):
        return "TIMEOUT"
    name = type(error).__name__
    module = type(error).__module__
    if "openai" in module or "API" in name or "RateLimit" in name or "Timeout" in name:
        return "MODEL_FAILURE"
    return "RUNTIME_FAILURE"


def _jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "model_dump"):
        return _jsonable(value.model_dump(mode="json"))
    if hasattr(value, "value") and not isinstance(value, (str, int, float, bool)):
        return _jsonable(value.value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(_jsonable(payload), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _collect_tool_calls(
    agent_results: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    workers: dict[str, Any] = {}
    for agent, result in agent_results.items():
        result_calls = [
            {
                "agent": agent,
                "tool_name": record.tool_name,
                "input": _jsonable(record.input),
                "output": str(record.output)[:4000],
                "duration_ms": round(float(record.duration) * 1000.0, 3),
            }
            for record in result.tool_calls
        ]
        calls.extend(result_calls)
        workers[agent] = {
            "success": bool(result.success),
            "output": str(result.output or ""),
            "stop_reason": _jsonable(result.stop_reason),
            "tool_calls": len(result_calls),
            "token_usage": _jsonable(result.token_usage),
        }
    return calls, workers


async def _run_mcp_setup(runtime: AtlasToolRuntime, task: BenchmarkTask) -> list[str]:
    if task.mcp_server is None:
        return []
    server_path = MCP_SERVER_DIR / f"{task.mcp_server}.py"
    if not server_path.is_file():
        raise EvaluationEnvironmentError(f"MCP server fixture is missing: {server_path}")
    config = MCPServerConfig(
        name=task.mcp_server,
        transport="stdio",
        command=sys.executable,
        args=[str(server_path)],
        timeout=30.0,
    )
    names = await runtime.add_mcp_server(config)
    return list(names)


async def run_sample(
    task: BenchmarkTask,
    *,
    settings: RunnerSettings,
    repetition_id: int,
    config_path: Path | None = None,
) -> EvalRunResult:
    """Execute exactly one benchmark sample with one fresh sandbox and run identity."""

    load_dotenv(REPO_ROOT / ".env", override=False)
    config_file = Path(config_path or settings.config_path)
    config = load_config(config_file)
    model = settings.model_override or config.provider.model
    started_at = datetime.now(UTC)
    started = time.perf_counter()
    # OpenSandbox metadata labels are limited to 63 characters; the durable run identity is the
    # short label while sample/repetition/config/fixture digests remain in the persisted record.
    run_id = f"eval-{uuid.uuid4().hex[:12]}"
    run_dir = (
        settings.runs_root / settings.benchmark_id / task.sample_id / f"r{repetition_id}-{run_id}"
    ).resolve()
    run_dir.mkdir(parents=True, exist_ok=False)
    fixture_dir = run_dir / "fixture"
    state_dir = run_dir / "state"
    state_dir.mkdir()
    materialize_fixture(task, fixture_dir)
    baseline_hashes = workspace_hashes(fixture_dir)
    # The commit message is part of the worker-visible workspace (``git log``), so it must not
    # advertise the benchmark sample id or that this is an evaluation fixture.
    stage_git_repo(fixture_dir, label=FIXTURE_COMMIT_LABEL)
    host_hashes_before = workspace_hashes(fixture_dir)

    identity = RunIdentity(
        benchmark_id=settings.benchmark_id,
        dataset_version=settings.dataset_version,
        scorer_version=settings.scorer_version,
        sample_id=task.sample_id,
        repetition_id=repetition_id,
        run_id=run_id,
        git_commit=git_commit(REPO_ROOT),
        git_dirty=git_dirty(REPO_ROOT),
        config_digest=settings.digest(),
        fixture_digest=task.fixture_digest,
        model=model,
        provider=config.provider.name,
        started_at=started_at,
    )

    observations: list[InferenceObservation] = []
    resolved_models: list[str] = []
    metrics = RuntimeMetrics()
    failure_class: FailureClass | None = None
    failure_reason: str | None = None
    final_output = ""
    agent_success = False
    tool_calls: list[dict[str, Any]] = []
    worker_results: dict[str, Any] = {}
    task_events: list[dict[str, Any]] = []
    team_lifecycle_events: list[Any] = []
    workspace_export: Path | None = None
    patch_digest: str | None = None
    oracle = None
    environment: dict[str, Any] = {
        "python": sys.version,
        "platform": platform.platform(),
        "config_path": str(config_file),
        "sandbox_image": config.sandbox.image,
        "sandbox_provider": config.sandbox.provider,
        "temperature": settings.temperature,
        "max_turns": settings.max_turns,
        "max_tokens": settings.max_tokens,
        "token_budget": settings.token_budget,
        "budget_mode": settings.budget_mode,
        "max_tool_calls": settings.max_tool_calls,
        "progressive_tools": settings.progressive_tools,
        "variant": settings.variant,
        "capture_evidence": settings.capture_evidence,
        "handoff_mode": settings.handoff_mode,
        "compaction_mode": settings.compaction_mode,
        "tool_error_mode": settings.tool_error_mode,
        "tool_result_mode": settings.tool_result_mode,
        "worker_contract_mode": settings.worker_contract_mode,
        "ownership_mode": settings.ownership_mode,
        "execution_integrity_mode": settings.execution_integrity_mode,
        "delegation_route_mode": settings.delegation_route_mode,
        "delegation_policy_version": settings.delegation_policy_version,
    }
    provider = None
    manager: ProjectSandboxManager | None = None
    engine: AtlasContextEngine | None = None
    idempotency: SQLiteToolIdempotencyStore | None = None
    runtime: AtlasToolRuntime | None = None
    handoff: HandoffRuntime | None = None
    compaction: HistoryCompactionRuntime | None = None
    ingress: ToolResultIngressRuntime | None = None
    recorder: TrajectoryRecorder | None = None
    budget_state: RunBudgetState | None = (
        RunBudgetState(limit_tokens=settings.token_budget)
        if settings.budget_mode == BUDGET_MODE_GUARDED and settings.token_budget > 0
        else None
    )
    binding = None
    workspace_task_id = f"{run_id}-workspace"
    failure_class: FailureClass | None = None
    failure_reason: str | None = None
    task_texts: dict[str, str] = {}
    contract_mode = WORKER_CONTRACT_LEGACY
    contract_overhead_chars = 0
    routing_record: dict[str, Any] | None = None
    cleanup_error: BaseException | None = None
    final_result: EvalRunResult | None = None
    try:
        if not os.environ.get("DEEPSEEK_API_KEY"):
            raise EvaluationEnvironmentError("DEEPSEEK_API_KEY is not configured")
        if config.sandbox.provider == "fake":
            raise EvaluationEnvironmentError("evaluation requires a real Atlas sandbox provider")
        provider = _build_provider(config)
        capabilities = provider.capabilities()
        environment["provider_is_real"] = is_real_sandbox_provider(
            provider, expected_name=config.sandbox.provider
        )
        environment["capabilities_digest"] = canonical_digest(capabilities.model_dump(mode="json"))
        require_capabilities(capabilities)
        health = await provider.health()
        environment["sandbox_health"] = health.status
        environment["sandbox_health_message"] = health.message
        if health.status == "unavailable":
            raise EvaluationEnvironmentError(f"sandbox provider is unavailable: {health.message}")

        provider_name = f"atlas-eval-{uuid.uuid4().hex[:12]}"

        async def factory(**_: Any) -> ModelRecordingAdapter:
            inner = build_deepseek_adapter(model, observations.append)
            return ModelRecordingAdapter(inner, resolved_models)

        register_provider_factory(provider_name, factory)

        project_config = _project_config(config)
        recorder = TrajectoryRecorder(
            FilesystemRunStore(run_dir / config.trajectory.store_root),
            run_id,
            agent_name="atlas-evaluation",
            model=model,
        )
        manager = ProjectSandboxManager(
            provider,
            project_config,
            run_id=run_id,
            binding_store=SQLiteSandboxBindingStore(state_dir / "sandbox_bindings.db"),
            ownership_root=state_dir,
            trajectory_recorder=recorder,
        )
        idempotency = SQLiteToolIdempotencyStore(state_dir / "idempotency.db")
        await idempotency.setup()
        ledger = SQLiteActionLedgerStore(state_dir / "ledger.db")
        sandbox_tools = SandboxWorkspaceTools(
            manager,
            workspace_task_id,
            provider,
            tool_error_mode=settings.tool_error_mode,
        )
        # Stage R1-1: the ownership manifest is built from benchmark metadata before any worker
        # runs, and the enforcer is installed as M3's external policy. In ``off`` both stay None,
        # so the gate is byte-identical to the pre-R1-1 configuration.
        ownership_mode = (
            settings.ownership_mode
            if settings.ownership_mode in OWNERSHIP_MODES
            else OWNERSHIP_MODE_OFF
        )
        integrity_mode = (
            settings.execution_integrity_mode
            if settings.execution_integrity_mode in EXECUTION_INTEGRITY_MODES
            else EXECUTION_INTEGRITY_OFF
        )
        ownership_manifest = None
        ownership_policy = None
        external_policy = None
        payload_corpus = (
            ToolPayloadCorpus.for_run(run_dir)
            if integrity_mode == EXECUTION_INTEGRITY_RECORD
            else None
        )
        if ownership_mode != OWNERSHIP_MODE_OFF:
            try:
                ownership_manifest = manifest_for_task(
                    task,
                    run_id=run_id,
                    workspace_root=project_config.workspace_root,
                )
            except OwnershipManifestError as error:
                # An ambiguous Host declaration is a configuration defect, not a model failure.
                raise EvaluationEnvironmentError(f"ownership manifest: {error}") from error
            external_policy, ownership_policy = build_ownership_enforcer(
                ownership_manifest, mode=ownership_mode
            )
            environment["ownership"] = manifest_public_summary(ownership_manifest)
            environment["ownership"]["declarations"] = [
                {
                    "worker": worker,
                    "task": ownership_manifest.task_of(worker),
                    "paths": list(ownership_manifest.declared_paths(worker)),
                }
                for worker in ownership_manifest.workers()
            ]
        # Stage R0: the delegation route decides whether the existing A0 (single worker) or A1
        # (candidate DAG) path runs. The routing decision is recorded either way; only
        # ``selective`` changes execution, and it changes nothing else.
        route_mode = (
            settings.delegation_route_mode
            if settings.delegation_route_mode in DELEGATION_ROUTE_MODES
            else DELEGATION_ROUTE_OFF
        )
        if route_mode != DELEGATION_ROUTE_OFF:
            routed = plan_route(
                task,
                capabilities_from_catalog(WorkerCatalog()),
                routing_policy(settings.delegation_policy_version),
            )
            routing_record = routed.as_dict()
            routing_record["mode"] = route_mode
            routing_record["candidate_worker_count"] = len(task.workers)
            if route_mode == DELEGATION_ROUTE_SELECTIVE and routed.is_direct:
                # The executed plan is the A0 plan, so the task is graded exactly like an A0
                # sample (single-worker mode, same oracle checks on artifacts and hidden tests).
                task = replace(
                    task,
                    mode="coding",
                    workers=a0_steps(task),
                    tool_expectations={
                        **task.tool_expectations,
                        "distinct_workers": 1,
                    },
                )
                routing_record["executed_worker_count"] = 1
            else:
                routing_record["executed_worker_count"] = len(task.workers)
            routing_record["route_digest"] = decision_digest(routed.decision)
        title_by_id = {step.task_id: f"{task.sample_id}-{step.task_id}" for step in task.workers}
        # Stage G1: one handoff runtime per run. The trusted task identity of every consumer is
        # the runtime task id the worker was created with, never a process-global variable.
        handoff = HandoffRuntime.create(
            run_dir=run_dir,
            run_id=run_id,
            nodes=[
                HandoffNode(
                    key=step.task_id,
                    title=title_by_id[step.task_id],
                    depends_on=tuple(step.depends_on),
                )
                for step in task.workers
            ],
            consumer_keys={f"{run_id}-{step.task_id}": step.task_id for step in task.workers},
            mode="dual" if settings.handoff_mode == "dual" else "legacy",
        )
        # Stage G2b-1: one compaction runtime per run. Created in both arms so the tool registry
        # and therefore the model-visible tool schema are identical in C0 and C1.
        compaction_mode = (
            settings.compaction_mode
            if settings.compaction_mode in COMPACTION_MODES
            else COMPACTION_OFF
        )
        compaction = HistoryCompactionRuntime.create(
            run_dir=run_dir, run_id=run_id, mode=compaction_mode
        )
        # Stage G3-2: ingress offloading reuses the same HistoryStore and the same
        # ``history.read`` tool, so the model-visible tool surface is identical in both arms.
        tool_result_mode = (
            settings.tool_result_mode
            if settings.tool_result_mode in TOOL_RESULT_MODES
            else TOOL_RESULT_OFF
        )
        ingress = ToolResultIngressRuntime.wrap(
            store=compaction.store,
            mode=tool_result_mode,
        )
        runtime_type = AtlasToolRuntime if settings.progressive_tools else AllToolsAtlasRuntime
        runtime = runtime_type(
            run_id,
            state_path=run_dir / "tool_state.json",
            conversation_path=run_dir / "tool_conversations.json",
            external_tools=(*handoff.external_tools(), *compaction.external_tools()),
            search_backend=config.tools.search_backend,
            embedding_model=config.tools.embedding_model,
            max_results=config.tools.max_results,
            action_policy=_coding_policy(),
            action_catalog=_EVAL_CATALOG,
            action_external_policy=external_policy,
            action_ledger=ledger,
            idempotency_store=idempotency,
            sandbox_tools=sandbox_tools,
            trajectory_recorder=recorder,
            runtime_instance_id=f"eval-{run_id}",
            tool_result_offloader=ingress,
            execution_integrity_mode=integrity_mode,
            tool_payload_corpus=payload_corpus,
            external_denial_retry_safe=ownership_mode != OWNERSHIP_MODE_OFF,
        )
        await _run_mcp_setup(runtime, task)

        parent = ExecutionContext(
            principal="user:evaluation",
            tenant_scope="atlas-evaluation",
            classification="internal",
            attributes={
                "run_id": run_id,
                "sample_id": task.sample_id,
                "benchmark_id": settings.benchmark_id,
            },
        )
        worker_factory = ScopedWorkerFactory(
            model=model,
            provider=provider_name,
            store=WorkerStore(run_dir / "workers.json"),
            trajectory_recorder=recorder,
        )
        environment_note = _environment_note(project_config.workspace_root)
        worker_by_task: dict[str, Any] = {}
        for step in task.workers:
            worker = worker_factory.create(
                parent_context=parent,
                parent_agent="atlas-evaluation",
                assignment=WorkerAssignment(
                    profile_id="coding_worker",
                    task=f"{environment_note}\n\n{step.prompt}",
                ),
                worker_id=f"{run_id}-{step.task_id}",
                task_id=f"{run_id}-{step.task_id}",
            )
            worker_by_task[step.task_id] = worker

        provision_started = time.perf_counter()
        first_worker = worker_by_task[task.workers[0].task_id]
        binding = await manager.ensure_binding(
            task_id=workspace_task_id,
            source_repo=str(fixture_dir),
            context=first_worker.execution_context,
            worker_id=first_worker.worker_id,
        )
        metrics.provision_latency_ms = round((time.perf_counter() - provision_started) * 1000.0, 3)
        metrics.sandbox_created = manager.create_count > 0
        # Stage G3-4a: drop the provisioning staging bundle and prove that no private artifact
        # name is reachable from inside the worker sandbox.
        isolation = await enforce_evaluation_isolation(provider, binding.sandbox_handle)
        metrics.extra["integrity"] = isolation.as_dict()
        if not isolation.passed:
            # No worker turn may run in an environment that still exposes private artifacts.
            raise EvaluationEnvironmentError(
                "evaluation isolation check failed: " + "; ".join(isolation.failures)
            )

        def on_progress(event: Any) -> None:
            if settings.capture_evidence:
                # Stage R0.5: task/agent identity is recorded for *every* event so a start can be
                # paired with its own completion by runtime task id. This is measurement wiring
                # only: it observes the scheduler, it never influences it.
                recorded = {
                    "ts": datetime.now(UTC).isoformat(),
                    "type": str(event.type),
                    "task": str(getattr(event, "task", "") or ""),
                    "agent": str(getattr(event, "agent", "") or ""),
                }
                data = getattr(event, "data", None)
                if data is not None:
                    recorded["title"] = getattr(data, "title", None)
                    recorded["assignee"] = getattr(data, "assignee", None)
                if event.type == "task_complete":
                    recorded["task_status"] = str(getattr(event, "status", "") or "")
                task_events.append(recorded)
            if event.type == "task_start" and getattr(event, "data", None) is not None:
                title = getattr(event.data, "title", None)
                for task_id, expected_title in title_by_id.items():
                    if title == expected_title:
                        set_current_task_id(task_id)
                        break
            elif event.type == "task_complete":
                set_current_task_id(None)

        engine = AtlasContextEngine(
            OrchestratorConfig(on_progress=on_progress),
            tool_runtime=runtime,
            prompt_builder=handoff.build_prompt,
            budget_state=budget_state,
            compaction=compaction,
        )
        agent_configs = [
            worker_by_task[step.task_id].agent_config.model_copy(
                update={
                    "provider": provider_name,
                    "max_turns": step.max_turns or settings.max_turns,
                    "max_tokens": settings.max_tokens,
                    "temperature": settings.temperature,
                    "provider_resilience": evaluation_resilience(),
                    "tool_security": ToolSecurityPolicy(
                        workspace_root=project_config.workspace_root
                    ),
                }
            )
            for step in task.workers
        ]
        team = engine.create_team(
            "evaluation",
            TeamConfig(name="evaluation", agents=agent_configs),
        )
        # Stage G3-3: only the worker's task turn changes. The assignment (system prompt), the
        # tool list, the DAG and the budget are identical in both contract modes.
        contract_mode = (
            settings.worker_contract_mode
            if settings.worker_contract_mode in WORKER_CONTRACT_MODES
            else WORKER_CONTRACT_LEGACY
        )
        task_texts = {
            step.task_id: worker_task_text(
                step, steps=task.workers, sample_id=task.sample_id, mode=contract_mode
            )
            for step in task.workers
        }
        contract_overhead_chars = sum(
            contract_overhead(step.prompt, task_texts[step.task_id]) for step in task.workers
        )
        task_specs = [
            TaskSpec(
                title=title_by_id[step.task_id],
                description=task_texts[step.task_id],
                assignee=worker_by_task[step.task_id].worker_name,
                depends_on=[title_by_id[item] for item in step.depends_on],
            )
            for step in task.workers
        ]
        agent_started = time.perf_counter()
        result = await asyncio.wait_for(
            engine.run_tasks(team, task_specs),
            timeout=settings.timeout_seconds,
        )
        environment["agent_latency_ms"] = round((time.perf_counter() - agent_started) * 1000.0, 3)
        agent_success = bool(result.success)
        team_lifecycle_events = _jsonable(getattr(result, "lifecycle_events", []))
        tool_calls, worker_results = _collect_tool_calls(result.agent_results)
        final_output = "\n".join(
            str(item.get("output", "")) for item in worker_results.values() if item.get("output")
        )

        patch = await export_patch(
            provider,
            binding.sandbox_handle,
            base_commit=binding.source_commit,
            workspace_root=binding.workspace_root,
        )
        patch_digest = patch.patch_digest
        (run_dir / "workspace.patch").write_text(patch.patch_text, encoding="utf-8")
        workspace_export = run_dir / "workspace"
        await export_workspace(
            provider,
            binding.sandbox_handle,
            destination=workspace_export,
            workspace_root=binding.workspace_root,
        )
    except TimeoutError as error:
        failure_class = "TIMEOUT"
        failure_reason = f"sample exceeded {settings.timeout_seconds}s"
        del error
    except BaseException as error:  # noqa: BLE001 - every failure is classified and persisted
        if not isinstance(error, Exception):
            raise
        failure_class = _classify_exception(error)
        failure_reason = f"{type(error).__name__}: {error}"
    finally:
        if runtime is not None:
            with contextlib.suppress(BaseException):
                await runtime.close()
        if engine is not None:
            with contextlib.suppress(Exception):
                await engine.close()
        if idempotency is not None:
            with contextlib.suppress(Exception):
                await idempotency.teardown()
        if manager is not None:
            try:
                if binding is not None:
                    if settings.retain_sandbox_on_failure and failure_class is not None:
                        metrics.sandbox_destroyed = False
                    else:
                        managed = await manager.close(workspace_task_id)
                        metrics.sandbox_destroyed = bool(managed)
                        if not managed:
                            cleanup_error = RuntimeError("provider did not confirm destroy")
                else:
                    metrics.sandbox_destroyed = True
            except BaseException as error:
                cleanup_error = error
                metrics.sandbox_destroyed = False
            if cleanup_error is not None and failure_class is None:
                failure_class = "CLEANUP_FAILURE"
                failure_reason = f"sandbox cleanup failed: {cleanup_error}"
            elif cleanup_error is not None and failure_reason is not None:
                failure_reason = f"{failure_reason}; sandbox cleanup also failed: {cleanup_error}"

        host_hashes_after = workspace_hashes(fixture_dir)
        host_unchanged = host_hashes_before == host_hashes_after
        if not host_unchanged and failure_class is None:
            failure_class = "HOST_ISOLATION_VIOLATION"
            failure_reason = "Host fixture changed during the run"
        if runtime is not None:
            with contextlib.suppress(Exception):
                tool_metrics = runtime.metrics()
                environment["tool_runtime_metrics"] = (
                    tool_metrics.model_dump(mode="json")
                    if hasattr(tool_metrics, "model_dump")
                    else str(tool_metrics)
                )
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
        environment["resolved_models"] = sorted(set(resolved_models))
        environment["requested_model"] = model
        environment["pricing_basis"] = (
            "DeepSeek Flash peak rates snapshot 2026-11-06; deepseek-chat resolves to "
            "deepseek-flash; upper-bound estimate"
        )
        metrics.extra["provider_name"] = config.sandbox.provider
        metrics.extra["sandbox_health"] = environment.get("sandbox_health")
        if handoff is not None:
            metrics.extra["handoff"] = handoff.metrics.as_dict()
        if compaction is not None:
            metrics.extra["compaction"] = compaction.metrics_dict()
        if ingress is not None:
            metrics.extra["tool_result"] = ingress.metrics_dict()
        # Stage R1-1: ownership evidence is measurement only. It never feeds the oracle.
        if ownership_policy is not None:
            metrics.extra["ownership"] = ownership_policy.evidence_snapshot()
        if recorder is not None and integrity_mode == EXECUTION_INTEGRITY_RECORD:
            with contextlib.suppress(Exception):
                recorder.record_run_terminal(
                    status=failure_class or "SUCCESS",
                    task_count=len(task.workers),
                    final_output_digest=canonical_digest({"output": final_output}),
                    final_output_size=len(final_output.encode("utf-8")),
                )
        if integrity_mode == EXECUTION_INTEGRITY_RECORD or ownership_mode != OWNERSHIP_MODE_OFF:
            trace = summarize_trace(
                run_dir,
                recorder_failures=recorder.recording_failures if recorder is not None else 0,
            )
            trace["result_comparison"] = compare_with_result(trace, tool_calls)
            trace["integrity_mode"] = integrity_mode
            trace["ownership_mode"] = ownership_mode
            metrics.extra["trace"] = trace
            environment["trace_status"] = trace["status"]
        task_text_digest = hashlib.sha256(
            "\n".join(f"{step_id}:{text}" for step_id, text in sorted(task_texts.items())).encode(
                "utf-8"
            )
        ).hexdigest()
        if routing_record is not None:
            metrics.extra["routing"] = routing_record
        metrics.extra["worker_contract"] = {
            "mode": contract_mode,
            "overhead_chars": contract_overhead_chars,
            "task_text_digest": task_text_digest,
            "workers": [
                {
                    "task_id": step.task_id,
                    "task_text_chars": len(task_texts.get(step.task_id, step.prompt)),
                    "overhead_chars": contract_overhead(
                        step.prompt, task_texts.get(step.task_id, step.prompt)
                    ),
                }
                for step in task.workers
            ],
        }
        call_records = _provider_call_records(
            observations, run_id=run_id, model=model, pricing=config.pricing
        )
        (run_dir / "provider_calls.jsonl").write_text(
            "".join(json.dumps(record, sort_keys=True) + "\n" for record in call_records),
            encoding="utf-8",
        )
        metrics.extra["provider_calls"] = len(call_records)
        metrics.tool_search_calls = sum(
            1 for item in tool_calls if item["tool_name"] in ("tool.search", "tool_search")
        )
        metrics.dispatch_calls = sum(
            1 for item in tool_calls if item["tool_name"] == "dispatch_action"
        )
        unknown_tools = [
            item
            for item in tool_calls
            if item["tool_name"] not in _SANDBOX_TOOLS
            and item["tool_name"] not in _CONTROL_TOOLS
            and not str(item["tool_name"]).startswith("mcp_")
        ]
        metrics.extra["unexpected_native_tools"] = len(unknown_tools)
        metrics.host_isolation_violations = 0 if host_unchanged else 1
        metrics.undisclosed_tool_rejections = sum(
            str(item.get("output", "")).count("tool_not_disclosed") for item in tool_calls
        )
        metrics.worker_selection = sorted(worker_results)
        budget_snapshot = budget_state.finalize() if budget_state is not None else None
        if budget_snapshot is not None:
            metrics.extra["budget"] = budget_snapshot
            if failure_class is None and budget_state is not None:
                rejections = int(budget_snapshot["admission_rejections"])
                if rejections:
                    failure_class = "BUDGET_EXCEEDED"
                    failure_reason = (
                        f"run budget guard rejected {rejections} LLM request(s): "
                        f"{budget_snapshot['last_rejection']}"
                    )
        if (
            failure_class is None
            and metrics.prompt_tokens is not None
            and metrics.completion_tokens is not None
            and settings.token_budget > 0
            and metrics.prompt_tokens + metrics.completion_tokens > settings.token_budget
        ):
            failure_class = "BUDGET_EXCEEDED"
            failure_reason = (
                f"token usage exceeded budget: "
                f"{metrics.prompt_tokens + metrics.completion_tokens} > {settings.token_budget}"
            )
        if failure_class is None and metrics.tool_calls > settings.max_tool_calls:
            failure_class = "BUDGET_EXCEEDED"
            failure_reason = (
                f"tool call budget exceeded: {metrics.tool_calls} > {settings.max_tool_calls}"
            )

        if workspace_export is not None and workspace_export.is_dir():
            oracle_outcome = evaluate_oracle(
                task,
                workspace=workspace_export,
                baseline_hashes=baseline_hashes,
                tool_calls=tool_calls,
                worker_results=worker_results,
                run_dir=run_dir,
                host_fixture_unchanged=host_unchanged,
            )
        else:
            from experiments.evaluation.models import OracleOutcome

            oracle_outcome = OracleOutcome(
                passed=False,
                checks={"workspace_exported": False},
                failure_reasons=["sandbox workspace was not exported"],
            )
        oracle = oracle_outcome
        if failure_class is None and not oracle.passed:
            failure_class = "ORACLE_FAILURE"
            failure_reason = "; ".join(oracle.failure_reasons)[:2000] or "oracle failed"
        if failure_class is None:
            failure_class = "TASK_SUCCESS"
        success = failure_class == "TASK_SUCCESS" and oracle.passed

        finished_at = datetime.now(UTC)
        metrics.latency_ms = round((time.perf_counter() - started) * 1000.0, 3)
        result = EvalRunResult(
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
            workspace_export=str(workspace_export) if workspace_export else None,
            patch_digest=patch_digest,
            started_at=started_at,
            finished_at=finished_at,
            environment=environment,
            safety={
                "host_fixture_unchanged": host_unchanged,
                "sandbox_destroyed": metrics.sandbox_destroyed,
                "cleanup_error": str(cleanup_error) if cleanup_error else None,
                "unexpected_native_tools": len(unknown_tools),
                "real_sandbox_provider": environment.get("provider_is_real", False),
            },
        )
        _write_json(run_dir / "result.json", result.as_dict())
        _write_json(
            run_dir / "run.json",
            {
                "identity": identity.as_dict(),
                "task": task.public_metadata(),
                "environment": environment,
                "config_digest": settings.digest(),
                "fixture_digest": task.fixture_digest,
                "workspace_digest": workspace_digest(fixture_dir),
            },
        )
        if settings.capture_evidence:
            _write_json(
                run_dir / "stage_e_evidence.json",
                {
                    "identity": identity.as_dict(),
                    "task": {
                        **task.public_metadata(),
                        "workers": [
                            {
                                "task_id": step.task_id,
                                "title": step.title,
                                "depends_on": list(step.depends_on),
                            }
                            for step in task.workers
                        ],
                    },
                    "task_events": task_events,
                    "anycode_lifecycle_events": team_lifecycle_events,
                    "worker_results": worker_results,
                    "tool_calls": tool_calls,
                    "handoff": metrics.extra.get("handoff"),
                },
            )
        ledger_path = settings.runs_root / settings.benchmark_id / "runs.jsonl"
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with ledger_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(result.as_dict(), sort_keys=True) + "\n")
        final_result = result

    if final_result is None:
        raise RuntimeError("evaluation run finished without a persisted result")
    return final_result
