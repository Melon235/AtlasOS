# AtlasOS V2 M1 — Tool Runtime and Progressive Tool Discovery

Status: implemented and frozen at clean commit `fc9aa6d`. Results:
[v2-m1-results.md](../../experiments/results/v2-m1-results.md). V1 remains frozen; M1 adds a
tool-runtime layer around AnyCode rather than changing the execution engine.

## Goal

Provide one unified tool runtime and hide a large external catalog behind progressive discovery:

```text
core tools native -> tool.search -> Top-3 schemas in conversation tail
                   -> RunToolState -> dispatch_action -> AnyCode ToolExecutor -> real tool
```

The stable native prefix is preserved across a run for KV-cache locality.

## Architecture

```text
                       ProviderSafeToolRegistry (one registry)
                        /                         \
                 Core Tools                  Searchable Tools
                      |                             |
        native tools[] (stable)            ToolSearchIndex
                      |                             |
                      |                        tool_search
                      |                             |
                      |                       RunToolState
                      |                             |
                      +----------- model ----------+
                                    |
                             dispatch_action
                                    |
                         AtlasDispatchExecutor
                                    |
                          AnyCode ToolExecutor
                                    |
                            real tool / MCP
```

- **Registry**: AnyCode `ToolRegistry` subclass (`ProviderSafeToolRegistry`) owns definitions,
  schemas, execution callables, MCP tools, and executor resolution. Atlas never copies a
  `ToolDefinition`.
- **ToolCatalog**: exposure only. It owns `core_tool_names` and `searchable_tool_names`; it does
  not modify AnyCode `ToolDefinition`.
- **ToolSearchIndex**: capability retrieval over `tool_id + description`. It stores vectors and
  retrieval text, never definitions.
- **RunToolState**: per-run monotonic set of disclosed tool ids, persisted as a JSON sidecar.
- **AtlasDispatchExecutor**: thin routing wrapper around AnyCode's `ToolExecutor`; it checks
  disclosure/availability and delegates real execution to AnyCode.
- **Provider-safe naming**: model-facing names are sanitized to `[A-Za-z0-9_-]+` (DeepSeek/OpenAI
  constraint), while logical ids keep dots (`tool.search`, `github.issue.get`). The executor maps
  provider names back to logical ids before execution.

## Tool categories

Core tools (native for the whole run):

```text
bash, file_read, file_write, file_edit, grep, list_files   # AnyCode built-ins
tool.search (provider: tool_search), dispatch_action       # Atlas control tools
```

Searchable tools: MCP, RAG, business APIs, and any external registration. They are in the same
registry, in `searchable_tool_names`, and absent from native `tools[]`.

## Native-surface invariant

The native surface is `ToolCatalog.native_tool_names`, registered in sorted order. Its provider
serialization is hashed (name + description + schema). `tool.search` never mutates native
definitions; it only updates `RunToolState` and appends a Tool Result to the conversation. Tests
capture the model-facing tool list on every turn and require one identical list.

## Search

`tool_search(query)`:

1. retrieves Top-3 (configurable up to 3 by default);
2. looks up full definitions via `ToolRegistry.get`;
3. unions matched ids into `RunToolState`;
4. returns only `tool_id`, `description`, `input_schema` in the Tool Result.

No provider name, score, timeout, or risk metadata is exposed to the model; those stay in the
runtime trace. Ties are deterministic by `tool_id`.

## Dispatch

`dispatch_action(tool_id, arguments)`:

```text
published?  no -> tool_not_disclosed
registered? no -> tool_unavailable
otherwise       -> AnyCode ToolExecutor.execute(logical_tool_id, arguments, ...)
```

`arguments` is always a JSON object (no string double-encoding). The wrapper forwards the
runner's idempotency key to the target tool, so AnyCode's idempotency/security/validation/tracing
remain the single execution semantics.

## Initialization lifecycle

