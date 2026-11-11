"""Stage R1-1 private tool-payload corpus.

The trajectory transcript records digests only. That keeps a published transcript free of API keys,
provider dialogue, private file contents, personal host paths, unsanitized shell payloads and
sandbox credentials, but it also made the 187 "input unavailable" actions of R1-0 unrecoverable.

This module adds the other half of the split required by the evidence contract:

``PUBLIC_METADATA``
    ``transcript.jsonl`` - digest, shape, size, decision, outcome.
``PRIVATE_TOOL_PAYLOAD``
    ``<run_dir>/private/tool_payloads.jsonl`` - the exact tool input, keyed by call id and digest.

The corpus is written under the run directory, is never copied into ``result.json`` or a report,
and is documented as non-publishable. A payload that cannot be written is reported as
``UNAVAILABLE`` rather than synthesized.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
from pathlib import Path
from typing import Any

EXECUTION_INTEGRITY_OFF = "off"
EXECUTION_INTEGRITY_RECORD = "record"
EXECUTION_INTEGRITY_MODES: tuple[str, ...] = (
    EXECUTION_INTEGRITY_OFF,
    EXECUTION_INTEGRITY_RECORD,
)

INPUT_RECORDED = "RECORDED"
INPUT_REDACTED = "REDACTED"
INPUT_UNAVAILABLE = "UNAVAILABLE"

PRIVATE_PAYLOAD_ROOT = "private"
PRIVATE_PAYLOAD_FILE = "tool_payloads.jsonl"


class ToolPayloadCorpus:
    """Append-only, lock-guarded corpus of exact tool inputs for one run."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.write_failures = 0
        self.last_error: str | None = None

    @classmethod
    def for_run(cls, run_dir: str | Path) -> ToolPayloadCorpus:
        return cls(Path(run_dir) / PRIVATE_PAYLOAD_ROOT / PRIVATE_PAYLOAD_FILE)

    def record(
        self,
        *,
        call_id: str,
        input_digest: str,
        tool_id: str,
        payload: Any,
        worker_id: str | None = None,
        task_id: str | None = None,
        model_response_id: str | None = None,
        ordinal: int | None = None,
    ) -> str | None:
        """Persist one payload and return its reference, or None when it could not be written."""

        ref = payload_ref(call_id=call_id, input_digest=input_digest)
        record = {
            "ref": ref,
            "call_id": call_id,
            "input_digest": input_digest,
            "tool_id": tool_id,
            "worker_id": worker_id,
            "task_id": task_id,
            "model_response_id": model_response_id,
            "ordinal": ordinal,
            "payload": payload,
        }
        try:
            line = json.dumps(record, sort_keys=True, ensure_ascii=True)
        except (TypeError, ValueError) as error:
            self._fail(error)
            return None
        try:
            with (
                self._lock,
                self.path.open("a", encoding="utf-8", newline="\n") as handle,
            ):
                handle.write(line + "\n")
                handle.flush()
                with contextlib.suppress(OSError):
                    os.fsync(handle.fileno())
        except OSError as error:
            self._fail(error)
            return None
        return ref

    def _fail(self, error: BaseException) -> None:
        self.write_failures += 1
        self.last_error = f"{type(error).__name__}: {error}"


def payload_ref(*, call_id: str, input_digest: str) -> str:
    return f"payload:{call_id}:{input_digest[:12]}"


def read_corpus(path: str | Path) -> dict[str, dict[str, Any]]:
    """Read a corpus back, keyed by ``ref``. Corrupt trailing lines are skipped."""

    records: dict[str, dict[str, Any]] = {}
    file = Path(path)
    if not file.is_file():
        return records
    with file.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            ref = record.get("ref")
            if isinstance(ref, str):
                records[ref] = record
    return records


__all__ = [
    "EXECUTION_INTEGRITY_MODES",
    "EXECUTION_INTEGRITY_OFF",
    "EXECUTION_INTEGRITY_RECORD",
    "INPUT_RECORDED",
    "INPUT_REDACTED",
    "INPUT_UNAVAILABLE",
    "PRIVATE_PAYLOAD_FILE",
    "PRIVATE_PAYLOAD_ROOT",
    "ToolPayloadCorpus",
    "payload_ref",
    "read_corpus",
]
