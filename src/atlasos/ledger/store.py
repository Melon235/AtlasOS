"""Thin SQLite-backed durable Action Ledger. Execution dedupe remains AnyCode's."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from atlasos.ledger.models import ActionLedgerRecord, LedgerState

_SCHEMA = """
CREATE TABLE IF NOT EXISTS action_ledger (
    action_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    runtime_instance_id TEXT NOT NULL,
    agent TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    canonical_action TEXT NOT NULL,
    resource TEXT NOT NULL,
    idempotency_key_hash TEXT,
    state TEXT NOT NULL,
    completion_kind TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (action_id, attempt_id)
);
CREATE INDEX IF NOT EXISTS action_ledger_key_idx
    ON action_ledger (idempotency_key_hash);
CREATE INDEX IF NOT EXISTS action_ledger_state_idx
    ON action_ledger (state);
CREATE INDEX IF NOT EXISTS action_ledger_runtime_idx
    ON action_ledger (runtime_instance_id);
"""


class LedgerStoreError(RuntimeError):
    pass


class SQLiteActionLedgerStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10.0)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.executescript(_SCHEMA)

    def create(self, record: ActionLedgerRecord) -> None:
        payload = record.model_dump_json()
        try:
            with self._connection() as connection:
                connection.execute(
                    "INSERT INTO action_ledger "
                    "(action_id, attempt_id, runtime_instance_id, agent, tool_name, "
                    "canonical_action, resource, idempotency_key_hash, state, completion_kind, "
                    "created_at, updated_at, payload) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        record.action_id,
                        record.attempt_id,
                        record.runtime_instance_id,
                        record.agent,
                        record.tool_name,
                        record.canonical_action,
                        record.resource,
                        record.idempotency_key_hash,
                        record.state,
                        record.completion_kind,
                        record.created_at.isoformat(),
                        record.updated_at.isoformat(),
                        payload,
                    ),
                )
        except sqlite3.IntegrityError as error:
            raise LedgerStoreError(
                f"ledger record already exists: {record.action_id}/{record.attempt_id}"
            ) from error

    def update(self, record: ActionLedgerRecord) -> None:
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE action_ledger SET runtime_instance_id=?, agent=?, tool_name=?, "
                "canonical_action=?, resource=?, idempotency_key_hash=?, state=?, "
                "completion_kind=?, updated_at=?, payload=? WHERE action_id=? AND attempt_id=?",
                (
                    record.runtime_instance_id,
                    record.agent,
                    record.tool_name,
                    record.canonical_action,
                    record.resource,
                    record.idempotency_key_hash,
                    record.state,
                    record.completion_kind,
                    record.updated_at.isoformat(),
                    record.model_dump_json(),
                    record.action_id,
                    record.attempt_id,
                ),
            )
            if cursor.rowcount != 1:
                raise LedgerStoreError(
                    f"ledger record not found: {record.action_id}/{record.attempt_id}"
                )

    def get(self, action_id: str, attempt_id: str) -> ActionLedgerRecord | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT payload FROM action_ledger WHERE action_id=? AND attempt_id=?",
                (action_id, attempt_id),
            ).fetchone()
        return ActionLedgerRecord.model_validate_json(row[0]) if row else None

    def latest_for_key_hash(self, key_hash: str) -> ActionLedgerRecord | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT payload FROM action_ledger WHERE idempotency_key_hash=? "
                "ORDER BY created_at DESC, attempt_id DESC LIMIT 1",
                (key_hash,),
            ).fetchone()
        return ActionLedgerRecord.model_validate_json(row[0]) if row else None

    def latest_for_action(self, action_id: str) -> ActionLedgerRecord | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT payload FROM action_ledger WHERE action_id=? "
                "ORDER BY created_at DESC, attempt_id DESC LIMIT 1",
                (action_id,),
            ).fetchone()
        return ActionLedgerRecord.model_validate_json(row[0]) if row else None

    def list(self, *, state: LedgerState | None = None) -> tuple[ActionLedgerRecord, ...]:
        query = "SELECT payload FROM action_ledger"
        parameters: tuple[str, ...] = ()
        if state is not None:
            query += " WHERE state=?"
            parameters = (state,)
        query += " ORDER BY created_at ASC, attempt_id ASC"
        with self._connection() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return tuple(ActionLedgerRecord.model_validate_json(row[0]) for row in rows)
