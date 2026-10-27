"""V2 M2 scoped-worker models. No capability/grant semantics live here."""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime
from typing import Literal

from anycode.identity.context import ExecutionContext
from anycode.types import AgentConfig, AgentInfo, ToolSecurityPolicy, ToolUseContext
from pydantic import BaseModel, ConfigDict, Field, field_validator

COARSE_SCOPES = frozenset({"research", "coding", "data", "diagnostics", "communication", "general"})
_DELEGATE_TOOLS = frozenset({"delegate", "delegate_capability", "delegate_worker"})
_PROFILE_ID = r"^[a-z][a-z0-9_]*$"


def _unique_names(values: Iterable[str]) -> tuple[str, ...]:
    names = tuple(values)
    if len(set(names)) != len(names):
        raise ValueError("base_tools must not contain duplicates")
    return tuple(sorted(names))


class WorkerProfile(BaseModel):
    """Host-owned immutable worker template."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    profile_id: str = Field(min_length=1, max_length=64, pattern=_PROFILE_ID)
    role: str = Field(min_length=1, max_length=200)
    instructions: str = Field(min_length=1, max_length=4000)
    base_tools: tuple[str, ...] = ()
    operating_scope: tuple[str, ...] = ()
    max_turns: int = Field(default=6, ge=1, le=20)
    can_delegate: bool = False

    @field_validator("base_tools")
    @classmethod
    def _tools(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        names = _unique_names(value)
        if any(name in _DELEGATE_TOOLS for name in names):
            raise ValueError("delegate tools are not allowed in a worker base profile")
        return names

    @field_validator("operating_scope")
    @classmethod
    def _scopes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("operating_scope must contain at least one coarse scope")
        if len(set(value)) != len(value):
            raise ValueError("operating_scope must not contain duplicates")
        invalid = set(value) - COARSE_SCOPES
        if invalid:
            raise ValueError(f"invalid coarse operating scope(s): {sorted(invalid)}")
        return value


class WorkerAssignment(BaseModel):
    """Planner-supplied untrusted assignment. No trusted fields are accepted."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    profile_id: str = Field(min_length=1, max_length=64)
    task: str = Field(min_length=1, max_length=8000)
    context: str | None = Field(default=None, max_length=16000)


class WorkerTaskContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    objective: str
    background: str | None = None


class WorkerRuntimeMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    worker_id: str
    profile_id: str
    parent_agent: str
    task_id: str
    operating_scope: tuple[str, ...]
    can_delegate: bool

    def nested_attributes(self) -> dict[str, object]:
        return {
            "worker_id": self.worker_id,
            "profile_id": self.profile_id,
            "parent_agent": self.parent_agent,
            "task_id": self.task_id,
            "operating_scope": list(self.operating_scope),
            "can_delegate": self.can_delegate,
        }


class WorkerInstanceRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    worker_id: str
    worker_name: str
    profile_id: str
    task_id: str
    parent_agent: str
    assignment: WorkerAssignment
    task_context: WorkerTaskContext
    execution_context: ExecutionContext
    rendered_context_hash: str
    created_at: datetime
    status: Literal["ready"] = "ready"


class WorkerInstance(BaseModel):
    """A built worker descriptor; the actual Agent is built from its AgentConfig."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    record: WorkerInstanceRecord
    profile: WorkerProfile
    agent_config: AgentConfig
    rendered_context: str
    resumed: bool = False

    @property
    def worker_id(self) -> str:
        return self.record.worker_id

    @property
    def execution_context(self) -> ExecutionContext:
        return self.record.execution_context

    @property
    def worker_name(self) -> str:
        return self.record.worker_name

    @property
    def context_hash(self) -> str:
        return self.record.rendered_context_hash

    def tool_context(self, *, security_policy: ToolSecurityPolicy | None = None) -> ToolUseContext:
        """Build the exact AnyCode ToolUseContext M3 must receive."""

        config = self.agent_config
        return ToolUseContext(
            agent=AgentInfo(
                name=self.worker_name,
                role=self.profile.role,
                model=config.model,
            ),
            execution_context=self.execution_context,
            security_policy=security_policy,
        )

    def instantiate(self, tool_registry, tool_executor):
        """Build the real AnyCode Agent over the caller-supplied M1 registry/executor."""

        from anycode.core.agent import Agent

        return Agent(self.agent_config, tool_registry, tool_executor)


class WorkerConfigurationError(ValueError):
    pass


def canonical_worker_context_hash(rendered: str) -> str:
    import hashlib

    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def safe_parent_attributes(attributes: dict[str, object]) -> dict[str, object]:
    """Copy only JSON-safe parent metadata; worker/task/capability keys are replaced."""

    reserved = {
        "atlas_worker",
        "worker_id",
        "profile_id",
        "worker_profile",
        "operating_scope",
        "task_id",
        "parent_agent",
        "worker_name",
        "delegation",
        "task",
        "objective",
        "background",
    }
    safe: dict[str, object] = {}
    for key, value in attributes.items():
        if key in reserved:
            continue
        try:
            json.dumps(value, allow_nan=False)
        except (TypeError, ValueError):
            continue
        safe[str(key)] = value
    return safe
