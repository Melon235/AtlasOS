"""SQLite-backed Runtime Suspension state with optimistic concurrency.

This database belongs to the Atlas Runtime. It is not the M6 ApprovalStore and not the M4
ActionLedger. Every advancement is compare-and-swap on ``revision`` so two runtime processes
cannot both commit the same continuation.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from atlasos.suspension.models import (
    SUSPENSION_SCHEMA_VERSION,
    ExecutionSuspension,
    StaleSuspension,
    SuspensionConflict,
    SuspensionNotFound,
    SuspensionSettlement,
    SuspensionStateError,
    SuspensionStoreError,
    SuspensionStoreUnavailable,
    UnsupportedSuspensionSchema,
)

RunState = str
_SCHEMA = """
CREATE TABLE IF NOT EXISTS execution_suspensions (
    suspension_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    task_id TEXT,
    state TEXT NOT NULL,
    revision INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS suspension_run_idx
    ON execution_suspensions (run_id, state);
CREATE INDEX IF NOT EXISTS suspension_state_idx
    ON execution_suspensions (state);
CREATE TABLE IF NOT EXISTS runtime_run_state (
    run_id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    suspension_id TEXT,
    reason TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runtime_task_state (
    run_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    state TEXT NOT NULL,
    suspension_id TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (run_id, task_id)
);
"""


class SQLiteSuspensionStore:
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
            raise SuspensionStoreUnavailable(
                f"runtime suspension store unavailable: {error}"
            ) from error

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10.0)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            with connection:
                yield connection
        except sqlite3.Error as error:
            raise SuspensionStoreUnavailable(
                f"runtime suspension store operation failed: {error}"
            ) from error
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.executescript(_SCHEMA)

    def now(self) -> datetime:
        return self._clock()

    @staticmethod
    def _load(payload: str) -> ExecutionSuspension:
        suspension = ExecutionSuspension.model_validate_json(payload)
        if suspension.schema_version != SUSPENSION_SCHEMA_VERSION:
            raise UnsupportedSuspensionSchema(
                "unsupported execution suspension schema "
                f"{suspension.schema_version!r}; expected {SUSPENSION_SCHEMA_VERSION}"
            )
        return suspension

    def save_if_absent(self, suspension: ExecutionSuspension) -> ExecutionSuspension:
        if suspension.schema_version != SUSPENSION_SCHEMA_VERSION:
            raise UnsupportedSuspensionSchema(
                f"cannot persist suspension schema {suspension.schema_version}"
            )
        with self._connection() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO execution_suspensions "
                "(suspension_id, run_id, task_id, state, revision, created_at, "
                "updated_at, payload) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    suspension.suspension_id,
                    suspension.run_id,
                    suspension.task_id,
                    suspension.state,
                    suspension.revision,
                    suspension.created_at.isoformat(),
                    suspension.updated_at.isoformat(),
                    suspension.model_dump_json(),
                ),
            )
            row = connection.execute(
                "SELECT payload FROM execution_suspensions WHERE suspension_id=?",
                (suspension.suspension_id,),
            ).fetchone()
        if row is None:
            raise SuspensionStoreError(
                f"suspension {suspension.suspension_id!r} disappeared during creation"
            )
        stored = self._load(row[0])
        self._verify_same_identity(stored, suspension)
        return stored

    @staticmethod
    def _verify_same_identity(stored: ExecutionSuspension, proposed: ExecutionSuspension) -> None:
        checks = (
            ("run_id", stored.run_id, proposed.run_id),
            ("task_id", stored.task_id, proposed.task_id),
            ("response_identity", stored.response_identity, proposed.response_identity),
            (
                "assistant_response_digest",
                stored.assistant_response_digest,
                proposed.assistant_response_digest,
            ),
            (
                "pending_invocations",
                [item.model_dump(mode="json") for item in stored.pending_invocations],
                [item.model_dump(mode="json") for item in proposed.pending_invocations],
            ),
        )
        mismatched = [name for name, left, right in checks if left != right]
        if mismatched:
            raise SuspensionConflict(
                "deterministic suspension id collided with different content: "
                + ", ".join(mismatched)
            )

    def get(self, suspension_id: str) -> ExecutionSuspension | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT payload FROM execution_suspensions WHERE suspension_id=?",
                (suspension_id,),
            ).fetchone()
        return self._load(row[0]) if row else None

    def require(self, suspension_id: str) -> ExecutionSuspension:
        suspension = self.get(suspension_id)
        if suspension is None:
            raise SuspensionNotFound(f"no execution suspension {suspension_id!r}")
        return suspension

    def list_for_run(
        self,
        run_id: str,
        *,
        states: tuple[str, ...] | None = None,
    ) -> tuple[ExecutionSuspension, ...]:
        query = "SELECT payload FROM execution_suspensions WHERE run_id=?"
        parameters: list[str] = [run_id]
        if states:
            query += " AND state IN (" + ",".join("?" for _ in states) + ")"
            parameters.extend(states)
        query += " ORDER BY created_at ASC, suspension_id ASC"
        with self._connection() as connection:
            rows = connection.execute(query, tuple(parameters)).fetchall()
        return tuple(self._load(row[0]) for row in rows)

    def active_for_run(self, run_id: str) -> tuple[ExecutionSuspension, ...]:
        return self.list_for_run(run_id, states=("ACTIVE", "SETTLING", "SETTLED"))

    def list_active(self) -> tuple[ExecutionSuspension, ...]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT payload FROM execution_suspensions "
                "WHERE state IN ('ACTIVE','SETTLING','SETTLED') "
                "ORDER BY created_at ASC, suspension_id ASC"
            ).fetchall()
        return tuple(self._load(row[0]) for row in rows)

    def _cas_update(
        self,
        suspension_id: str,
        expected_revision: int,
        *,
        expected_state: str,
        updated: ExecutionSuspension,
    ) -> bool:
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE execution_suspensions SET state=?, revision=?, updated_at=?, payload=? "
                "WHERE suspension_id=? AND revision=? AND state=?",
                (
                    updated.state,
                    updated.revision,
                    updated.updated_at.isoformat(),
                    updated.model_dump_json(),
                    suspension_id,
                    expected_revision,
                    expected_state,
                ),
            )
        return cursor.rowcount == 1

    def begin_settlement(
        self,
        suspension_id: str,
        expected_revision: int,
        *,
        owner: str,
        lease_seconds: float = 60.0,
    ) -> ExecutionSuspension:
        current = self.require(suspension_id)
        now = self.now()
        if current.state == "SETTLED":
            return current
        if current.state == "CLEARED":
            raise StaleSuspension(f"suspension {suspension_id!r} is already cleared")
        if current.state == "STALE":
            raise StaleSuspension(f"suspension {suspension_id!r} is stale")
        if current.revision != expected_revision:
            raise SuspensionConflict(
                f"stale suspension revision: expected {expected_revision}, "
                f"actual {current.revision}"
            )
        if current.state == "SETTLING":
            if current.settling_lease_until is not None and current.settling_lease_until > now:
                raise SuspensionConflict(f"suspension {suspension_id!r} is already being resumed")
        elif current.state != "ACTIVE":
            raise SuspensionStateError(f"cannot settle suspension in state {current.state!r}")
        updated = current.model_copy(
            update={
                "state": "SETTLING",
                "revision": current.revision + 1,
                "updated_at": now,
                "settling_owner": owner,
                "settling_lease_until": now + timedelta(seconds=lease_seconds),
            }
        )
        if not self._cas_update(
            suspension_id,
            current.revision,
            expected_state=current.state,
            updated=updated,
        ):
            raise SuspensionConflict("suspension changed while starting settlement")
        return updated

    def complete_settlement(
        self,
        suspension_id: str,
        expected_revision: int,
        settlement: SuspensionSettlement,
        *,
        owner: str,
    ) -> ExecutionSuspension:
        current = self.require(suspension_id)
        if current.state == "SETTLED":
            return current
        if current.state != "SETTLING":
            raise SuspensionStateError(f"cannot complete settlement in state {current.state!r}")
        if current.revision != expected_revision:
            raise SuspensionConflict(
                f"stale suspension revision: expected {expected_revision}, "
                f"actual {current.revision}"
            )
        if current.settling_owner not in (None, owner):
            raise SuspensionConflict("settlement is owned by another resume process")
        now = self.now()
        updated = current.model_copy(
            update={
                "state": "SETTLED",
                "revision": current.revision + 1,
                "updated_at": now,
                "settlement": settlement,
                "settling_owner": None,
                "settling_lease_until": None,
            }
        )
        if not self._cas_update(
            suspension_id,
            current.revision,
            expected_state="SETTLING",
            updated=updated,
        ):
            raise SuspensionConflict("suspension changed while completing settlement")
        return updated

    def clear(self, suspension_id: str, expected_revision: int) -> ExecutionSuspension:
        current = self.require(suspension_id)
        if current.state == "CLEARED":
            return current
        if current.state != "SETTLED":
            raise SuspensionStateError(f"refusing to clear suspension in state {current.state!r}")
        if current.settlement is None:
            raise SuspensionStateError("refusing to clear suspension without durable results")
        if current.revision != expected_revision:
            raise SuspensionConflict(
                f"stale suspension revision: expected {expected_revision}, "
                f"actual {current.revision}"
            )
        now = self.now()
        updated = current.model_copy(
            update={
                "state": "CLEARED",
                "revision": current.revision + 1,
                "updated_at": now,
            }
        )
        if not self._cas_update(
            suspension_id,
            current.revision,
            expected_state="SETTLED",
            updated=updated,
        ):
            raise SuspensionConflict("suspension changed while clearing")
        return updated

    def release_settlement(
        self,
        suspension_id: str,
        expected_revision: int,
        *,
        owner: str,
        reason: str | None = None,
    ) -> ExecutionSuspension:
        """Return a SETTLING suspension to ACTIVE after a pre-execution refusal.

        No M4 call has happened at this point, so releasing cannot duplicate a side effect. The
        attempt is recorded for operations without becoming execution truth.
        """

        current = self.require(suspension_id)
        if current.state != "SETTLING":
            raise SuspensionStateError(f"cannot release settlement in state {current.state!r}")
        if current.revision != expected_revision:
            raise SuspensionConflict(
                f"stale suspension revision: expected {expected_revision}, "
                f"actual {current.revision}"
            )
        if current.settling_owner not in (None, owner):
            raise SuspensionConflict("settlement is owned by another resume process")
        updated = current.model_copy(
            update={
                "state": "ACTIVE",
                "revision": current.revision + 1,
                "updated_at": self.now(),
                "settling_owner": None,
                "settling_lease_until": None,
                "last_error": reason,
            }
        )
        if not self._cas_update(
            suspension_id,
            current.revision,
            expected_state="SETTLING",
            updated=updated,
        ):
            raise SuspensionConflict("suspension changed while releasing settlement")
        return updated

    def mark_stale(self, suspension_id: str, reason: str) -> ExecutionSuspension:
        current = self.require(suspension_id)
        if current.state == "CLEARED":
            return current
        now = self.now()
        updated = current.model_copy(
            update={
                "state": "STALE",
                "revision": current.revision + 1,
                "updated_at": now,
                "last_error": reason,
            }
        )
        if not self._cas_update(
            suspension_id,
            current.revision,
            expected_state=current.state,
            updated=updated,
        ):
            raise SuspensionConflict("suspension changed while marking stale")
        return updated

    # -- runtime run/task pause state ---------------------------------------

    def mark_run(
        self,
        run_id: str,
        state: str,
        *,
        suspension_id: str | None = None,
        reason: str | None = None,
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO runtime_run_state (run_id, state, suspension_id, reason, updated_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(run_id) DO UPDATE SET state=excluded.state, "
                "suspension_id=excluded.suspension_id, reason=excluded.reason, "
                "updated_at=excluded.updated_at",
                (run_id, state, suspension_id, reason, self.now().isoformat()),
            )

    def run_state(self, run_id: str) -> str | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT state FROM runtime_run_state WHERE run_id=?",
                (run_id,),
            ).fetchone()
        return str(row[0]) if row else None

    def mark_task(
        self,
        run_id: str,
        task_id: str,
        state: str,
        *,
        suspension_id: str | None = None,
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO runtime_task_state "
                "(run_id, task_id, state, suspension_id, updated_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(run_id, task_id) DO UPDATE SET state=excluded.state, "
                "suspension_id=excluded.suspension_id, updated_at=excluded.updated_at",
                (run_id, task_id, state, suspension_id, self.now().isoformat()),
            )

    def task_state(self, run_id: str, task_id: str) -> str | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT state FROM runtime_task_state WHERE run_id=? AND task_id=?",
                (run_id, task_id),
            ).fetchone()
        return str(row[0]) if row else None
