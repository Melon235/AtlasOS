"""V2 M1 tool runtime: one registry, exposure catalog, searchable dispatch, durable state."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from anycode.types import AgentInfo, ToolDefinition, ToolResult, ToolUseContext
from pydantic import BaseModel

from atlasos.tools import (
    AtlasToolRuntime,
    MappingToolSearchIndex,
    RunToolStateStore,
    ToolCatalog,
    ToolStateError,
)
from atlasos.tools.catalog import DEFAULT_CORE_TOOL_NAMES
from atlasos.tools.search import build_entries


class EchoInput(BaseModel):
    text: str = ""


def make_tool(name: str, description: str, calls: list[tuple[str, str]]) -> ToolDefinition:
    async def execute(validated: EchoInput, context: ToolUseContext) -> ToolResult:
        del context
        calls.append((name, validated.text))
        return ToolResult(data=f"{name}:{validated.text}", is_error=False)

    return ToolDefinition(
        name=name,
        description=description,
        input_model=EchoInput,
        execute=execute,
        side_effecting=False,
    )


def context(tmp_path: Path) -> ToolUseContext:
    return ToolUseContext(
        agent=AgentInfo(name="tester", role="test", model="fake"),
        cwd=str(tmp_path),
    )


def test_core_tools_are_native_and_searchable_tools_are_hidden(tmp_path: Path) -> None:
    calls: list[tuple[str, str]] = []
    runtime = AtlasToolRuntime(
        "run-1",
        external_tools=(
            make_tool("atlas_rag.search", "Search internal policy documents.", calls),
            make_tool("github.issue.get", "Get a GitHub issue by number.", calls),
        ),
    )

    assert runtime.native_tool_names == tuple(sorted(DEFAULT_CORE_TOOL_NAMES))
    assert runtime.native_tool_names == runtime.catalog.native_tool_names
    assert "atlas_rag.search" not in runtime.native_tool_names
    assert "github.issue.get" not in runtime.native_tool_names
    assert runtime.registry.has("atlas_rag.search")
    assert set(runtime.catalog.searchable_tool_names) == {
        "atlas_rag.search",
        "github.issue.get",
    }
    assert runtime.state.dispatchable_tool_names == frozenset()
    runtime.catalog.validate(runtime.registry)


def test_tool_search_discloses_top_three_and_exposes_schema(tmp_path: Path) -> None:
    calls: list[tuple[str, str]] = []
    tools = tuple(
        make_tool(f"vendor.tool{i}", f"Capability number {i} for policy work.", calls)
        for i in range(5)
    )
    runtime = AtlasToolRuntime("run-2", external_tools=tools)
    query = "policy capability"
    entries = build_entries(runtime.registry, runtime.catalog)
    runtime.replace_search_index(
        MappingToolSearchIndex(
            {query: ("vendor.tool3", "vendor.tool1", "vendor.tool0")},
            entries,
        )
    )

    result = runtime.executor
    import asyncio

    search_result = asyncio.run(result.execute("tool.search", {"query": query}, context(tmp_path)))
    assert search_result.is_error is False
    payload = json.loads(search_result.data)
    assert [match["tool_id"] for match in payload["matches"]] == [
        "vendor.tool3",
        "vendor.tool1",
        "vendor.tool0",
    ]
    assert all(
        set(match) == {"tool_id", "description", "input_schema"} for match in payload["matches"]
    )
    assert all("score" not in match for match in payload["matches"])
    assert runtime.state.dispatchable_tool_names == frozenset(
        {"vendor.tool3", "vendor.tool1", "vendor.tool0"}
    )
    assert runtime.state.sorted_names == ("vendor.tool0", "vendor.tool1", "vendor.tool3")


def test_disclosed_tool_dispatches_and_undisclosed_tool_is_rejected(tmp_path: Path) -> None:
    import asyncio

    calls: list[tuple[str, str]] = []
    runtime = AtlasToolRuntime(
        "run-3",
        external_tools=(make_tool("atlas_rag.search", "Search internal policy.", calls),),
    )
    entries = build_entries(runtime.registry, runtime.catalog)
    runtime.replace_search_index(MappingToolSearchIndex({"policy": ("atlas_rag.search",)}, entries))
    ctx = context(tmp_path)

    denied = asyncio.run(
        runtime.executor.execute(
            "dispatch_action",
            {"tool_id": "atlas_rag.search", "arguments": {"text": "before"}},
            ctx,
        )
    )
    assert denied.is_error is True
    assert denied.data.startswith("tool_not_disclosed")

    asyncio.run(runtime.executor.execute("tool.search", {"query": "policy"}, ctx))
    accepted = asyncio.run(
        runtime.executor.execute(
            "dispatch_action",
            {"tool_id": "atlas_rag.search", "arguments": {"text": "after"}},
            ctx,
        )
    )
    assert accepted.is_error is False
    assert accepted.data == "atlas_rag.search:after"
    assert calls == [("atlas_rag.search", "after")]


def test_dispatchable_set_is_monotonic_and_native_surface_is_immutable(tmp_path: Path) -> None:
    import asyncio

    calls: list[tuple[str, str]] = []
    runtime = AtlasToolRuntime(
        "run-4",
        external_tools=(
            make_tool("atlas_rag.search", "Search internal policy.", calls),
            make_tool("github.issue.get", "Get a GitHub issue.", calls),
        ),
    )
    entries = build_entries(runtime.registry, runtime.catalog)
    runtime.replace_search_index(
        MappingToolSearchIndex(
            {"policy": ("atlas_rag.search",), "github": ("github.issue.get",)},
            entries,
        )
    )
    native_before = runtime.native_tool_names
    hash_before = runtime.native_surface_hash()
    ctx = context(tmp_path)

    asyncio.run(runtime.executor.execute("tool.search", {"query": "policy"}, ctx))
    after_first = runtime.state.dispatchable_tool_names
    asyncio.run(runtime.executor.execute("tool.search", {"query": "github"}, ctx))
    after_second = runtime.state.dispatchable_tool_names

    assert after_first == frozenset({"atlas_rag.search"})
    assert after_second == frozenset({"atlas_rag.search", "github.issue.get"})
    assert runtime.native_tool_names == native_before
    assert runtime.native_surface_hash() == hash_before


def test_same_disclosure_twice_does_not_grow_or_save_again(tmp_path: Path) -> None:
    import asyncio

    calls: list[tuple[str, str]] = []
    state_path = tmp_path / "run_tool_state.json"
    runtime = AtlasToolRuntime(
        "run-5",
        state_path=state_path,
        external_tools=(make_tool("atlas_rag.search", "Search policy.", calls),),
    )
    entries = build_entries(runtime.registry, runtime.catalog)
    runtime.replace_search_index(MappingToolSearchIndex({"policy": ("atlas_rag.search",)}, entries))
    ctx = context(tmp_path)

    asyncio.run(runtime.executor.execute("tool.search", {"query": "policy"}, ctx))
    revision = runtime.state.revision
    asyncio.run(runtime.executor.execute("tool.search", {"query": "policy"}, ctx))
    assert runtime.state.revision == revision
    assert runtime.state.dispatchable_tool_names == frozenset({"atlas_rag.search"})


def test_disclosure_state_is_per_run_and_restored_from_sidecar(tmp_path: Path) -> None:
    import asyncio

    calls: list[tuple[str, str]] = []
    state_path = tmp_path / "state" / "run_tool_state.json"
    tool = make_tool("atlas_rag.search", "Search internal policy documents.", calls)
    first = AtlasToolRuntime("run-6", state_path=state_path, external_tools=(tool,))
    entries = build_entries(first.registry, first.catalog)
    first.replace_search_index(MappingToolSearchIndex({"policy": ("atlas_rag.search",)}, entries))
    ctx = context(tmp_path)
    asyncio.run(first.executor.execute("tool.search", {"query": "policy"}, ctx))

    restored = AtlasToolRuntime("run-6", state_path=state_path, external_tools=(tool,))
    restored.replace_search_index(
        MappingToolSearchIndex({"policy": ("atlas_rag.search",)}, entries)
    )
    assert restored.state.dispatchable_tool_names == frozenset({"atlas_rag.search"})
    result = asyncio.run(
        restored.executor.execute(
            "dispatch_action",
            {"tool_id": "atlas_rag.search", "arguments": {"text": "resumed"}},
            ctx,
        )
    )
    assert result.is_error is False

    other = AtlasToolRuntime("run-7", external_tools=(tool,))
    assert other.state.dispatchable_tool_names == frozenset()


def test_disclosed_but_unavailable_tool_returns_tool_unavailable(tmp_path: Path) -> None:
    import asyncio

    calls: list[tuple[str, str]] = []
    state_path = tmp_path / "run_tool_state.json"
    tool = make_tool("atlas_rag.search", "Search policy.", calls)
    first = AtlasToolRuntime("run-8", state_path=state_path, external_tools=(tool,))
    entries = build_entries(first.registry, first.catalog)
    first.replace_search_index(MappingToolSearchIndex({"policy": ("atlas_rag.search",)}, entries))
    ctx = context(tmp_path)
    asyncio.run(first.executor.execute("tool.search", {"query": "policy"}, ctx))

    without_server = AtlasToolRuntime("run-8", state_path=state_path)
    result = asyncio.run(
        without_server.executor.execute(
            "dispatch_action",
            {"tool_id": "atlas_rag.search", "arguments": {"text": "later"}},
            ctx,
        )
    )
    assert result.is_error is True
    assert result.data.startswith("tool_unavailable")
    assert without_server.state.is_dispatchable("atlas_rag.search")


def test_state_schema_and_run_identity_are_enforced(tmp_path: Path) -> None:
    state_path = tmp_path / "run_tool_state.json"
    state_path.write_text(
        json.dumps(
            {
                "schema_version": 99,
                "run_id": "run-9",
                "dispatchable_tool_names": [],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ToolStateError, match="unsupported"):
        RunToolStateStore(state_path).load(run_id="run-9")

    state_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "run_id": "another-run",
                "dispatchable_tool_names": [],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ToolStateError, match="another-run"):
        RunToolStateStore(state_path).load(run_id="run-9")


def test_catalog_rejects_overlap_and_missing_registry_entries(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="both core and searchable"):
        ToolCatalog(
            core_tool_names=frozenset({"a"}),
            searchable_tool_names=frozenset({"a"}),
        )

    runtime = AtlasToolRuntime("run-10")
    bad = ToolCatalog(
        core_tool_names=frozenset({"missing"}),
        searchable_tool_names=frozenset(),
    )
    with pytest.raises(ValueError, match="unregistered"):
        bad.validate(runtime.registry)


def test_core_tool_still_executes_through_anycode_executor(tmp_path: Path) -> None:
    import asyncio

    (tmp_path / "marker.txt").write_text("ok", encoding="utf-8")
    runtime = AtlasToolRuntime("run-11")
    result = asyncio.run(
        runtime.executor.execute(
            "list_files",
            {"path": str(tmp_path), "max_results": 10},
            context(tmp_path),
        )
    )
    assert result.is_error is False
    assert "marker.txt" in result.data


def test_provider_safe_names_preserve_logical_tool_ids() -> None:
    runtime = AtlasToolRuntime(
        "run-12",
        external_tools=(make_tool("atlas_rag.search", "Search policy.", []),),
    )

    assert "tool.search" in runtime.native_tool_names
    assert "tool_search" in runtime.native_provider_tool_names
    assert "tool_search" in runtime.agent_tools
    assert "atlas_rag.search" in runtime.catalog.searchable_tool_names
    assert runtime.registry.logical_name("atlas_rag_search") == "atlas_rag.search"
    assert runtime.registry.provider_name("tool.search") == "tool_search"
    assert all(name.replace("_", "").replace("-", "").isalnum() for name in runtime.agent_tools)


async def test_search_is_capped_at_configured_top_k() -> None:
    tools = tuple(
        make_tool(f"vendor.tool{index}", f"Policy capability {index}.", []) for index in range(5)
    )
    runtime = AtlasToolRuntime("run-13", external_tools=tools, max_results=3)
    result = await runtime.search("policy capability")
    payload = json.loads(result.data)
    assert len(payload["matches"]) == 3
    assert runtime.state.dispatchable_tool_names == frozenset(
        match["tool_id"] for match in payload["matches"]
    )
