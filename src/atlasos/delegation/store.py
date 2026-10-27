"""Durable committed ExecutionContext snapshot for one run."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from anycode.identity.context import ExecutionContext

SNAPSHOT_SCHEMA_VERSION = 1


class ExecutionContextStoreError(RuntimeError):
    pass


class ExecutionContextStore:
    """Atomic JSON snapshot of all committed actor contexts for one run.

    This is M2 authority recovery truth; AnyCode remains execution truth. It is a sidecar, not a
    second checkpoint engine.
    """

    def __init__(self, path: str | Path, *, run_id: str) -> None:
        self.path = Path(path)
        self.run_id = run_id

    def _empty(self) -> dict[str, Any]:
        return {
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "run_id": self.run_id,
            "contexts": {},
        }

    def _load_payload(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._empty()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ExecutionContextStoreError(
                f"cannot read execution context snapshot {self.path}: {error}"
            ) from error
        if not isinstance(payload, dict):
            raise ExecutionContextStoreError("execution context snapshot must be a JSON object")
        if payload.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
            raise ExecutionContextStoreError(
                "unsupported execution context snapshot schema "
                f"{payload.get('schema_version')!r}; expected {SNAPSHOT_SCHEMA_VERSION}"
            )
        if payload.get("run_id") != self.run_id:
            raise ExecutionContextStoreError(
                f"execution context snapshot belongs to run {payload.get('run_id')!r}, "
                f"not {self.run_id!r}"
            )
        if not isinstance(payload.get("contexts"), dict):
            raise ExecutionContextStoreError(
                "execution context snapshot 'contexts' must be an object"
            )
        return payload

    def load(self) -> dict[str, ExecutionContext]:
        payload = self._load_payload()
        contexts: dict[str, ExecutionContext] = {}
        for actor, raw in payload["contexts"].items():
            if not isinstance(actor, str) or not isinstance(raw, dict):
                raise ExecutionContextStoreError("invalid actor/context entry in snapshot")
            try:
                contexts[actor] = ExecutionContext.model_validate(raw)
            except Exception as error:
                raise ExecutionContextStoreError(
                    f"invalid execution context for {actor!r}: {error}"
                ) from error
        return contexts

    def save_all(self, contexts: dict[str, ExecutionContext]) -> None:
        payload = self._empty()
        payload["contexts"] = {
            actor: context.model_dump(mode="json") for actor, context in sorted(contexts.items())
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(temporary, self.path)

    def save_context(self, actor: str, context: ExecutionContext) -> None:
        contexts = self.load()
        contexts[actor] = context
        self.save_all(contexts)