```text
AtlasRuntime(run) -> ToolRuntime
1. create ProviderSafeToolRegistry
2. register AnyCode built-ins + Atlas tools
3. register external definitions (same registry)
4. build ToolCatalog
5. build search index from core/searchable descriptions
6. create AnyCode ToolExecutor, wrap with AtlasDispatchExecutor
7. connect MCP servers through MCPClient + discover_and_register
8. build every agent with core provider names only
```

AnyCode's default `build_agent` creates a per-agent registry and auto-attaches MCP tools. Atlas
overrides only `build_agent`/`_build_task_prompt` on `AtlasContextEngine`; it does not copy the
scheduler, DAG, wave engine, or checkpoint engine.

## MCP integration

`AtlasToolRuntime.add_mcp_server(MCPServerConfig)` uses AnyCode's `MCPClient` and
`mcp.bridge.discover_and_register` into the one registry, then marks returned names searchable and
rebuilds the index. MCP tools never enter `AgentConfig.tools`; the agent gets core names only.
A real stdio MCP test server is run in `tests/fixtures/mcp_policy_server.py`.
Install `mcp<2` via the `tools` extra (AnyCode 0.9 targets MCP 1.x).

## Durability and recovery

Per-run sidecars under `runs/<run_id>/`:

- `run_tool_state.json` — schema version, run id, revision, sorted `dispatchable_tool_names`.
- `tool_conversations.json` — turn-boundary conversation per task.

`AtlasToolAgent` reads its task snapshot before `run`; `ConversationSnapshotHook` writes before
each model turn. On resume:

1. the task-level AnyCode checkpoint selects unfinished work;
2. `RunToolState` is restored before execution;
3. the in-flight task's conversation is restored (including its `tool.search` result);
4. the native tool surface is rebuilt from the same core set.

Disclosed names are historical facts. If an MCP server is unavailable after resume, the name stays
in `dispatchable_tool_names`; dispatch returns `tool_unavailable`, never `tool_not_disclosed`.
The sidecar is not a second checkpoint engine: AnyCode checkpoints remain execution truth.

## Evaluation summary (clean `fc9aa6d`)

- 24-tool retrieval: semantic Recall@1 0.875, Recall@3 1.000, MRR 0.938.
- Native surface: 8 core tools vs 32 full-exposure tools; schema chars 4,207 vs 8,558 (−50.8%).
- Live prompt tokens: 1,399 vs 2,599 (−46.2%); provider cache is warm-prone and uncontrolled, so
  no cache-hit improvement is claimed.
- All 15 M1 acceptance checks pass, including real stdio MCP discovery and dispatch.
- Recovery test: search → disclosure → crash → resume → dispatch succeeds; unavailable case
  returns `tool_unavailable`.

## Tests

```text
tests/unit/test_tools.py
tests/integration/test_tool_runtime.py
tests/integration/test_tool_mcp.py
tests/integration/test_tool_search_semantic.py
```

Coverage includes core visibility, hidden searchable/MCP tools, native immutability, Top-3 shape,
disclosure union, undisclosed rejection, per-run isolation, durable restore, unavailable tool,
real MCP discovery, provider-safe names, and semantic recall/determinism.

## M1 non-goals

No identity delegation, capability tokens, RBAC/ABAC, advanced policy engine, durable HITL,
sandbox worker, action ledger, advanced idempotency semantics, tool eviction/TTL, dynamic core
profiles, reranking, large vector DB, or business workflow. No second registry/executor/checkpoint
engine. ContextPilot reorder remains off the production path.

## Reproduce

```bash
uv sync --extra dev --extra tools
python examples/m1_demo.py
python examples/m1_tool_search_eval.py
python examples/m1_surface_eval.py
ATLASOS_RUN_LIVE_TESTS=1 python examples/m1_surface_eval.py
python -m pytest tests/unit/test_tools.py tests/integration/test_tool_runtime.py \
  tests/integration/test_tool_mcp.py tests/integration/test_tool_search_semantic.py
```

Formal records are written only from a clean worktree (`ATLASOS_M1_DRY_RUN=1` for a dry run).
