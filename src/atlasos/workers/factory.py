"""Scoped worker factory: trusted profile + assignment -> isolated AnyCode worker."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from anycode.identity.context import ExecutionContext
from anycode.types import AgentConfig

from atlasos.workers.catalog import WorkerCatalog
from atlasos.workers.models import (
    WorkerAssignment,
    WorkerConfigurationError,
    WorkerInstance,
    WorkerInstanceRecord,
    WorkerProfile,
    WorkerRuntimeMetadata,
    WorkerTaskContext,
    safe_parent_attributes,
)
from atlasos.workers.renderer import render_worker_context, render_worker_context_hash
from atlasos.workers.store import WorkerStore

if TYPE_CHECKING:
    from atlasos.trajectory.recorder import TrajectoryRecorder

DEFAULT_AVAILABLE_TOOLS = frozenset(
    {
        "tool_search",
        "dispatch_action",
        "file_read",
        "file_write",
        "file_edit",
        "grep",
        "list_files",
        "bash",
    }
)


class ScopedWorkerFactory:
    """Builds focused workers. Never performs action authorization or HITL."""

    def __init__(
        self,
        *,
        model: str,
        provider: str | None,
        store: WorkerStore,
        catalog: WorkerCatalog | None = None,
        available_tools: Iterable[str] | None = None,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
        trajectory_recorder: TrajectoryRecorder | None = None,
    ) -> None:
        self.model = model
        self.provider = provider
        self.store = store
        self.catalog = catalog or WorkerCatalog()
        self.available_tools = frozenset(
            DEFAULT_AVAILABLE_TOOLS if available_tools is None else available_tools
        )
        self._clock = clock or (lambda: datetime.now(UTC))
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex)
        self.trajectory_recorder = trajectory_recorder

    def create(
        self,
        *,
        parent_context: ExecutionContext,
        parent_agent: str,
        assignment: WorkerAssignment,
        worker_id: str | None = None,
        task_id: str | None = None,
    ) -> WorkerInstance:
        profile = self.catalog.resolve(assignment.profile_id)
        self._validate_profile(profile)
        actual_worker_id = worker_id or self._id_factory()
        actual_task_id = task_id or self._id_factory()
        task = WorkerTaskContext(
            task_id=actual_task_id,
            objective=assignment.task,
            background=assignment.context,
        )
        rendered = render_worker_context(profile, task)
        context = self._derive_context(
            parent_context=parent_context,
            parent_agent=parent_agent,
            profile=profile,
            worker_id=actual_worker_id,
            task_id=actual_task_id,
        )
        worker_name = f"{profile.profile_id}:{actual_worker_id}"
        config = AgentConfig(
            name=worker_name,
            model=self.model,
            provider=self.provider,
            system_prompt=rendered,
            tools=list(profile.base_tools),
            max_turns=profile.max_turns,
            execution_context=context,
        )
        record = WorkerInstanceRecord(
            worker_id=actual_worker_id,
            worker_name=worker_name,
            profile_id=profile.profile_id,
            task_id=actual_task_id,
            parent_agent=parent_agent,
            assignment=assignment,
            task_context=task,
            execution_context=context,
            rendered_context_hash=render_worker_context_hash(profile, task),
            created_at=self._clock(),
        )
        self.store.save(record)
        self._record_lifecycle(record, mode="create", occurred_at=record.created_at)
        return WorkerInstance(
            record=record,
            profile=profile,
            agent_config=config,
            rendered_context=rendered,
        )

    def resume(self, worker_id: str) -> WorkerInstance:
        record = self.store.load(worker_id)
        profile = self.catalog.resolve(record.profile_id)
        self._validate_profile(profile)
        rendered = render_worker_context(profile, record.task_context)
        expected_hash = render_worker_context_hash(profile, record.task_context)
        if expected_hash != record.rendered_context_hash:
            raise WorkerConfigurationError("worker rendered context hash mismatch on resume")
        # resume(worker_id) has no trusted expected principal/parent identity parameter; the
        # persisted record schema, render hash, and atlas_worker metadata are the available
        # integrity checks. Do not invent a tautological security comparison.
        metadata = record.execution_context.attributes.get("atlas_worker")
        if not isinstance(metadata, dict):
            raise WorkerConfigurationError("worker context metadata missing")
        if (
            metadata.get("worker_id") != record.worker_id
            or metadata.get("profile_id") != record.profile_id
            or metadata.get("task_id") != record.task_id
        ):
            raise WorkerConfigurationError("worker context metadata does not match record")
        config = AgentConfig(
            name=record.worker_name,
            model=self.model,
            provider=self.provider,
            system_prompt=rendered,
            tools=list(profile.base_tools),
            max_turns=profile.max_turns,
            execution_context=record.execution_context,
        )
        restored_at = self._clock()
        self._record_lifecycle(record, mode="restore", occurred_at=restored_at)
        return WorkerInstance(
            record=record,
            profile=profile,
            agent_config=config,
            rendered_context=rendered,
            resumed=True,
        )

    def _record_lifecycle(
        self,
        record: WorkerInstanceRecord,
        *,
        mode: str,
        occurred_at: datetime,
    ) -> None:
        recorder = self.trajectory_recorder
        if recorder is None:
            return
        try:  # noqa: SIM105
            recorder.record_worker_lifecycle(
                worker_id=record.worker_id,
                profile_id=record.profile_id,
                parent_agent=record.parent_agent,
                mode=mode,
                context_hash=record.rendered_context_hash,
                transition_id=occurred_at.isoformat(),
                occurred_at=occurred_at,
                correlation=None,
            )
        except Exception:  # noqa: BLE001 - evidence is optional
            pass

    def _validate_profile(self, profile: WorkerProfile) -> None:
        unknown = set(profile.base_tools) - self.available_tools
        if unknown:
            raise WorkerConfigurationError(
                f"worker profile {profile.profile_id!r} uses unavailable tools: {sorted(unknown)}"
            )

    def _derive_context(
        self,
        *,
        parent_context: ExecutionContext,
        parent_agent: str,
        profile,
        worker_id: str,
        task_id: str,
    ) -> ExecutionContext:
        metadata = WorkerRuntimeMetadata(
            worker_id=worker_id,
            profile_id=profile.profile_id,
            parent_agent=parent_agent,
            task_id=task_id,
            operating_scope=profile.operating_scope,
            can_delegate=profile.can_delegate,
        )
        attributes = safe_parent_attributes(dict(parent_context.attributes))
        attributes["atlas_worker"] = metadata.nested_attributes()
        # Flat trusted mirrors let the frozen M3 action context consume worker metadata.
        attributes["worker_id"] = worker_id
        attributes["profile_id"] = profile.profile_id
        attributes["worker_profile"] = profile.profile_id
        attributes["operating_scope"] = profile.operating_scope[0]
        attributes["task_id"] = task_id
        attributes["parent_agent"] = parent_agent
        attributes["worker_name"] = f"{profile.profile_id}:{worker_id}"
        return parent_context.model_copy(
            update={
                "delegation": (),
                "attributes": attributes,
            }
        )
