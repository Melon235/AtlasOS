# Stage 3 Notes — ContextPilot Integration (Closure)

Recorded 2026-10-22. Commits: Stage 1 `7eae2c1`, Stage 2 `34ea504`, initial Stage 3 `ccbfa71`,
closure code `3d90bfc`.

## Corrected architecture

```text
Task -> TaskScopedSelector -> selected blocks
     -> per-request ContextTransform (identity | ContextPilot dedup)
     -> StablePromptRenderer (Atlas owns prefix/task/layout)
     -> rendered requests
     -> BatchRequestPolicy (identity | ContextPilot reorder, experiment-only)
     -> provider
```

`ContextPilot.dedup_chat_completions()` is a per-request transformation.
`ContextPilot.reorder()` is a cross-request batch primitive and is not part of the runtime path.
Historical P0–P3 labels are retired; historical `optimizer` configs are rejected instead of
silently mapping to the wrong semantics.

## Runtime path

- `ContextTransform` implementations: `IdentityContextTransform`,
  `ContextPilotDedupTransform` (lazy import; missing package fails clearly).
- Selection output is stable-ordered before the transform; the renderer preserves that order, so
  `pre_transform_layout_hash` stays deterministic across inverted completion order.
- Atlas keeps the stable system prefix, agent role, task prefix, delimiters, and dynamic tail.
- A failed ContextPilot import never silently falls back to a different algorithm.

## Experiment taxonomy

| Layer | Variants |
|---|---|
| Per-request transform | T0 identity, T1 ContextPilot dedup |
| Batch request policy | B0 identity, B1 ContextPilot reorder (experiment-only, not on the V1 path) |

## Formal clean-worktree dedup result

Source tree clean at execution (`git_dirty=false`, code commit `3d90bfc`). Workload: deterministic
cache-stress `small` scale, four requests, three repetitions per variant.

| variant | prompt | hit | miss | ratio | provider ms (reps) | transform ms |
|---|---:|---:|---:|---:|---|---:|
| T0 identity | 19,868 | 18,944 | 924 | 0.9535 | 3,969.2 / 4,199.3 / 3,258.5 | 0 / 0 / 0 |
| T1 dedup | 18,372 | 17,536 | 836 | 0.9545 | 4,925.7 / 3,664.1 / 4,691.1 | 2.41 / 2.38 / 2.78 |

Token counts are identical across repetitions; latency varies. Dedup reduced context characters
104,496 → 96,375 (the exact duplicate block in request 4) and prompt tokens 19,868 → 18,372.

Cache-miss reduction: **(924 − 836) / 924 = 9.52%**. The provider cache was warm from earlier
runs, so the absolute hit ratio is not a cold-start number; only the paired T0/T1 comparison is
meaningful.

## Reorder CPU viability

The bounded benchmark classifies ContextPilot reorder as **C — impractical for the online V1 CPU
path**: ~1.1 s even for the smallest batch, and the 104 K-char stress batch did not finish within
three minutes. B1 therefore remains experiment-only and was not run live. No unbounded vendor
call remains in tests or examples.

## Claims

- AtlasOS reproduced and integrated ContextPilot's official dedup behavior as a per-request
  context transform.
- AtlasOS measured provider-reported cache effects and reports a formal 9.52% miss reduction on
  this deterministic workload.
- ContextPilot reorder was verified to be a cross-request batch primitive and characterized as
  impractical for the current online CPU path.

## Known limitations

- One workload scale, one provider, three repetitions; latency is noisy.
- The provider cache was warm, so the ratio is not cold-start.
- Dedup only triggers for multi-chunk duplicate content.
- Reorder is not evaluated live; only its cost is characterized.
- The exploratory dirty-tree figure is superseded by the formal results above.
