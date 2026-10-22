# Stage 3 Verification Notes — ContextPilot 0.5.0 API

Inspected the installed `contextpilot==0.5.0` before writing the adapter.

## Constructor

```python
ContextPilot(alpha=0.001, use_gpu=False, linkage_method="average", batch_size=10000)
```

No server, GPU, vLLM, or SGLang is required for in-process use. Initialization prints progress
banners to stdout; the adapter suppresses them.

## `reorder`

```python
reorder(contexts, initial_tokens_per_context=0, conversation_id=None)
    -> (reordered_contexts, original_indices)
```

- `contexts` is a batch: `list[list[str]]` (a single list is auto-wrapped).
- Returned `reordered_contexts[i]` is the reordered document list for request `i`.
- `original_indices` is the **request execution order**, not a per-document permutation.
- There is therefore no per-document index map. The adapter maps reordered text back to blocks
  by exact content, consuming duplicate contents in first-seen order.
- Reorder is only meaningful across a batch of requests; a single-context call returns the input
  unchanged (observed on the functional workload).
- Index state is per instance and per `conversation_id`; the runtime creates one optimizer per
  run (`warm_within_run`).

## `deduplicate`

`ContextPilot.deduplicate(contexts, conversation_id, hint_template=None)` is **cross-turn**
deduplication: it reports `new_docs` / `overlapping_docs` / `reference_hints` against documents
already seen in earlier turns. It does not remove duplicates inside one prompt.

The relevant within-payload API is:

```python
contextpilot.dedup_chat_completions(body, min_block_chars=40, min_content_chars=200,
                                    chunk_modulus=13, system_content=None) -> DedupResult
```

- It **mutates** `body["messages"]` in place and returns stats
  (`blocks_total`, `blocks_deduped`, `chars_saved`, …).
- Only exact block-hash matches are replaced; the replacement is a textual reference hint such as
  `[... "first line" — identical to earlier user message result, see above ...]`.
- Blocks are content-defined chunks of lines; content with fewer than two chunks is never
  deduplicated. Single-line duplicate strings are therefore untouched.
- The adapter passes one user message per context block, so message count and block identity stay
  one-to-one while only content changes.

## `optimize`

`optimize(docs, query, *, conversation_id=None, system_instruction=None)` returns a complete
OpenAI message list (one system message containing all docs, one user message). It is **not**
used: it would replace the Atlas stable system/task prefix.

## Context index semantics

The ContextPilot index is an optimization hint. It does not represent DeepSeek's provider cache.
All cache claims in Stage 3 use provider-reported `prompt_cache_hit_tokens` /
`prompt_cache_miss_tokens` only.

## Cost observation

Batched `reorder` on the Stage 3 stress scale (4 requests × 4 blocks × ~5.5 KB) did not complete
within three minutes of CPU time in two probes. Single-context reorder is effectively free
(< 1 ms). This shaped the experiment plan: reorder effects are exercised offline/by API, while
live stress ran identity and dedup variants.
