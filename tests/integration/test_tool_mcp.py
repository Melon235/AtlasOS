"""V2 M1 MCP integration: discovery into the one registry, hidden by default, dispatchable."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from anycode.types import AgentInfo, MCPServerConfig, ToolUseContext

from atlasos.tools import AtlasToolRuntime
from atlasos.tools.catalog import DEFAULT_CORE_TOOL_NAMES

pytest.importorskip("mcp")

SERVER = Path(__file__).resolve().parents[1] / "fixtures" / "mcp_policy_server.py"


def _context(tmp_path: Path) -> ToolUseContext:
    return ToolUseContext(
        agent=AgentInfo(name="mcp-tester", role="test", model="fake"),
        cwd=str(tmp_path),
    )


async def test_mcp_tools_are_searchable_but_not_native(tmp_path: Path) -> None:
    config = MCPServerConfig(
        name="policy",
        transport="stdio",
        command=sys.executable,
        args=[str(SERVER)],
        timeout=30.0,
    )
    runtime = AtlasToolRuntime("mcp-run", search_backend="keyword")
    try:
        names = await runtime.add_mcp_server(config)
        assert names
        assert set(names) == set(runtime.catalog.searchable_tool_names)
        assert runtime.registry.has("mcp_policy_lookup_policy")
        assert "mcp_policy_lookup_policy" not in runtime.native_tool_names
        assert runtime.native_tool_names == tuple(sorted(DEFAULT_CORE_TOOL_NAMES))

        search = await runtime.executor.execute(
            "tool.search",
            {"query": "internal company policy topic"},
            _context(tmp_path),
        )
        assert search.is_error is False
        assert "mcp_policy_lookup_policy" in search.data
        assert runtime.state.is_dispatchable("mcp_policy_lookup_policy")

        dispatch = await runtime.executor.execute(
            "dispatch_action",
            {
                "tool_id": "mcp_policy_lookup_policy",
                "arguments": {"topic": "travel"},
            },
            _context(tmp_path),
            idempotency_key="mcp-test-1",
        )
        assert dispatch.is_error is False
        assert "Policy for travel" in dispatch.data

        replay = await runtime.executor.execute(
            "dispatch_action",
            {
                "tool_id": "mcp_policy_lookup_policy",
                "arguments": {"topic": "travel"},
            },
            _context(tmp_path),
            idempotency_key="mcp-test-1",
        )
        assert replay.is_error is False
        assert replay.data == dispatch.data

        assert runtime.metrics().mcp_servers == ("policy",)
        assert runtime.metrics().dispatch_count == 2
    finally:
        await runtime.close()
