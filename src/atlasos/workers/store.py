"""Minimal durable WorkerInstance sidecar; no Agent objects or checkpoints are persisted."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from atlasos.workers.models import WorkerInstanceRecord

SCHEMA_VERSION = 1


class WorkerStoreError(RuntimeError):
    pass


class WorkerStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"schema_version": SCHEMA_VERSION, "workers": {}}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise WorkerStoreError(f"cannot read worker store {self.path}: {error}") from error
        if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
            raise WorkerStoreError("unsupported worker store schema")
        workers = payload.get("workers")
        if not isinstance(workers, dict):
            raise WorkerStoreError("worker store 'workers' must be an object")
        return payload

    def save(self, record: WorkerInstanceRecord) -> None:
        payload = self._load()
        payload["workers"][record.worker_id] = record.model_dump(mode="json")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(temporary, self.path)

    def load(self, worker_id: str) -> WorkerInstanceRecord:
        raw = self._load()["workers"].get(worker_id)
        if raw is None:
            raise WorkerStoreError(f"unknown worker {worker_id!r}")
        try:
            return WorkerInstanceRecord.model_validate(raw)
        except Exception as error:
            raise WorkerStoreError(f"invalid worker record {worker_id!r}: {error}") from error

    def list(self) -> tuple[WorkerInstanceRecord, ...]:
        workers = self._load()["workers"]
        return tuple(
            WorkerInstanceRecord.model_validate(raw)
            for raw in sorted(workers.values(), key=lambda item: item["worker_id"])
        )
