"""Declarative benchmark task and run-result models for the evaluation layer."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

EvalCategory = Literal[
    "bugfix",
    "feature",
    "tool_discovery",
    "multi_worker",
    "long_context",
    "recovery",
    "safety",
]
EvalMode = Literal["coding", "multi_worker", "tool_discovery", "long_context"]
FailureClass = Literal[
    "TASK_SUCCESS",
    "ORACLE_FAILURE",
    "MODEL_FAILURE",
    "RUNTIME_FAILURE",
    "ENVIRONMENT_FAILURE",
    "SANDBOX_FAILURE",
    "TIMEOUT",
    "BUDGET_EXCEEDED",
    "HOST_ISOLATION_VIOLATION",
    "FORBIDDEN_WORKSPACE_CHANGE",
    "CLEANUP_FAILURE",
    "NOT_EVALUATED",
]


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def canonical_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def text_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class WorkerStep:
    """One worker/task node in an evaluation workflow."""

    task_id: str
    title: str
    prompt: str
    depends_on: tuple[str, ...] = ()
    max_turns: int | None = None


@dataclass(frozen=True)
class BenchmarkTask:
    """A benchmark sample with a visible fixture and an independent hidden oracle.

    ``visible_files`` are staged into the sandbox repository. ``hidden_files`` are written only
    into the external oracle workspace, never into the sandbox. ``fixture_digest`` binds both.
    """

    sample_id: str
    category: EvalCategory
    mode: EvalMode
    prompt: str
    target: str
    visible_files: dict[str, str]
    hidden_files: dict[str, str] = field(default_factory=dict)
    workers: tuple[WorkerStep, ...] = ()
    allowed_paths: tuple[str, ...] = ()
    forbidden_paths: tuple[str, ...] = ("tests/**", "TASK.md")
    required_artifacts: tuple[str, ...] = ()
    tool_expectations: dict[str, Any] = field(default_factory=dict)
    mcp_server: str | None = None
    max_turns: int = 14
    tags: tuple[str, ...] = ()
    notes: str = ""
    #: Stage R0.5: public interface documents that are frozen *before* workers start, keyed by
    #: worker task id. The routing adapter verifies each path against ``visible_files``; a declared
    #: path that is not staged is recorded as UNKNOWN evidence, never as a trusted contract.
    frozen_contracts: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.sample_id.strip():
            raise ValueError("sample_id must not be empty")
        if self.mode == "multi_worker" and len(self.workers) < 2:
            raise ValueError(f"{self.sample_id}: multi_worker requires at least two workers")
        if self.mode != "multi_worker" and len(self.workers) != 1:
            raise ValueError(f"{self.sample_id}: non-multi-worker tasks require exactly one worker")
        task_ids = {worker.task_id for worker in self.workers}
        if len(task_ids) != len(self.workers):
            raise ValueError(f"{self.sample_id}: duplicate worker task ids")
        for worker in self.workers:
            missing = set(worker.depends_on) - task_ids
            if missing:
                raise ValueError(f"{self.sample_id}: unknown dependencies {sorted(missing)}")

    @property
    def fixture_digest(self) -> str:
        payload: dict[str, Any] = {
            "sample_id": self.sample_id,
            "category": self.category,
            "mode": self.mode,
            "prompt": self.prompt,
            "target": self.target,
            "visible_files": self.visible_files,
            "hidden_files": self.hidden_files,
            "workers": [
                {
                    "task_id": worker.task_id,
                    "title": worker.title,
                    "prompt": worker.prompt,
                    "depends_on": worker.depends_on,
                    "max_turns": worker.max_turns,
                }
                for worker in self.workers
            ],
            "allowed_paths": self.allowed_paths,
            "forbidden_paths": self.forbidden_paths,
            "required_artifacts": self.required_artifacts,
            "tool_expectations": self.tool_expectations,
            "mcp_server": self.mcp_server,
            "max_turns": self.max_turns,
        }
        # Only present when declared, so tasks without a frozen contract keep their digest.
        if self.frozen_contracts:
            payload["frozen_contracts"] = {
                key: list(value) for key, value in sorted(self.frozen_contracts.items())
            }
        return canonical_digest(payload)

    def public_metadata(self) -> dict[str, Any]:
        return {
            "sample_id": self.sample_id,
            "category": self.category,
            "mode": self.mode,
            "fixture_digest": self.fixture_digest,
            "workers": [worker.task_id for worker in self.workers],
            "tags": list(self.tags),
        }


@dataclass(frozen=True)
class RunIdentity:
    benchmark_id: str
    dataset_version: str
    scorer_version: str
    sample_id: str
    repetition_id: int
    run_id: str
    git_commit: str | None
    git_dirty: bool
    config_digest: str
    fixture_digest: str
    model: str
    provider: str
    started_at: datetime

    def as_dict(self) -> dict[str, Any]:
        return {
            "benchmark_id": self.benchmark_id,
            "dataset_version": self.dataset_version,
            "scorer_version": self.scorer_version,
            "sample_id": self.sample_id,
            "repetition_id": self.repetition_id,
            "run_id": self.run_id,
            "git_commit": self.git_commit,
            "git_dirty": self.git_dirty,
            "config_digest": self.config_digest,
            "fixture_digest": self.fixture_digest,
            "model": self.model,
            "provider": self.provider,
            "started_at": self.started_at.isoformat(),
        }


@dataclass
class OracleOutcome:
    passed: bool
    checks: dict[str, bool] = field(default_factory=dict)
    details: dict[str, Any] = field(default_factory=dict)
    failure_reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "checks": self.checks,
            "details": self.details,
            "failure_reasons": self.failure_reasons,
        }


@dataclass
class RuntimeMetrics:
    model_turns: int = 0
    tool_calls: int = 0
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cache_hit_tokens: int | None = None
    cache_miss_tokens: int | None = None
    latency_ms: float | None = None
    provision_latency_ms: float | None = None
    reattach_latency_ms: float | None = None
    estimated_cost_usd: float | None = None
    sandbox_created: bool = False
    sandbox_destroyed: bool = False
    host_isolation_violations: int = 0
    tool_search_calls: int = 0
    dispatch_calls: int = 0
    undisclosed_tool_rejections: int = 0
    worker_selection: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "model_turns": self.model_turns,
            "tool_calls": self.tool_calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cache_hit_tokens": self.cache_hit_tokens,
            "cache_miss_tokens": self.cache_miss_tokens,
            "latency_ms": self.latency_ms,
            "provision_latency_ms": self.provision_latency_ms,
            "reattach_latency_ms": self.reattach_latency_ms,
            "estimated_cost_usd": self.estimated_cost_usd,
            "sandbox_created": self.sandbox_created,
            "sandbox_destroyed": self.sandbox_destroyed,
            "host_isolation_violations": self.host_isolation_violations,
            "tool_search_calls": self.tool_search_calls,
            "dispatch_calls": self.dispatch_calls,
            "undisclosed_tool_rejections": self.undisclosed_tool_rejections,
            "worker_selection": self.worker_selection,
            **self.extra,
        }


@dataclass
class EvalRunResult:
    identity: RunIdentity
    task: BenchmarkTask
    success: bool
    failure_class: FailureClass
    failure_reason: str | None
    final_output: str
    agent_success: bool
    tool_calls: list[dict[str, Any]]
    worker_results: dict[str, Any]
    oracle: OracleOutcome
    metrics: RuntimeMetrics
    run_dir: str
    fixture_dir: str
    workspace_export: str | None
    patch_digest: str | None
    started_at: datetime
    finished_at: datetime
    environment: dict[str, Any] = field(default_factory=dict)
    safety: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "identity": self.identity.as_dict(),
            "sample_id": self.task.sample_id,
            "category": self.task.category,
            "mode": self.task.mode,
            "task": {
                **self.task.public_metadata(),
                "tool_expectations": self.task.tool_expectations,
                "allowed_paths": list(self.task.allowed_paths),
                "required_artifacts": list(self.task.required_artifacts),
            },
            "success": self.success,
            "failure_class": self.failure_class,
            "failure_reason": self.failure_reason,
            "final_output": self.final_output,
            "agent_success": self.agent_success,
            "tool_calls": self.tool_calls,
            "worker_results": self.worker_results,
            "oracle": self.oracle.as_dict(),
            "metrics": self.metrics.as_dict(),
            "run_dir": self.run_dir,
            "fixture_dir": self.fixture_dir,
            "workspace_export": self.workspace_export,
            "patch_digest": self.patch_digest,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "environment": self.environment,
            "safety": self.safety,
        }
