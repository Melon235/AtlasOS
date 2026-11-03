"""Durable M5 sandbox binding store and continuity projection tests."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from atlasos.sandbox import (
    DurableSandboxBindingRecord,
    SandboxBindingConflict,
    SandboxBindingNotFound,
    SandboxBindingStateError,
    SandboxProjectConfig,
    SQLiteSandboxBindingStore,
    compute_sandbox_config_digest,
    compute_sandbox_continuity_digest,
)

CREATED = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def record(**overrides) -> DurableSandboxBindingRecord:
    values = {
        "binding_id": "binding-1",
        "run_id": "run-1",
        "task_id": "task-1",
        "worker_id": "worker-1",
        "provider_name": "fake",
        "sandbox_id": "sandbox-1",
        "workspace_root": "/workspace/project",
        "config_digest": "sha256:config",
        "capabilities_digest": "sha256:caps",
        "source_commit": "a" * 40,
        "source_digest": "sha256:source",
        "bundle_digest": "sha256:bundle",
        "state": "READY",
        "created_at": CREATED,
        "updated_at": CREATED,
        "revision": 1,
    }
    values.update(overrides)
    return DurableSandboxBindingRecord(**values)


def test_store_migrates_stage_a_records_without_provider_evidence(
    tmp_path: Path,
) -> None:
    path = tmp_path / "stage-a.db"
    old = record()
    payload = old.model_dump(mode="json")
    for field in (
        "provider_control_plane_digest",
        "provider_created_at",
        "provider_metadata_digest",
    ):
        payload.pop(field)
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE sandbox_bindings (
            binding_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            task_id TEXT NOT NULL,
            worker_id TEXT,
            provider_name TEXT NOT NULL,
            sandbox_id TEXT NOT NULL,
            workspace_root TEXT NOT NULL,
            config_digest TEXT NOT NULL,
            capabilities_digest TEXT NOT NULL,
            source_commit TEXT NOT NULL,
            source_digest TEXT NOT NULL,
            bundle_digest TEXT NOT NULL,
            state TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            revision INTEGER NOT NULL,
            payload TEXT NOT NULL,
            UNIQUE (run_id, task_id)
        );
        """
    )
    connection.execute(
        "INSERT INTO sandbox_bindings VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            old.binding_id,
            old.run_id,
            old.task_id,
            old.worker_id,
            old.provider_name,
            old.sandbox_id,
            old.workspace_root,
            old.config_digest,
            old.capabilities_digest,
            old.source_commit,
            old.source_digest,
            old.bundle_digest,
            old.state,
            old.created_at.isoformat(),
            old.updated_at.isoformat(),
            old.revision,
            json.dumps(payload),
        ),
    )
    connection.commit()
    connection.close()

    store = SQLiteSandboxBindingStore(path)
    loaded = store.get(old.binding_id)
    assert loaded is not None
    assert loaded.provider_control_plane_digest is None
    assert loaded.provider_created_at is None
    assert loaded.provider_metadata_digest is None
    assert loaded.state == "READY"
    columns_connection = sqlite3.connect(path)
    try:
        columns = {
            row[1]
            for row in columns_connection.execute("PRAGMA table_info(sandbox_bindings)").fetchall()
        }
    finally:
        columns_connection.close()
    assert {
        "provider_control_plane_digest",
        "provider_created_at",
        "provider_metadata_digest",
    } <= columns


def test_store_persists_provider_verification_fields(tmp_path: Path) -> None:
    path = tmp_path / "sandbox_bindings.db"
    first = SQLiteSandboxBindingStore(path)
    created = first.create(
        record(
            provider_control_plane_digest="sha256:control-plane",
            provider_created_at=CREATED,
            provider_metadata_digest="sha256:metadata",
        )
    )
    loaded = SQLiteSandboxBindingStore(path).get(created.binding_id)
    assert loaded is not None
    assert loaded.provider_control_plane_digest == "sha256:control-plane"
    assert loaded.provider_created_at == CREATED
    assert loaded.provider_metadata_digest == "sha256:metadata"


