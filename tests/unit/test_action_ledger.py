"""M4 ledger model and persistence tests."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from atlasos.ledger import ActionLedgerRecord, SQLiteActionLedgerStore
from atlasos.ledger.bridge import _digest


def record(**overrides) -> ActionLedgerRecord:
    now = datetime.now(UTC)
    values = {
        "action_id": "action-1",
        "attempt_id": "attempt-1",
        "runtime_instance_id": "runtime-1",
        "agent": "agent:planner",
        "tool_name": "email.send",
        "canonical_action": "communication.send",
        "resource": "email:ops@example.test",
        "request_digest": "digest-1",
        "digest_version": "1",
        "idempotency_key_hash": "key-hash-1",
        "state": "IN_PROGRESS",
        "created_at": now,
        "updated_at": now,
    }
    values.update(overrides)
    return ActionLedgerRecord(**values)


def test_m4_01_record_is_immutable_and_strict() -> None:
    item = record()
    with pytest.raises(ValidationError):
        item.agent = "other"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        record(unexpected="x")  # type: ignore[call-arg]


def test_m4_02_state_enum_is_small() -> None:
    assert {record().state} == {"IN_PROGRESS"}
    with pytest.raises(ValidationError):
        record(state="FAILED")  # type: ignore[arg-type]


def test_m4_03_no_raw_arguments_or_results_in_record() -> None:
    item = record()
    dumped = item.model_dump()
    assert "arguments" not in dumped
    assert "result" not in dumped
    assert "secret" not in item.model_dump_json()
    assert item.request_digest == "digest-1"


def test_m4_04_request_digest_deterministic() -> None:
    first = _digest({"body": "hello", "to": "ops@example.test"})
    second = _digest({"to": "ops@example.test", "body": "hello"})
    assert first == second
    assert first != _digest({"body": "changed", "to": "ops@example.test"})


def test_m4_05_digest_version_explicit() -> None:
    assert record().digest_version == "1"


def test_m4_06_action_and_attempt_ids_are_separate() -> None:
    item = record(action_id="action-1", attempt_id="attempt-2")
    assert item.action_id != item.attempt_id


def test_m4_07_runtime_instance_id_stored() -> None:
    assert record().runtime_instance_id == "runtime-1"


def test_m4_08_ledger_persists_across_restart(tmp_path: Path) -> None:
    path = tmp_path / "ledger.db"
    store = SQLiteActionLedgerStore(path)
    item = record()
    store.create(item)
    reopened = SQLiteActionLedgerStore(path)
    assert reopened.get("action-1", "attempt-1") == item


def test_m4_09_completed_record_reloads(tmp_path: Path) -> None:
    path = tmp_path / "ledger.db"
    store = SQLiteActionLedgerStore(path)
    now = datetime.now(UTC)
    store.create(
        record(
            state="COMPLETED",
            completion_kind="EXECUTED_SUCCESS",
            result_digest="result-digest",
            retry_safe=True,
            completed_at=now,
        )
    )
    loaded = SQLiteActionLedgerStore(path).get("action-1", "attempt-1")
    assert loaded is not None
    assert loaded.state == "COMPLETED"
    assert loaded.completion_kind == "EXECUTED_SUCCESS"


def test_m4_10_uncertain_record_reloads(tmp_path: Path) -> None:
    path = tmp_path / "ledger.db"
    store = SQLiteActionLedgerStore(path)
    store.create(record(state="UNCERTAIN", completion_kind="EXECUTED_ERROR", retry_safe=False))
    loaded = SQLiteActionLedgerStore(path).get("action-1", "attempt-1")
    assert loaded is not None
    assert loaded.state == "UNCERTAIN"
    assert loaded.retry_safe is False
