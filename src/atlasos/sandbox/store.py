"""SQLite-backed durable M5 sandbox-binding relationship truth.

The durable store owns which sandbox belongs to which Atlas run/task. It deliberately stores no
SDK object, provider session, credential, or live handle. Runtime binds remain an in-process cache.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from atlasos.sandbox.models import (
    DurableBindingState,
    DurableSandboxBindingRecord,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sandbox_bindings (
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
    provider_control_plane_digest TEXT,
    provider_created_at TEXT,
    provider_metadata_digest TEXT,
    payload TEXT NOT NULL,
    UNIQUE (run_id, task_id)
);
CREATE INDEX IF NOT EXISTS sandbox_bindings_run_idx
    ON sandbox_bindings (run_id);
CREATE INDEX IF NOT EXISTS sandbox_bindings_state_idx
    ON sandbox_bindings (state);
"""

_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "READY": frozenset({"LOST", "CLOSED"}),
}

_PROVIDER_VERIFICATION_COLUMNS = {
    "provider_control_plane_digest": "TEXT",
    "provider_created_at": "TEXT",
    "provider_metadata_digest": "TEXT",
}


class SandboxBindingStoreError(RuntimeError):
    pass


class SandboxBindingConflict(SandboxBindingStoreError):
    pass


class SandboxBindingNotFound(SandboxBindingStoreError):
    pass


class SandboxBindingStateError(SandboxBindingStoreError):
    pass


class SandboxBindingStoreUnavailable(SandboxBindingStoreError):
    pass


class SandboxBindingStore(Protocol):
    def create(self, record: DurableSandboxBindingRecord) -> DurableSandboxBindingRecord: ...

    def get(self, binding_id: str) -> DurableSandboxBindingRecord | None: ...

    def get_for_task(self, run_id: str, task_id: str) -> DurableSandboxBindingRecord | None: ...

    def transition(
        self,
        binding_id: str,
        *,
        expected_revision: int,
        new_state: DurableBindingState,
    ) -> DurableSandboxBindingRecord: ...

    def list(self) -> tuple[DurableSandboxBindingRecord, ...]: ...


class SQLiteSandboxBindingStore:
    def __init__(
        self,
        path: str | Path,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.path = Path(path)
        self._clock = clock or (lambda: datetime.now(UTC))
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._initialize()
        except (OSError, sqlite3.Error) as error:
            raise SandboxBindingStoreUnavailable(
                f"durable sandbox binding store unavailable: {error}"
            ) from error

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10.0)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            with connection:
                yield connection
        except sqlite3.IntegrityError:
            raise
        except sqlite3.Error as error:
            raise SandboxBindingStoreUnavailable(
                f"durable sandbox binding store operation failed: {error}"
            ) from error
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.executescript(_SCHEMA)
            existing = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(sandbox_bindings)").fetchall()
            }
            for name, column_type in _PROVIDER_VERIFICATION_COLUMNS.items():
                if name not in existing:
                    connection.execute(
                        f"ALTER TABLE sandbox_bindings ADD COLUMN {name} {column_type}"
                    )

    def _now(self) -> datetime:
        return self._clock()

    @staticmethod
    def _load(payload: str) -> DurableSandboxBindingRecord:
        return DurableSandboxBindingRecord.model_validate_json(payload)

    def _insert(self, connection: sqlite3.Connection, record: DurableSandboxBindingRecord) -> None:
        connection.execute(
            "INSERT INTO sandbox_bindings "
            "(binding_id, run_id, task_id, worker_id, provider_name, sandbox_id, workspace_root, "
            "config_digest, capabilities_digest, source_commit, source_digest, bundle_digest, "
            "state, created_at, updated_at, revision, provider_control_plane_digest, "
            "provider_created_at, provider_metadata_digest, payload) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.binding_id,
                record.run_id,
                record.task_id,
                record.worker_id,
                record.provider_name,
                record.sandbox_id,
                record.workspace_root,
                record.config_digest,
                record.capabilities_digest,
                record.source_commit,
                record.source_digest,
                record.bundle_digest,
                record.state,
                record.created_at.isoformat(),
                record.updated_at.isoformat(),
                record.revision,
                record.provider_control_plane_digest,
                record.provider_created_at.isoformat()
                if record.provider_created_at is not None
                else None,
                record.provider_metadata_digest,
                record.model_dump_json(),
            ),
        )

    def create(self, record: DurableSandboxBindingRecord) -> DurableSandboxBindingRecord:
        try:
            with self._connection() as connection:
                self._insert(connection, record)
        except sqlite3.IntegrityError as error:
            raise SandboxBindingConflict(
                "durable sandbox binding already exists for "
                f"binding_id={record.binding_id!r} or run/task "
                f"{record.run_id!r}/{record.task_id!r}"
            ) from error
        return record

    def get(self, binding_id: str) -> DurableSandboxBindingRecord | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT payload FROM sandbox_bindings WHERE binding_id=?",
                (binding_id,),
            ).fetchone()
        return self._load(row[0]) if row else None

    def get_for_task(self, run_id: str, task_id: str) -> DurableSandboxBindingRecord | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT payload FROM sandbox_bindings WHERE run_id=? AND task_id=?",
                (run_id, task_id),
            ).fetchone()
        return self._load(row[0]) if row else None

    def transition(
        self,
        binding_id: str,
        *,
        expected_revision: int,
        new_state: DurableBindingState,
    ) -> DurableSandboxBindingRecord:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT payload FROM sandbox_bindings WHERE binding_id=?",
                (binding_id,),
            ).fetchone()
            if row is None:
                raise SandboxBindingNotFound(f"durable sandbox binding not found: {binding_id}")
            current = self._load(row[0])
            if current.revision != expected_revision:
                raise SandboxBindingConflict(
                    f"durable sandbox binding {binding_id!r} revision "
                    f"{current.revision} != expected {expected_revision}"
                )
            if new_state not in _ALLOWED_TRANSITIONS.get(current.state, frozenset()):
                raise SandboxBindingStateError(
                    f"durable sandbox binding {binding_id!r} cannot transition "
                    f"{current.state} -> {new_state}"
                )
            updated = current.model_copy(
                update={
                    "state": new_state,
                    "revision": current.revision + 1,
                    "updated_at": self._now(),
                }
            )
            cursor = connection.execute(
                "UPDATE sandbox_bindings SET state=?, updated_at=?, revision=?, payload=? "
                "WHERE binding_id=? AND revision=?",
                (
                    updated.state,
                    updated.updated_at.isoformat(),
                    updated.revision,
                    updated.model_dump_json(),
                    binding_id,
                    expected_revision,
                ),
            )
            if cursor.rowcount != 1:
                raise SandboxBindingConflict(
                    f"durable sandbox binding {binding_id!r} changed concurrently"
                )
        return updated

    def list(self) -> tuple[DurableSandboxBindingRecord, ...]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT payload FROM sandbox_bindings ORDER BY created_at, binding_id"
            ).fetchall()
        return tuple(self._load(row[0]) for row in rows)


__all__ = [
    "SQLiteSandboxBindingStore",
    "SandboxBindingConflict",
    "SandboxBindingNotFound",
    "SandboxBindingStateError",
    "SandboxBindingStore",
    "SandboxBindingStoreError",
    "SandboxBindingStoreUnavailable",
]
