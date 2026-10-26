# V2 M1 Acceptance Matrix

Status: **PASS** at code commit `fc9aa6d` (evaluation phase) / current documentation commit.
Every item below maps to executable tests in the repository.

## T01–T17 checklist

| ID | Check | Test |
|---|---|---|
| T01 | Single shared `ToolRegistry` across agents | `tests/integration/test_m1_acceptance.py::test_t01_one_shared_registry_across_agents` |
| T02 | Core tools visible | `test_t01...` / `test_t02_t03_core_visible_and_searchable_hidden` |
| T03 | Searchable tools hidden | `test_t02_t03...` / `tests/unit/test_tools.py::test_core_tools_are_native_and_searchable_tools_are_hidden` |
| T04 | Native surface stable across turns | `tests/integration/test_tool_runtime.py::test_native_surface_is_core_only_stable_and_search_dispatch_works`; final scenario captures every turn |
| T05 | Search returns Top-3 | `tests/unit/test_tools.py::test_search_is_capped_at_configured_top_k` |
| T06 | Search adds results to dispatchable set | `tests/unit/test_tools.py::test_tool_search_discloses_top_three_and_exposes_schema` |
| T07 | Disclosed tool dispatches | `tests/unit/test_tools.py::test_disclosed_tool_dispatches_and_undisclosed_tool_is_rejected` |
| T08 | Guessed undisclosed tool rejected | same + `tests/integration/test_tool_runtime.py::test_undisclosed_guess_is_rejected_by_the_executor` |
| T09 | Dispatchable set monotonic union | `tests/unit/test_tools.py::test_dispatchable_set_is_monotonic_and_native_surface_is_immutable` |
| T10 | Per-run state isolation | `tests/unit/test_tools.py::test_disclosure_state_is_per_run_and_restored_from_sidecar` |
| T11 | Dispatch delegates to AnyCode executor | `test_disclosed_tool_dispatches...`; `tests/integration/test_tool_mcp.py::test_mcp_tools_are_searchable_but_not_native` (idempotency replay) |
| T12 | MCP registered but hidden | `test_mcp_tools_are_searchable_but_not_native` |
| T13 | MCP search + dispatch | `test_mcp_tools_are_searchable_but_not_native` |
| T14 | Checkpoint restores dispatchable state | `test_resume_restores_disclosure_and_dispatches_after_crash` |
| T15 | Disclosed missing tool → `tool_unavailable` | `test_disclosed_but_unavailable_tool_returns_tool_unavailable`; `test_resume_keeps_disclosure_when_tool_is_unavailable` |
| T16 | Search does not modify native tools | `test_dispatchable_set_is_monotonic_and_native_surface_is_immutable` |
| T17 | Stable ordering / deterministic search | `test_t17_deterministic_core_order_and_tied_search_order`; `tests/integration/test_tool_search_semantic.py::test_semantic_search_recall_and_determinism` |

Final end-to-end acceptance scenario (core-only → search RAG → dispatch → guess email rejected
→ search email → dispatch → native tools unchanged):
`tests/integration/test_m1_acceptance.py::test_final_acceptance_scenario`.

## Architecture veto review

| Veto | Status | Evidence |
|---|---|---|
| ❌ second ToolRegistry | not present | T01 asserts both agents share `AtlasToolRuntime.registry` |
| ❌ searchable tool dynamically added to native `tools[]` | not present | T04/T16 hash and per-turn provider list |
| ❌ MCP tools all exposed | not present | T12/T13 native list excludes MCP names |
| ❌ dispatch can run undisclosed tool | not present | T08/error `tool_not_disclosed` |
| ❌ global dispatchable set | not present | T10 per-run sidecar/run id |
| ❌ Atlas copies ToolExecutor | not present | `AtlasDispatchExecutor` delegates to `anycode.tools.executor.ToolExecutor` |
| ❌ Atlas calls `real_tool.execute()` | not present | dispatch calls `self._inner_executor.execute(...)` |
| ❌ complex auth/HITL for M1 | not present | none added |
| ❌ TTL/eviction/profile | not present | monotonic set only |
| ❌ search index as second source of truth | not present | index stores `vector/tool_id/retrieval_text`; schema comes from `ToolRegistry.get` |
| ❌ disclosure lost after checkpoint | not present | T14 restores sidecar state before execution |
| ❌ unstable serialization order | not present | sorted core names, provider-safe mapping, tie-by-id |

## One-sentence invariant

```text
REGISTERED != VISIBLE != DISCLOSED != EXECUTABLE BY GUESS
```

Searchable tools must pass `registered → searched → disclosed → dispatchable → AnyCode
ToolExecutor → executed`, while the core native surface stays identical for the whole run.

## Reproduce

```bash
python -m pytest \
  tests/integration/test_m1_acceptance.py \
  tests/unit/test_tools.py \
  tests/integration/test_tool_runtime.py \
  tests/integration/test_tool_mcp.py \
  tests/integration/test_tool_search_semantic.py
```
