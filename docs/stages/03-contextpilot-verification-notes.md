# Stage 3 Verification Notes — ContextPilot 0.5.0 API

Inspected the installed `contextpilot==0.5.0` before and after integration.

## Reorder semantic correction

**Original assumption (wrong):** `reorder()` was treated as a per-request block reordering
operation, interchangeable with `dedup_chat_completions()` inside one `ContextOptimizer`.

**What the pinned API actually does:**

```python
reorder(contexts, initial_tokens_per_context=0, conversation_id=None)
    -> (reordered_contexts, original_indices)
```

- `contexts` is a batch of requests (`list[list[str]]`).
- `original_indices` is the **request execution order**, not a per-document permutation.
- Per-request document order is returned, but there is no index map from returned documents back
  to input documents; mapping by content is ambiguous for duplicates and was the failure seen in
  the earlier stress attempt.
- A single-request call returns the input order unchanged.

**Correction:** AtlasOS now defines two separate seams:

```text
per-request ContextTransform  -> identity | ContextPilot dedup
batch BatchRequestPolicy      -> identity | ContextPilot reorder (experiment-only)
```

Reorder no longer lives in the runtime path and never rewrites Atlas-rendered prompts; a batch
policy may only change request execution order.

## Dedup API (used)

```python
contextpilot.dedup_chat_completions(body, min_block_chars=40, min_content_chars=200,
                                    chunk_modulus=13, system_content=None) -> DedupResult
```

- Mutates `body["messages"]` in place; returns counters (`blocks_total`, `blocks_deduped`,
  `chars_saved`).
- Exact block-hash matches only; replacements are textual hints such as
  `[... "first line" — identical to earlier user message result, see above ...]`.
- Content must chunk into two or more blocks to be eligible (multi-line documents); a single
  long line is never shortened.
- The adapter passes one message per context block, so block count and ids are preserved while
  only content changes.

## Rejected API

`optimize(docs, query, ...)` returns a complete message list and would replace AtlasOS's stable
system/task prefix. It is not used.

## Index semantics

Dedup is stateless and creates no index. Reorder's index state is per instance and not provider
cache state; all cache claims use provider-reported hit/miss tokens only.

## Reorder CPU viability

Bounded subprocess benchmark (`experiments/benchmarks/reorder_cpu.py`, 10 s timeout per case):

| case | requests | blocks/req | chars | reorder ms | timed out |
|---|---:|---:|---:|---:|---|
| c1 | 2 | 2 | 8,352 | 1,119.8 | no |
| c2 | 2 | 3 | 12,528 | 1,102.5 | no |
| c3 | 3 | 3 | 18,792 | 1,140.1 | no |
| c4 | 4 | 4 | 33,408 | 1,114.0 | no |
| c5 | 2 | 2 | 16,112 | 1,133.3 | no |
| c6 | 3 | 3 | 36,252 | 1,165.5 | no |

Even the smallest batch costs about **1.1 s**, almost independent of size up to ~36 K chars,
which matches one-time index/initialization overhead. The earlier stress batch (104 K chars,
including an exact duplicate document) did not complete in over three minutes in two probes,
so cost is not linear at the real scale.

**Classification: C — impractical for the AtlasOS V1 online CPU path.** It may be usable for
small offline batch studies, but it is not included in the runtime path.
