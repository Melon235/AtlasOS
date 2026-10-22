"""Test/experiment-only fault injection at durable boundaries.

An injected crash represents a process-like interruption, not an ordinary runtime error. It
derives from ``BaseException`` so upstream retry/error handling cannot swallow it. AtlasOS
production paths never raise it; only tests and experiment harnesses do, after they have
confirmed that the target state is durable.
"""

from __future__ import annotations


class InjectedCrash(BaseException):
    """Deterministic interruption raised by test/experiment code."""