def test_store_persists_binding_across_store_instances(tmp_path: Path) -> None:
    path = tmp_path / "sandbox_bindings.db"
    first = SQLiteSandboxBindingStore(path)
    created = first.create(record())
    assert created.state == "READY"

    second = SQLiteSandboxBindingStore(path)
    loaded = second.get("binding-1")
    assert loaded is not None
    assert loaded == created
    assert second.get_for_task("run-1", "task-1") == created
    assert second.get_for_task("run-1", "other") is None
    assert second.list() == (created,)


def test_store_rejects_second_binding_for_same_run_task(tmp_path: Path) -> None:
    store = SQLiteSandboxBindingStore(tmp_path / "sandbox_bindings.db")
    store.create(record())
    with pytest.raises(SandboxBindingConflict, match="already exists"):
        store.create(
            record(
                binding_id="binding-2",
                sandbox_id="sandbox-2",
                source_digest="sha256:other",
            )
        )
    assert store.get("binding-2") is None


def test_store_transition_is_terminal_and_revision_checked(tmp_path: Path) -> None:
    store = SQLiteSandboxBindingStore(
        tmp_path / "sandbox_bindings.db",
        clock=lambda: CREATED + timedelta(seconds=1),
    )
    store.create(record())
    lost = store.transition("binding-1", expected_revision=1, new_state="LOST")
    assert lost.state == "LOST"
    assert lost.revision == 2
    assert lost.updated_at == CREATED + timedelta(seconds=1)

    with pytest.raises(SandboxBindingStateError, match="cannot transition"):
        store.transition("binding-1", expected_revision=2, new_state="CLOSED")
    with pytest.raises(SandboxBindingConflict, match="revision"):
        store.transition("binding-1", expected_revision=1, new_state="LOST")
    assert store.get("binding-1") == lost

    closed_store = SQLiteSandboxBindingStore(tmp_path / "closed.db")
    closed_store.create(record(binding_id="binding-2", task_id="task-2"))
    closed = closed_store.transition("binding-2", expected_revision=1, new_state="CLOSED")
    assert closed.state == "CLOSED"
    assert closed.revision == 2


def test_store_missing_binding_fails_closed(tmp_path: Path) -> None:
    store = SQLiteSandboxBindingStore(tmp_path / "sandbox_bindings.db")
    with pytest.raises(SandboxBindingNotFound):
        store.transition("missing", expected_revision=1, new_state="LOST")


def test_continuity_digest_is_stable_across_store_reloads(tmp_path: Path) -> None:
    path = tmp_path / "sandbox_bindings.db"
    store = SQLiteSandboxBindingStore(path)
    created = store.create(record())
    digest = compute_sandbox_continuity_digest(created)
    reloaded = SQLiteSandboxBindingStore(path).get("binding-1")
    assert reloaded is not None
    assert reloaded == created
    assert compute_sandbox_continuity_digest(reloaded) == digest
    assert digest.startswith("sha256:")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("sandbox_id", "sandbox-2"),
        ("provider_name", "other-provider"),
        ("workspace_root", "/workspace/other"),
        ("config_digest", "sha256:other-config"),
        ("task_id", "task-2"),
        ("worker_id", "worker-2"),
        ("source_commit", "b" * 40),
        ("source_digest", "sha256:other-source"),
    ],
)
def test_continuity_digest_sensitive_to_identity_fields(field: str, value: str) -> None:
    original = record()
    changed = record(**{field: value})
    assert compute_sandbox_continuity_digest(changed) != compute_sandbox_continuity_digest(original)


def test_continuity_digest_ignores_state_revision_and_timestamp() -> None:
    original = record()
    mutated = record(
        state="LOST",
        revision=9,
        updated_at=CREATED + timedelta(days=1),
    )
    assert compute_sandbox_continuity_digest(mutated) == compute_sandbox_continuity_digest(original)


def test_config_digest_is_stable_and_sensitive_to_trusted_config() -> None:
    config = SandboxProjectConfig(provider_name="fake", image="fake@sha256:0123456789abcdef")
    same = SandboxProjectConfig(provider_name="fake", image="fake@sha256:0123456789abcdef")
    different = SandboxProjectConfig(provider_name="fake", image="fake@sha256:fedcba9876543210")
    digest = compute_sandbox_config_digest(config)
    assert digest == compute_sandbox_config_digest(same)
    assert digest != compute_sandbox_config_digest(different)
    assert "secret" not in digest.lower()
    assert digest.startswith("sha256:")
