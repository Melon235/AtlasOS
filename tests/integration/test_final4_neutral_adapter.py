"""Final-4 R-10: native AnyCode accepts externally executed tools, and they really do the work.

What this proves: the six logical tools of the baseline can be executed by an external backend
(here an isolated directory + subprocess, which is the same interface a container backend supplies),
inside unmodified AnyCode 0.9.0, with no Atlas gate, ledger, context plane or budget.

What this does not prove: container parity. No sandbox credential exists in this workspace, so the
environment half stays ``BASELINE_PARITY_BLOCKED``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from anycode.types import AgentConfig, AgentInfo, ToolUseContext
from experiments.evaluation.final4.neutral_adapter import (
    NEUTRAL_TOOLS,
    LocalProcessBackend,
    NeutralEngine,
    neutral_tool_definitions,
)
from experiments.evaluation.neutral_env import adapter_status, capability_report


def _context(root: Path) -> ToolUseContext:
    return ToolUseContext(
        agent=AgentInfo(name="neutral", role="coder", model="fake"), cwd=str(root)
    )


def _tools(root: Path) -> dict[str, object]:
    backend = LocalProcessBackend(root=root)
    return {tool.name: tool for tool in neutral_tool_definitions(backend)}


async def test_every_neutral_tool_executes_inside_the_backend_root(tmp_path: Path) -> None:
    tools = _tools(tmp_path)
    context = _context(tmp_path)

    written = await tools["file_write"].execute(  # type: ignore[attr-defined]
        type("A", (), {"path": "pkg/mod.py", "content": "VALUE = 1\n"})(), context
    )
    assert written.is_error is False
    assert (tmp_path / "pkg" / "mod.py").read_text(encoding="utf-8") == "VALUE = 1\n"

    read = await tools["file_read"].execute(  # type: ignore[attr-defined]
        type("A", (), {"path": "pkg/mod.py"})(), context
    )
    assert read.is_error is False
    assert "VALUE = 1" in str(read.data)

    edited = await tools["file_edit"].execute(  # type: ignore[attr-defined]
        type("A", (), {"path": "pkg/mod.py", "old": "VALUE = 1", "new": "VALUE = 2"})(), context
    )
    assert edited.is_error is False
    assert "VALUE = 2" in (tmp_path / "pkg" / "mod.py").read_text(encoding="utf-8")

    listed = await tools["list_files"].execute(  # type: ignore[attr-defined]
        type("A", (), {"path": "pkg"})(), context
    )
    assert "mod.py" in str(listed.data)

    searched = await tools["grep"].execute(  # type: ignore[attr-defined]
        type("A", (), {"pattern": "VALUE", "path": "."})(), context
    )
    assert "mod.py" in str(searched.data)

    ran = await tools["bash"].execute(  # type: ignore[attr-defined]
        type("A", (), {"command": "echo SHELL-OK"})(), context
    )
    assert ran.is_error is False
    assert "SHELL-OK" in str(ran.data)


async def test_backend_refuses_to_escape_its_root(tmp_path: Path) -> None:
    tools = _tools(tmp_path)

    escaped = await tools["file_read"].execute(  # type: ignore[attr-defined]
        type("A", (), {"path": "../outside.txt"})(), _context(tmp_path)
    )

    assert escaped.is_error is True
    assert "escapes the workspace" in str(escaped.data)


def test_native_engine_builds_an_agent_with_exactly_the_six_tools(tmp_path: Path) -> None:
    backend = LocalProcessBackend(root=tmp_path)
    engine = NeutralEngine(backend)
    config = AgentConfig(
        name="neutral", model="fake", provider="fake", system_prompt="s", tools=list(NEUTRAL_TOOLS)
    )

    agent = engine.build_agent(config)

    assert {tool.name for tool in engine._neutral_registry.list()} == set(NEUTRAL_TOOLS)  # noqa: SLF001
    assert agent is not None


def test_environment_parity_is_still_reported_as_blocked(tmp_path: Path) -> None:
    assert capability_report().status == "BASELINE_PARITY_BLOCKED"
    adapter = adapter_status()
    assert adapter.status == "ADAPTER_READY_ENVIRONMENT_BLOCKED"
    assert adapter.missing


@pytest.mark.parametrize("tool_name", NEUTRAL_TOOLS)
def test_every_baseline_tool_name_is_present(tmp_path: Path, tool_name: str) -> None:
    assert tool_name in _tools(tmp_path)
