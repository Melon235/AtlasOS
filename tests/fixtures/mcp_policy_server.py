"""Minimal stdio MCP server used by AtlasOS M1 integration tests."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

server = FastMCP("atlas-policy")


@server.tool()
def lookup_policy(topic: str) -> str:
    """Look up an internal company policy by topic."""

    return f"Policy for {topic}: use stable prefixes and explicit context."


@server.tool()
def file_claim(claim_id: str) -> str:
    """File an insurance claim by claim id."""

    return f"Claim {claim_id} filed."


if __name__ == "__main__":
    server.run()
