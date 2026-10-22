"""Deterministic prompt fingerprints used only for cache diagnostics.

A fingerprint is metadata: it is recorded next to an inference call and never injected into a
prompt. Hashing the same rendered prompt twice must yield the same value, so the canonical form
below fixes key order and separators.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from typing import Any

from anycode.types import LLMMessage, LLMToolDef


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _tools_payload(tools: Sequence[LLMToolDef] | None) -> list[dict[str, Any]]:
    return [tool.model_dump(mode="json") for tool in tools or ()]


def stable_prefix_hash(
    system_prompt: str | None,
    tools: Sequence[LLMToolDef] | None = None,
) -> str:
    """Hash the parts of a prompt that must stay byte-identical across turns."""

    payload = {"system": system_prompt or "", "tools": _tools_payload(tools)}
    return _sha256(_canonical(payload))


def full_prompt_hash(
    system_prompt: str | None,
    messages: Sequence[LLMMessage],
    tools: Sequence[LLMToolDef] | None = None,
) -> str:
    """Hash the complete rendered prompt, including the dynamic tail."""

    payload = {
        "system": system_prompt or "",
        "tools": _tools_payload(tools),
        "messages": [message.model_dump(mode="json") for message in messages],
    }
    return _sha256(_canonical(payload))
