"""Trusted M5 project-sandbox binding models."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from anycode.sandbox import SandboxHandle, SandboxSpec
from pydantic import BaseModel, ConfigDict, Field

BindingState = Literal["PROVISIONING", "READY", "LOST", "CLOSED"]
ExecutionMode = Literal["HOST", "SANDBOX"]


class ProjectSandboxBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    task_id: str
    worker_id: str | None = None
    provider_name: str
    sandbox_handle: SandboxHandle
    workspace_root: str = "/workspace/project"
    source_commit: str
    source_digest: str
    bundle_digest: str
    state: BindingState = "PROVISIONING"
    created_at: datetime


class SandboxProjectConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_name: str
    image: str | None = None
    snapshot: str | None = None
    workspace_root: str = "/workspace/project"
    max_bundle_bytes: int = Field(default=64 * 1024 * 1024, ge=1024)
    network: Literal["none"] = "none"
    secret_references: dict[str, str] = Field(default_factory=dict)

    def validate_v1(self) -> None:
        if self.network != "none":
            raise ValueError("M5 v1 requires network='none'")
        if self.secret_references:
            raise ValueError("M5 v1 requires secret_references={}")
        if self.workspace_root != "/workspace/project":
            raise ValueError("M5 v1 workspace_root must be '/workspace/project'")
        if bool(self.image) == bool(self.snapshot):
            raise ValueError("M5 v1 requires exactly one of image or snapshot")
        selected = (self.image or self.snapshot or "").strip()
        if not selected or selected.lower() in {"latest", "stable", "lts"}:
            raise ValueError("M5 v1 requires a pinned sandbox image or snapshot")


class SandboxBindingError(RuntimeError):
    pass


class SandboxLostError(SandboxBindingError):
    pass


class SandboxProviderError(SandboxBindingError):
    """The selected provider failed before/while creating a sandbox."""


class SandboxProvisionError(SandboxBindingError):
    pass


class SandboxCapabilityError(SandboxBindingError):
    pass


class SandboxCredentialError(SandboxCapabilityError):
    """Trusted host credentials are missing; Atlas must not fall back to a fake provider."""


class SandboxFileTransferError(SandboxBindingError):
    pass


class SandboxExecutionError(SandboxBindingError):
    pass


class SandboxIsolationError(SandboxBindingError):
    pass


def trusted_sandbox_spec(
    *,
    run_id: str,
    task_id: str,
    worker_id: str | None,
    context,
    config: SandboxProjectConfig,
) -> SandboxSpec:
    """Build the sandbox spec exclusively from trusted deployment/task config."""

    config.validate_v1()
    labels = {"atlas_run_id": run_id, "atlas_task_id": task_id, "atlas_m5": "1"}
    if worker_id:
        labels["atlas_worker_id"] = worker_id
    return SandboxSpec(
        run_id=run_id,
        task_id=task_id,
        correlation_id=f"{run_id}:{task_id}",
        context=context,
        image=config.image,
        snapshot=config.snapshot,
        network="none",
        persistent=True,
        secret_references=dict(config.secret_references),
        labels=labels,
    )
