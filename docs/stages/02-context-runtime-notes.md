# Stage 2 Notes — Explicit Context Runtime and Stable Rendering

Recorded 2026-10-22. Baseline commits: Stage 0 `98cedb8`, Stage 1 `7eae2c1`.

## Integration seam

AnyCode 0.9.0 has no public prompt/context hook:

- `TurnHook.before_turn` can rewrite messages, but its `AgentInfo` carries no task identity, so
  AtlasOS could not know which task's dependencies to select without parsing prompt text.
- Dependency injection happens in the private `AnyCode._build_task_prompt(task, queue)`, called
  from `_run_wave_task`.

AtlasOS therefore uses the smallest available seam: `AtlasContextEngine(AnyCode)` overrides
**only** `_build_task_prompt` and delegates to a runtime-provided builder. Scheduling, waves,
lifecycle, concurrency, and checkpointing stay upstream. No upstream patch, no copied
`AgentRunner`, no second executor. The upstream version is pinned to 0.9.0; a future upgrade must
re-verify this symbol before trusting the seam.

## Data model

`ContextKind` (system/task/dependency_output/message/tool_result/artifact/memory/summary),
`ContextBlock` (id, kind, content, version, source, task_id, owner_agent_id), and `ContextRef`
(block id + version). No embeddings, TTL, residency, scores, or transaction state.

`InMemoryContextStore` keeps immutable versions per block id: `get` returns the newest version,
`get_many` resolves exact versions in reference order, `find_by_task` returns versions in order.
Missing blocks raise `KeyError` rather than silently degrading. The store is a logical
projection; AnyCode checkpoints remain the source of truth for task execution.

## Selection vs rendering

`TaskScopedSelector` decides membership only: the current task block plus outputs of **direct**
dependencies. It receives `available_refs` in the caller's projection order (completion order) and
never reorders for cache reuse.

`PromptRenderer` owns serialization:

| Renderer | Ordering |
|---|---|
| `NaiveExecutionOrderRenderer` | preserves incoming/completion order |
| `StablePromptRenderer` | sorts by kind priority, stable task id, version, block id |

Stable kind priority puts the task block before dependency outputs. The rendered body uses fixed
delimiters and no timestamps:

```text
Task: Review

<task description>

<dependency task_id="research-a" version="1">
...
</dependency>
```

## Fingerprints

- `stable_prefix_hash`: system prompt + tool definitions (existing metric).
- `context_layout_hash`: SHA-256 over `[(block_id, version, position)]`.
- `full_prompt_hash`: system prompt + rendered messages (existing metric).

Layout hashes are recorded per inference call and persisted; none of these values enter prompts.

## Structure token accounting

`stable_prefix_tokens`, `context_block_tokens`, and `dynamic_tail_tokens` are recorded as `None`.
No tokenizer is configured in Stage 2, and character heuristics would be fabricated precision.
The fields exist for Stage 3 to populate with a real tokenizer.

## Modes and fail-fast rules

Only these pairs are accepted; anything else fails at configuration load:

| Variant | policy | renderer |
|---|---|---|
| U0 | `upstream_default` | `upstream_default` |
| C1 | `task_scoped` | `naive_execution_order` |
| C2 | `task_scoped` | `stable_deterministic` |

`scheduler_policy` must remain `upstream_default`; `contextpilot_enabled` must stay false.

## Offline verification

`91 passed` includes inverted-delay runtime tests: with 10 ms/80 ms client-side delays, C1's
layout hash changes when completion order flips, while C2's layout hash, rendered order, and full
prompt hash stay identical. Selector tests confirm membership is independent of arrival order;
renderer tests cover delimiters, versioned refs, and content-vs-layout hash behavior.

## Live DeepSeek runs

Six runs, 10 ms–900 ms client-side delays to force order inversion (delay is excluded from
provider latency):

| experiment | completion order | rendered order | layout hash | prompt | hit | miss |
|---|---|---|---|---|---|---|
| u0 a-first | n/a | n/a | n/a | 894 | 128 | 766 |
| u0 b-first | n/a | n/a | n/a | 919 | 128 | 791 |
| c1 a-first | a, b | a, b | `ded6ce53…` | 897 | 128 | 769 |
| c1 b-first | b, a | b, a | `a97dffed…` | 912 | 128 | 784 |
| c2 a-first | a, b | a, b | `ded6ce53…` | 880 | 128 | 752 |
| c2 b-first | b, a | a, b | `ded6ce53…` | 875 | 128 | 747 |

All six runs succeeded. C1 demonstrably leaks completion order into serialization; C2 does not.
C2 full-prompt hashes differ across runs only because the dependency outputs themselves differ
(different model generations), not because of ordering.

Cache hits stayed at 128 tokens for every run: the shared system-prefix blocks, unaffected by
layout. Stage 2 makes no cache claim.

## Known limitations

- The seam is a private upstream method (`_build_task_prompt`); pinned-version coupling is real
  and must be re-checked on AnyCode upgrades.
- `context_layout_hash` includes the task block position, so it hashes full layout, not only
  dependency order.
- Order inversion in live runs relies on injected client-side delays; it is a controlled
  experiment, not a natural workload property.
- Token structure metrics remain `None` until a tokenizer is adopted.
- Provider prompt caching remains best-effort; no positive hit count is asserted anywhere.

## Exit checklist

- [x] ContextBlock/ContextRef exist
- [x] ContextStore exists
- [x] TaskScopedSelector exists
- [x] selection and rendering separated
- [x] naive execution-order renderer exists
- [x] stable deterministic renderer exists
- [x] completion order inverted in tests and live runs
- [x] stable layout identical under inversion
- [x] `context_layout_hash` recorded
- [x] DeepSeek metrics intact (hit/miss captured in all runs)
- [x] `upstream_default` baseline runnable (U0)
- [x] ContextPilot still disabled
- [x] no custom Atlas scheduler
- [x] Stage 0/1 regression tests green
- [x] ruff, format, pyright, pytest pass
- [x] live Stage 2 experiment recorded
- [x] Stage 2 findings documented
