"""Stage 0.8: prove the official ContextPilot package works in-process, without a server.

No AtlasOS optimization is exercised here; this only records that the external algorithm
backend can be installed and called as a normal Python dependency.
"""

from __future__ import annotations

import pytest

contextpilot = pytest.importorskip("contextpilot")


def test_official_package_returns_openai_messages_in_process() -> None:
    pilot = contextpilot.ContextPilot(use_gpu=False)
    contexts = ["alpha doc", "beta doc", "gamma doc"]

    messages = pilot.optimize(contexts, "Which doc is first?")

    assert [message["role"] for message in messages] == ["system", "user"]
    assert messages[1]["content"] == "Which doc is first?"
    for context in contexts:
        assert context in messages[0]["content"]
    assert pilot.get_stats()["num_requests"] == 1
