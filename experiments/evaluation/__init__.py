"""AtlasOS evaluation layer built on Inspect AI.

This package contains only evaluation glue: task definitions, a thin runtime adapter that
composes the existing AtlasOS/AnyCode runtime, independent oracles, scoring, and experiment
identity. It never owns tool execution, policy, approval, idempotency, scheduling, or recovery.
"""

from __future__ import annotations
