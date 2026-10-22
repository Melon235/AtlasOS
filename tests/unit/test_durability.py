"""Stage 5 durability core: manifest, compatibility, deterministic context rebuild."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from anycode import CheckpointData, TokenUsage
from anycode.tasks.task import create_task

from atlasos.durability import (
    SCHEMA_VERSION,
    AtlasSessionManifest,
    ManifestMismatch,
    read_manifest,
    rebuild_context_store,
    verify_compatible,
    workflow_hash,
    write_manifest,
)
from atlasos.workflow import AtlasAgentSpec, AtlasTaskSpec, AtlasWorkflow


def _workflow() -> AtlasWorkflow:
    return AtlasWorkflow(
        name="recovery",
        agents=(AtlasAgentSpec("a", "a", "worker"), AtlasAgentSpec("b", "b", "worker")),
        tasks=(
            AtlasTaskSpec("one", "One", "first", "a"),
            AtlasTaskSpec("two", "Two", "second", "b", ("one",)),
        ),
    )


def _manifest(tmp_path) -> AtlasSessionManifest:
    return AtlasSessionManifest(
        run_id="run-1",
        created_at=datetime.now(UTC),
        git_commit="abc123",
        git_dirty=False,
        workflow_hash=workflow_hash(_workflow()),
        config_hash="cfg",
        anycode_version="0.9.0",
        contextpilot_version=None,
        model="deepseek-chat",
        placement_policy="bypass",
        placement_weights={"locality": 1.0, "load": 0.2},
        context_policy="task_scoped",
        renderer_policy="stable_deterministic",
        context_transform="identity",
        checkpoint_location=str(tmp_path / "anycode"),
    )


def test_manifest_roundtrip_and_schema(tmp_path) -> None:
    manifest = _manifest(tmp_path)
    path = tmp_path / "atlas_manifest.json"

    write_manifest(path, manifest)
    loaded = read_manifest(path)

    assert loaded == manifest
    assert loaded.schema_version == SCHEMA_VERSION
    assert "key" not in path.read_text(encoding="utf-8").lower()


def test_unknown_schema_is_rejected(tmp_path) -> None:
    manifest = _manifest(tmp_path)
    path = tmp_path / "atlas_manifest.json"
    write_manifest(path, manifest)
    path.write_text(
        path.read_text(encoding="utf-8").replace('"schema_version":1', '"schema_version":99'),
        encoding="utf-8",
    )

    with pytest.raises(ManifestMismatch, match="unsupported manifest schema"):
        read_manifest(path)


def test_resume_rejects_semantic_changes(tmp_path) -> None:
    manifest = _manifest(tmp_path)

    with pytest.raises(ManifestMismatch, match="workflow_hash"):
        verify_compatible(
            manifest,
            workflow=AtlasWorkflow(name="other", agents=(), tasks=()),
            config_hash="cfg",
            placement_policy="bypass",
            context_policy="task_scoped",
            renderer_policy="stable_deterministic",
            context_transform="identity",
        )

    with pytest.raises(ManifestMismatch, match="placement_policy"):
        verify_compatible(
            manifest,
            workflow=_workflow(),
            config_hash="cfg",
            placement_policy="context_locality",
            context_policy="task_scoped",
            renderer_policy="stable_deterministic",
            context_transform="identity",
        )


def _checkpoint() -> CheckpointData:
    completed = create_task(title="One", description="first", assignee="a").model_copy(
        update={"status": "completed", "result": "out-one"}
    )
    failed = create_task(title="Two", description="second", assignee="b").model_copy(
        update={"status": "failed", "result": "partial"}
    )
    return CheckpointData.model_construct(
        id="c1",
        workflow_id="w",
        version=2,
        tasks=[completed, failed],
        agent_results={},
        wave_index=0,
        total_token_usage=TokenUsage(),
        created_at=datetime.now(UTC),
    )


def test_rebuild_only_uses_completed_tasks() -> None:
    store = rebuild_context_store(_checkpoint(), _workflow())

    block = store.get("task-output:one")
    assert block.content == "out-one"
    assert block.version == 1
    assert block.task_id == "one"
    assert block.owner_agent_id == "a"

    with pytest.raises(KeyError):
        store.get("task-output:two")


def test_rebuild_is_deterministic() -> None:
    first = rebuild_context_store(_checkpoint(), _workflow())
    second = rebuild_context_store(_checkpoint(), _workflow())

    assert first.find_by_task("one") == second.find_by_task("one")
