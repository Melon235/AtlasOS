"""AtlasRuntime adapter for the third-party Commerce Agent Bench.

The benchmark, its tasks, mock services and graders are upstream work by the Accio team at Alibaba
International (Apache-2.0 code, CC BY 4.0 task data). This package only adapts the *runtime*: it
starts the original environment, exposes the original tool surfaces to an Atlas agent, and runs the
original grader. Nothing here re-implements or redefines benchmark semantics.
"""

from __future__ import annotations

__all__ = ["ADAPTER_VERSION"]

ADAPTER_VERSION = "0.1.0"
