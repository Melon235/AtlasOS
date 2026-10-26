"""Per-run progressive disclosure state.

``RunToolState`` is durable runtime logical state: a searchable tool becomes dispatchable only
after ``tool.search`` discloses it in the current run. The set grows monotonically.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

STATE_SCHEMA_VERSION = 1


class ToolStateError(RuntimeError):
    """Raised when durable tool state cannot be restored safely."""


@dataclass
class RunToolState:
    """Dispatchable tool names for exactly one Atlas run."""

    _dispatchable: set[str] = field(default_factory=set)
    revision: int = 0

    def disclose(self, tool_names: set[str] | frozenset[str] | tuple[str, ...]) -> bool:
        """Monotonically add disclosed tools. Returns True when state changed."""

        before = len(self._dispatchable)
        self._dispatchable.update(tool_names)
        if len(self._dispatchable) != before:
            self.revision += 1
            return True
        return False

    def is_dispatchable(self, tool_name: str) -> bool:
        return tool_name in self._dispatchable

    @property
    def dispatchable_tool_names(self) -> frozenset[str]:
        return frozenset(self._dispatchable)

    @property
    def sorted_names(self) -> tuple[str, ...]:
        return tuple(sorted(self._dispatchable))

    def snapshot(self, *, run_id: str) -> dict[str, Any]:
        return {
            "schema_version": STATE_SCHEMA_VERSION,
            "run_id": run_id,
            "revision": self.revision,
            "dispatchable_tool_names": list(self.sorted_names),
            "updated_at": datetime.now(UTC).isoformat(),
        }

    @classmethod
    def from_snapshot(cls, payload: dict[str, Any], *, run_id: str) -> RunToolState:
        if payload.get("schema_version") != STATE_SCHEMA_VERSION:
            raise ToolStateError(
                "unsupported run tool state schema "
                f"{payload.get('schema_version')!r}; expected {STATE_SCHEMA_VERSION}"
            )
        stored_run_id = payload.get("run_id")
        if stored_run_id not in (None, run_id):
            raise ToolStateError(f"run tool state belongs to run {stored_run_id!r}, not {run_id!r}")
        names = payload.get("dispatchable_tool_names", [])
        if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
            raise ToolStateError("dispatchable_tool_names must be a list of strings")
        return cls(_dispatchable=set(names), revision=int(payload.get("revision", 0)))


class RunToolStateStore:
    """Atomic JSON sidecar for one run's disclosure state.

    This is durable Atlas session metadata, not a checkpoint engine: AnyCode remains execution
    truth. It exists so a resumed run restores disclosure history even when the external tool is
    no longer available.
    """

    def __init__(self, path: str | Path | None) -> None:
        self.path = Path(path) if path is not None else None

    def load(self, *, run_id: str) -> RunToolState:
        if self.path is None or not self.path.exists():
            return RunToolState()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ToolStateError(f"cannot read run tool state {self.path}: {error}") from error
        if not isinstance(payload, dict):
            raise ToolStateError(f"run tool state {self.path} must be a JSON object")
        return RunToolState.from_snapshot(payload, run_id=run_id)

    def save(self, state: RunToolState, *, run_id: str) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(state.snapshot(run_id=run_id), sort_keys=True),
            encoding="utf-8",
        )
        os.replace(temporary, self.path)
