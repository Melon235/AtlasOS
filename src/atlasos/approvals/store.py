"""SQLite-backed durable ApprovalStore (human decision truth only).

This is not a Runtime store, an M4 ActionLedger, or an idempotency store. It owns exactly one
question: has a trusted human made a terminal decision for this exact pending action?

Resolution is compare-and-swap on ``(state=PENDING, version=N)``. Two reviewers racing produce
exactly one terminal decision; the loser observes ``already_resolved``.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from atlasos.approvals.models import (
    ApprovalChallenge,
    ApprovalCreateResult,
    ApprovalDecision,
    ApprovalDecisionValue,
    ApprovalIdentityConflict,
    ApprovalResolution,
    ApprovalStoreError,
    ApprovalStoreUnavailable,
    ReviewerIdentity,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS approval_challenges (
    approval_id TEXT PRIMARY KEY,
    suspension_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    task_id TEXT,
    worker_id TEXT,
    profile_id TEXT,
    call_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    action_digest TEXT NOT NULL,
    decision_basis_digest TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    state TEXT NOT NULL,
    version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS approval_suspension_idx
    ON approval_challenges (suspension_id, state);
CREATE INDEX IF NOT EXISTS approval_run_idx
    ON approval_challenges (run_id, state);
CREATE INDEX IF NOT EXISTS approval_state_idx
    ON approval_challenges (state);
"""


class SQLiteApprovalStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._initialize()
        except (OSError, sqlite3.Error) as error:
            raise ApprovalStoreUnavailable(f"approval store unavailable: {error}") from error

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
            raise ApprovalStoreUnavailable(f"approval store operation failed: {error}") from error
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.executescript(_SCHEMA)

    def create_if_absent(self, challenge: ApprovalChallenge) -> ApprovalCreateResult:
        payload = challenge.model_dump_json()
        with self._connection() as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO approval_challenges "
                "(approval_id, suspension_id, run_id, task_id, worker_id, profile_id, call_id, "
                "ordinal, action_digest, decision_basis_digest, tool_name, state, version, "
                "created_at, payload) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    challenge.approval_id,
                    challenge.suspension_id,
                    challenge.run_id,
                    challenge.task_id,
                    challenge.worker_id,
                    challenge.profile_id,
                    challenge.call_id,
                    challenge.ordinal,
                    challenge.action_digest,
                    challenge.decision_basis_digest,
                    challenge.tool_name,
                    challenge.state,
                    challenge.version,
                    challenge.created_at.isoformat(),
                    payload,
                ),
            )
            existing_row = connection.execute(
                "SELECT payload FROM approval_challenges WHERE approval_id=?",
                (challenge.approval_id,),
            ).fetchone()
        if existing_row is None:
            raise ApprovalStoreError(
                f"approval challenge {challenge.approval_id!r} disappeared during creation"
            )
        existing = ApprovalChallenge.model_validate_json(existing_row[0])
        self._verify_same_identity(existing, challenge)
        return ApprovalCreateResult(challenge=existing, created=cursor.rowcount == 1)

    @staticmethod
    def _verify_same_identity(existing: ApprovalChallenge, proposed: ApprovalChallenge) -> None:
        checks = (
            ("suspension_id", existing.suspension_id, proposed.suspension_id),
            ("call_id", existing.call_id, proposed.call_id),
            ("ordinal", existing.ordinal, proposed.ordinal),
            ("action_digest", existing.action_digest, proposed.action_digest),
            (
                "decision_basis_digest",
                existing.decision_basis_digest,
                proposed.decision_basis_digest,
            ),
            ("tool_name", existing.tool_name, proposed.tool_name),
        )
        mismatched = [name for name, stored, current in checks if stored != current]
        if mismatched:
            raise ApprovalIdentityConflict(
                "approval identity collision for the same deterministic id: "
                + ", ".join(mismatched)
            )

    def get(self, approval_id: str) -> ApprovalChallenge | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT payload FROM approval_challenges WHERE approval_id=?",
                (approval_id,),
            ).fetchone()
        return ApprovalChallenge.model_validate_json(row[0]) if row else None

    def list_pending(
        self,
        *,
        suspension_id: str | None = None,
        run_id: str | None = None,
    ) -> tuple[ApprovalChallenge, ...]:
        query = "SELECT payload FROM approval_challenges WHERE state='PENDING'"
        parameters: list[str] = []
        if suspension_id is not None:
            query += " AND suspension_id=?"
            parameters.append(suspension_id)
        if run_id is not None:
            query += " AND run_id=?"
            parameters.append(run_id)
        query += " ORDER BY created_at ASC, approval_id ASC"
        with self._connection() as connection:
            rows = connection.execute(query, tuple(parameters)).fetchall()
        return tuple(ApprovalChallenge.model_validate_json(row[0]) for row in rows)

    def list_for_suspension(self, suspension_id: str) -> tuple[ApprovalChallenge, ...]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT payload FROM approval_challenges WHERE suspension_id=? "
                "ORDER BY ordinal ASC, approval_id ASC",
                (suspension_id,),
            ).fetchall()
        return tuple(ApprovalChallenge.model_validate_json(row[0]) for row in rows)

    def list_all(self) -> tuple[ApprovalChallenge, ...]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT payload FROM approval_challenges ORDER BY created_at ASC, approval_id ASC"
            ).fetchall()
        return tuple(ApprovalChallenge.model_validate_json(row[0]) for row in rows)

    def resolve(
        self,
        approval_id: str,
        expected_version: int,
        decision: ApprovalDecisionValue,
        reviewer: ReviewerIdentity,
        reason: str | None = None,
    ) -> ApprovalResolution:
        if decision not in ("APPROVED", "REJECTED"):
            raise ApprovalStoreError(f"invalid terminal decision {decision!r}")
        current = self.get(approval_id)
        if current is None:
            return ApprovalResolution(status="not_found")
        if current.state != "PENDING":
            return ApprovalResolution(status="already_resolved", challenge=current)
        if current.version != expected_version:
            return ApprovalResolution(status="version_conflict", challenge=current)

        now = datetime.now(UTC)
        terminal = current.model_copy(
            update={
                "state": decision,
                "version": current.version + 1,
                "resolved_at": now,
                "actor": reviewer.audit_label(),
                "reason": reason,
            }
        )
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE approval_challenges SET state=?, version=?, payload=? "
                "WHERE approval_id=? AND state='PENDING' AND version=?",
                (
                    terminal.state,
                    terminal.version,
                    terminal.model_dump_json(),
                    approval_id,
                    expected_version,
                ),
            )
        if cursor.rowcount != 1:
            observed = self.get(approval_id)
            if observed is None:
                return ApprovalResolution(status="not_found")
            if observed.state != "PENDING":
                return ApprovalResolution(status="already_resolved", challenge=observed)
            return ApprovalResolution(status="version_conflict", challenge=observed)
        decision_record = ApprovalDecision(
            approval_id=terminal.approval_id,
            suspension_id=terminal.suspension_id,
            call_id=terminal.call_id,
            decision=decision,
            action_digest=terminal.action_digest,
            decision_basis_digest=terminal.decision_basis_digest,
            reviewer=reviewer.audit_label(),
            reason=reason,
            decided_at=now,
            version=terminal.version,
        )
        return ApprovalResolution(
            status="resolved",
            challenge=terminal,
            decision=decision_record,
        )

    def supersede(
        self,
        approval_id: str,
        expected_version: int,
        reviewer: ReviewerIdentity,
        reason: str,
    ) -> ApprovalResolution:
        current = self.get(approval_id)
        if current is None:
            return ApprovalResolution(status="not_found")
        if current.state != "PENDING":
            return ApprovalResolution(status="already_resolved", challenge=current)
        if current.version != expected_version:
            return ApprovalResolution(status="version_conflict", challenge=current)
        terminal = current.model_copy(
            update={
                "state": "SUPERSEDED",
                "version": current.version + 1,
                "resolved_at": datetime.now(UTC),
                "actor": reviewer.audit_label(),
                "reason": reason,
            }
        )
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE approval_challenges SET state=?, version=?, payload=? "
                "WHERE approval_id=? AND state='PENDING' AND version=?",
                (
                    terminal.state,
                    terminal.version,
                    terminal.model_dump_json(),
                    approval_id,
                    expected_version,
                ),
            )
        if cursor.rowcount != 1:
            observed = self.get(approval_id)
            if observed is None:
                return ApprovalResolution(status="not_found")
            if observed.state != "PENDING":
                return ApprovalResolution(status="already_resolved", challenge=observed)
            return ApprovalResolution(status="version_conflict", challenge=observed)
        return ApprovalResolution(status="resolved", challenge=terminal)
