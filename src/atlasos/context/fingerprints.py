"""Deterministic fingerprinting for context layouts.

``context_layout_hash`` answers "did selected block ordering change?" independent of the full
prompt text. Hashes are metadata only and never rendered into prompts.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence

from atlasos.context.models import ContextRef


def context_layout_hash(refs: Sequence[ContextRef]) -> str:
    payload = [
        {"block_id": ref.block_id, "version": ref.version, "position": position}
        for position, ref in enumerate(refs)
    ]
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
