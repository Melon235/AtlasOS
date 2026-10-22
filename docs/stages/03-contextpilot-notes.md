# Stage 3 Notes — ContextPilot Integration

Recorded 2026-10-22. Baseline commits: Stage 0 `98cedb8`, Stage 1 `7eae2c1`, Stage 2 `34ea504`.

## Boundary

`ContextOptimizer` sits between `TaskScopedSelector` and the renderer:

```text
selector (membership) -> stable pre-order -> optimizer -> renderer (preserve optimizer order)
```

The selector output is sorted with the Stage 2 stable key **before** optimization, so
`pre_optimizer_layout_hash` is deterministic regardless of parallel completion order. The stable
renderer is constructed with `preserve_input_order=True` so ContextPilot's deliberate reorder is
not sorted back. The stable system/task prefix and delimiters remain Atlas-owned.

Optimizers never select blocks, render text, call providers, or schedule tasks.

## Modes

| Variant | optimizer | reorder | dedup |
|---|---|---:|---:|
| P0 | identity | false | false |
| P1 | contextpilot | true | false |
| P2 | contextpilot | false | true |
| P3 | contextpilot | true | true |

Configuration fails fast on `identity` with flags enabled, and on ContextPilot without
`policy=task_scoped` + `renderer=stable_deterministic`.

## Dependency isolation

No top-level ContextPilot import. The adapter imports lazily on first use; a missing package
raises `ContextPilotUnavailable` and the identity path continues to work. Initialization failures
are surfaced, never silently downgraded to identity.

## Metrics added

Per call: optimizer name/flags, optimizer latency, original/optimized block counts and context
chars, pre/post optimizer layout hashes, index mode. Per run: same labels plus total optimizer
latency, AnyCode/ContextPilot versions, `git_dirty`.

## Live functional results (P0–P3, one run each)

| variant | status | prompt | hit | miss | optimizer latency |
|---|---|---|---:|---:|---:|
| P0 identity | succeeded | 940 | 128 | 812 | ~0 ms |
| P1 reorder | succeeded | 927 | 128 | 799 | 0.6 ms |
| P2 dedup | succeeded | 901 | 128 | 773 | 0.6 ms |
| P3 full | succeeded | 900 | 128 | 772 | 0.6 ms |

All tasks completed; stable prefix hashes are equal across variants; P0 pre/post layout hashes are
equal. On this short workload reorder is a no-op (single-context calls) and dedup cannot trigger
(fewer than two chunks per block).

## Live cache-stress results

Deterministic blocks (`small`, ~5 KB each; E is an exact copy of A), 4 requests per repetition,
2 repetitions. Provider latency and cache tokens come from DeepSeek.

| variant | calls | prompt tokens | hit | miss | notes |
|---|---:|---:|---:|---:|---|
| P0 identity | 8 | 39,736 | 18,944 | 20,792 | no optimization |
| P2 dedup | 8 | 36,744 | 33,280 | 3,464 | request 4 drops from 4,967 to 3,471 prompt tokens |
| P1 reorder | — | — | — | — | batched reorder did not finish in >3 min (two probes) |
| P3 full | — | — | — | — | blocked by the same reorder cost |

Structural effect: exact-block dedup replaced the second copy of block A with the official
reference hint, saving 1,496 prompt characters in request 4 and preserving all block ids.
Provider cache metrics are noisy and are reported as observations only.

## Findings

- The official dedup mechanism is reproducible and safe at block level: same block count, same
  ids, transformed content, no semantic block dropped.
- The official reorder mechanism is only meaningful across a batch of requests, and on the pinned
  0.5.0 build it is prohibitively slow for long blocks (minutes of CPU for 16 × 5 KB documents).
  That blocked live P1/P3 stress runs.
- Functional P0–P3 all succeed and keep the Atlas stable prefix; no task semantics changed.
- No ContextPilot request/batch scheduling is used; AtlasOS keeps request order explicit.

## Limitations

- Live runs were produced from a dirty worktree (`git_dirty=true`); they are exploratory, not
  formal frozen results.
- P1/P3 stress results are missing because of vendor reorder cost; this is a reproducible
  structural limitation, not a silent fallback.
- Dedup only triggers for multi-chunk content; short blocks are never shortened.
- ContextPilot index state is not provider cache state; no cache claim relies on it.
