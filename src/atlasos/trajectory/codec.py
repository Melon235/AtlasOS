"""Trajectory payload codec and metadata-only projection helpers."""

from __future__ import annotations

import json
from typing import Any

from anycode.types import TranscriptEvent

from atlasos.trajectory.models import (
    TRAJECTORY_SCHEMA_VERSION,
    TrajectoryEnvelope,
    canonical_digest,
    canonical_json,
    validate_data,
)

ATLAS_TRAJECTORY_KEY = "atlas_trajectory"


class UnsupportedTrajectorySchema(ValueError):
    pass


def encode_envelope(envelope: TrajectoryEnvelope) -> dict[str, Any]:
    """Wrap an envelope in the namespaced lifecycle payload."""

    validate_data(envelope)
    return {ATLAS_TRAJECTORY_KEY: envelope.model_dump(mode="json")}


def decode_envelope(payload: dict[str, Any], *, seq: int) -> TrajectoryEnvelope | None:
    raw = payload.get(ATLAS_TRAJECTORY_KEY)
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise UnsupportedTrajectorySchema(
            f"atlas_trajectory payload at seq {seq} must be an object"
        )
    version = raw.get("schema_version")
    if type(version) is not int or version != TRAJECTORY_SCHEMA_VERSION:
        raise UnsupportedTrajectorySchema(f"unsupported trajectory schema {version!r} at seq {seq}")
    envelope = TrajectoryEnvelope.model_validate(raw)
    return validate_data(envelope)


def semantic_payload(envelope: TrajectoryEnvelope) -> str:
    """Canonical payload for same-event_id conflict detection.

    ``backfilled`` is evidence metadata, not a different semantic transition.
    """

    payload = envelope.model_dump(mode="json")
    payload.pop("backfilled", None)
    payload.pop("occurred_at", None)
    return canonical_json(payload)


def content_digest_and_size(content: Any) -> tuple[str, int]:
    encoded = canonical_json(content)
    return canonical_digest(content), len(encoded.encode("utf-8"))


def shape_of(value: Any) -> str:
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if value is None:
        return "null"
    return type(value).__name__


def size_of(value: Any) -> int:
    if isinstance(value, str):
        return len(value.encode("utf-8"))
    try:
        return len(canonical_json(value).encode("utf-8"))
    except (TypeError, ValueError):
        return len(str(value).encode("utf-8"))


def native_message_projection(event: TranscriptEvent) -> dict[str, Any] | None:
    """Project an AnyCode ``message`` event metadata-only.

    Current Atlas runs do not enable AgentRunner durability, so this path is defensive. It is
    still useful when a host has an existing AnyCode transcript.
    """

    if event.kind != "message":
        return None
    payload = event.payload
    role = payload.get("role")
    if role not in ("user", "assistant"):
        return None
    content = payload.get("content", [])
    digest, size = content_digest_and_size(content)
    return {
        "role": role,
        "content_digest": digest,
        "content_size": size,
        "transcript_seq_ref": event.seq,
    }


def native_tool_result_projection(event: TranscriptEvent) -> dict[str, Any] | None:
    """Defensive projection of an AnyCode ``tool_result`` event.

    Returns ``None`` when the native payload lacks a correlated tool-use id. The current
    AgentRunner persistence does not write that id, so Atlas records its own namespaced
    observation instead.
    """

    if event.kind != "tool_result":
        return None
    payload = event.payload
    call_id = payload.get("tool_use_id") or payload.get("call_id")
    if not isinstance(call_id, str) or not call_id:
        return None
    tool_name = payload.get("tool_name")
    if not isinstance(tool_name, str):
        tool_name = "unknown"
    output = payload.get("output", payload.get("content", ""))
    is_error = bool(payload.get("is_error", False))
    digest, size = content_digest_and_size(output)
    return {
        "call_id": call_id,
        "tool_id": tool_name,
        "tool_name": tool_name,
        "is_error": is_error,
        "result_digest": digest,
        "result_size": size,
        "transcript_seq_ref": event.seq,
    }


def safe_digest(value: Any) -> str:
    return canonical_digest(value)


def parse_json_object(raw: str) -> dict[str, Any] | None:
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None
