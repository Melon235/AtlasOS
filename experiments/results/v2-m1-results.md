# AtlasOS V2 M1 — Tool Runtime and Progressive Discovery Results

Frozen from clean code commit `fc9aa6d` (`git_dirty=false` in every formal record). Raw data:
`experiments/runs/v2-m1-tool-search.jsonl`, `experiments/runs/v2-m1-surface.jsonl` (gitignored).

## Semantic tool retrieval (24-tool fixed catalog, 24 capability queries)

| backend | Recall@1 | Recall@3 | MRR |
|---|---:|---:|---:|
| keyword (baseline) | 0.792 | 0.958 | 0.861 |
| SentenceTransformers + FAISS | **0.875** | **1.000** | **0.938** |

Model: `all-MiniLM-L6-v2` on CPU, retrieval text = `tool_id + description`, cosine inner product
in a FAISS `IndexFlatIP`. The same catalog/query set returns identical results on repeat runs.
Semantic misses are rank 2 only: `github.issue.get`, `jira.ticket.search`, `docs.read`.

## Native tool surface footprint

Workload: 24 external tools plus 8 core tools.

| measurement | full exposure | Atlas progressive | change |
|---|---:|---:|---:|
| native tools | 32 | **8** | −75.0% |
| native schema chars (deterministic) | 8,558 | **4,207** | −50.8% |
| live prompt tokens (`deepseek-chat`) | 2,599 | **1,399** | −46.2% |
| native surface hash stable after search | — | **true** | required invariant |

The native surface is core tools only and its hash is identical before and after `tool.search`.
No external tool schema enters `tools[]`; discovered schemas appear only in the Tool Result in
the conversation tail.

## Live provider cache observation (warm-prone, uncontrolled)

| variant | reps | prompt | hit | miss | weighted hit | mean latency |
|---|---:|---:|---:|---:|---:|---:|
| full exposure | 3 | 2,599 | 2,432 | 167 | 0.936 | 662 ms |
| Atlas progressive | 3 | 1,399 | 1,152 | 247 | 0.823 | 791 ms |

The provider cache was already warm and is not cold-controlled. The smaller native surface
lowers prompt tokens by 46.2%; it does **not** show a cache-hit improvement in this microbenchmark
(the shorter surface has a lower weighted hit ratio because its prefix differs). No cache claim
beyond the footprint and stability measurements is made.

## Integration acceptance

| Check | Result |
|---|---|
| One ToolRegistry holds core and searchable tools | pass |
| Core tools native-visible | pass |
| Searchable/MCP tools registered but hidden | pass |
| Native tool surface immutable across model turns | pass |
| `tool.search` returns Top-3 with `tool_id`, `description`, `input_schema` | pass |
| Search results union monotonically into per-run dispatchable set | pass |
| Search result appears in conversation tail, not native tools | pass |
| Disclosed tool dispatches through AnyCode ToolExecutor | pass |
| Undisclosed tool id is rejected (`tool_not_disclosed`) | pass |
| Per-run dispatchable isolation | pass |
| Disclosure survives task-level crash/resume | pass |
| Disclosed but unavailable tool returns `tool_unavailable` | pass |
| Real stdio MCP discovery, hidden by default, dispatched successfully | pass |
| Duplicate/guessed searchable tool cannot bypass disclosure | pass |
| MCP tool never auto-enters native `tools[]` | pass |

Deterministic end-to-end demo:

```bash
python examples/m1_demo.py
```

It shows the same 8-tool native surface on all three model turns, `tool_search` discovering
`atlas_rag.search`, `dispatch_action` executing it, and the final model answer.

## Recovery scenario

```text
search -> disclose atlas_rag.search -> crash before dispatch
checkpoint -> resume -> dispatch atlas_rag.search -> success
```

The task-level regression test captures the pre-crash checkpoint, restores the dispatchable set
and turn-boundary conversation, and confirms the resumed in-flight task sees the original
`tool.search` result. When the resumed runtime does not register the disclosed tool, dispatch
returns `tool_unavailable` (not `tool_not_disclosed`) and the historical set is not rewritten.

## Reproduce

```bash
python examples/m1_tool_search_eval.py
python examples/m1_surface_eval.py
ATLASOS_RUN_LIVE_TESTS=1 python examples/m1_surface_eval.py
python examples/m1_demo.py
```

Install the M1 extras for semantic retrieval and real MCP:

```bash
uv sync --extra dev --extra tools
```

## Limitations

- Provider function names cannot contain dots, so model-facing core names are provider-safe:
  `tool_search` (logical `tool.search`) and `dispatch_action`. Search results and dispatch
  targets use the logical dotted ids.
- The live cache sample is warm-prone and uncontrolled; no cold-start claim.
- Task-level recovery restores the turn-boundary conversation and disclosure state, but AnyCode
  remains execution truth; the sidecar is not a second checkpoint engine.
- A crash *during* a tool call after disclosure but before the next turn snapshot is a narrow
  edge; the disclosure set is still durable, but the conversation may not yet contain the result.
- The surface microbenchmark measures tool schemas only; it is not a full task workload.
