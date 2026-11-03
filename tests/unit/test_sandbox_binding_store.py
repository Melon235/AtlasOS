"""Durable M5 sandbox binding store and continuity projection tests."""

from __future__ import annotations

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
