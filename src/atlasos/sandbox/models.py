"""Trusted M5 project-sandbox binding models."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Literal

from anycode.sandbox import SandboxHandle, SandboxSpec
from pydantic import BaseModel, ConfigDict, Field

BindingState = Literal["PROVISIONING", "READY", "LOST", "CLOSED"]
DurableBindingState = Literal["READY", "LOST", "CLOSED"]
ExecutionMode = Literal["HOST", "SANDBOX"]
SANDBOX_CONTINUITY_PROTOCOL = "atlas/m5-sandbox-continuity/v1"


class ProjectSandboxBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    binding_id: str = Field(min_length=1)
    run_id: str
    task_id: str
    worker_id: str | None = None
    provider_name: str
    sandbox_handle: SandboxHandle
    workspace_root: str = "/workspace/project"
    config_digest: str = Field(min_length=1)
    capabilities_digest: str = Field(min_length=1)
    source_commit: str
    source_digest: str
    bundle_digest: str
    state: BindingState = "PROVISIONING"
    created_at: datetime


class DurableSandboxBindingRecord(BaseModel):
    """Durable M5 relationship truth; never contains a live SDK object."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1)
    binding_id: str = Field(min_length=1)
    run_id: str
    task_id: str
    worker_id: str | None = None
    provider_name: str
    sandbox_id: str = Field(min_length=1)
    workspace_root: str
    config_digest: str
    capabilities_digest: str
    source_commit: str
    source_digest: str
    bundle_digest: str
    state: DurableBindingState
    created_at: datetime
    updated_at: datetime
    revision: int = Field(default=1, ge=1)
    provider_control_plane_digest: str | None = None
    provider_created_at: datetime | None = None
    provider_metadata_digest: str | None = None


class SandboxContinuityProjection(BaseModel):
    """Stable identity consumed by M6; excludes runtime/state/lifecycle fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_version: Literal["atlas/m5-sandbox-continuity/v1"] = SANDBOX_CONTINUITY_PROTOCOL
    binding_id: str
    run_id: str
    task_id: str
    worker_id: str | None = None
    provider_name: str
    sandbox_id: str
    workspace_root: str
    config_digest: str
    capabilities_digest: str
    source_commit: str
    source_digest: str

    @classmethod
    def from_record(cls, record: DurableSandboxBindingRecord) -> SandboxContinuityProjection:
        return cls(
            binding_id=record.binding_id,
            run_id=record.run_id,
            task_id=record.task_id,
            worker_id=record.worker_id,
            provider_name=record.provider_name,
            sandbox_id=record.sandbox_id,
            workspace_root=record.workspace_root,
            config_digest=record.config_digest,
            capabilities_digest=record.capabilities_digest,
            source_commit=record.source_commit,
            source_digest=record.source_digest,
        )

    @classmethod
    def from_binding(cls, binding: ProjectSandboxBinding) -> SandboxContinuityProjection:
        return cls(
            binding_id=binding.binding_id,
            run_id=binding.run_id,
            task_id=binding.task_id,
            worker_id=binding.worker_id,
            provider_name=binding.provider_name,
            sandbox_id=binding.sandbox_handle.id,
            workspace_root=binding.workspace_root,
            config_digest=binding.config_digest,
            capabilities_digest=binding.capabilities_digest,
            source_commit=binding.source_commit,
            source_digest=binding.source_digest,
        )


def _canonical_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def compute_sandbox_config_digest(config: SandboxProjectConfig) -> str:
    """Hash only trusted sandbox execution configuration semantics."""

    return _canonical_digest(
        {
            "provider_name": config.provider_name,
            "image": config.image,
            "snapshot": config.snapshot,
            "workspace_root": config.workspace_root,
            "max_bundle_bytes": config.max_bundle_bytes,
            "network": config.network,
            "secret_references": dict(sorted(config.secret_references.items())),
        }
    )


def compute_sandbox_continuity_digest(
    record: DurableSandboxBindingRecord | ProjectSandboxBinding,
) -> str:
    """One M5-owned continuity representation for M6 consumption."""

    projection = (
        SandboxContinuityProjection.from_record(record)
        if isinstance(record, DurableSandboxBindingRecord)
        else SandboxContinuityProjection.from_binding(record)
    )
    return _canonical_digest(projection.model_dump(mode="json"))


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


class SandboxEnvironmentBlocked(SandboxBindingError):
    """The local sandbox service or its Docker backend is unavailable."""


class SandboxReattachmentRequired(SandboxBindingError):
    """A durable READY binding exists but this runtime has no live handle for it."""


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
    labels = {
        "atlas_run_id": run_id,
        "atlas_task_id": task_id,
        "atlas_m5": "1",
        "atlas_config_digest": compute_sandbox_config_digest(config),
    }
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
