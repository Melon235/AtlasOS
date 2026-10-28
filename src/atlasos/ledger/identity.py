"""Trusted stable action/idempotency identity; no authorization or execution."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass

from atlasos.ledger.models import ActionLedgerStore


@dataclass(frozen=True)
class ActionIdentity:
    action_id: str
    idempotency_key: str
    idempotency_key_hash: str


class ActionIdentityProvider:
    """Derives stable action IDs from a trusted stable key, persisted via the ledger."""

    def __init__(self, ledger: ActionLedgerStore) -> None:
        self._ledger = ledger

    @staticmethod
    def key_hash(key: str) -> str:
        return hashlib.sha256(key.encode("utf-8")).hexdigest()

    def for_key(self, trusted_key: str | None) -> ActionIdentity | None:
        if not isinstance(trusted_key, str) or not trusted_key.strip():
            return None
        key = trusted_key.strip()
        key_hash = self.key_hash(key)
        existing = self._ledger.latest_for_key_hash(key_hash)
        action_id = existing.action_id if existing is not None else str(uuid.uuid4())
        return ActionIdentity(
            action_id=action_id,
            idempotency_key=key,
            idempotency_key_hash=key_hash,
        )
