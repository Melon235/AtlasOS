# Stage 3 Snapshot — Formal Clean-Worktree Results

Frozen 2026-10-22 from code commit `3d90bfc` with `git_dirty=false` at run time
(`examples/stage3_contextpilot.py`). Raw data: `experiments/runs/stage3-formal.jsonl`
(gitignored). The earlier exploratory dirty-tree numbers are superseded and must not be quoted.

## Formal T0/T1 cache-stress results

Workload: deterministic `small` stress scale, four requests per repetition, three repetitions per
variant, `deepseek-chat`.

| variant | rep | prompt | hit | miss | ratio | provider ms | transform ms |
|---|---:|---:|---:|---:|---:|---:|---:|
| T0 identity | 1 | 19,868 | 18,944 | 924 | 0.9535 | 3,969.2 | 0.00 |
| T0 identity | 2 | 19,868 | 18,944 | 924 | 0.9535 | 4,199.3 | 0.00 |
| T0 identity | 3 | 19,868 | 18,944 | 924 | 0.9535 | 3,258.5 | 0.00 |
| T1 dedup | 1 | 18,372 | 17,536 | 836 | 0.9545 | 4,925.7 | 2.41 |
| T1 dedup | 2 | 18,372 | 17,536 | 836 | 0.9545 | 3,664.1 | 2.38 |
| T1 dedup | 3 | 18,372 | 17,536 | 836 | 0.9545 | 4,691.1 | 2.78 |

### Summary

| metric | variant | mean | median | min | max |
|---|---|---:|---:|---:|---:|
| prompt_tokens | T0 | 19,868 | 19,868 | 19,868 | 19,868 |
| prompt_tokens | T1 | 18,372 | 18,372 | 18,372 | 18,372 |
| cache_hit_tokens | T0 | 18,944 | 18,944 | 18,944 | 18,944 |
| cache_hit_tokens | T1 | 17,536 | 17,536 | 17,536 | 17,536 |
| cache_miss_tokens | T0 | 924 | 924 | 924 | 924 |
| cache_miss_tokens | T1 | 836 | 836 | 836 | 836 |
| cache_hit_ratio | T0 | 0.9535 | 0.9535 | 0.9535 | 0.9535 |
| cache_hit_ratio | T1 | 0.9545 | 0.9545 | 0.9545 | 0.9545 |
| provider_latency_ms | T0 | 3,809.0 | 3,969.2 | 3,258.5 | 4,199.3 |
| provider_latency_ms | T1 | 4,427.0 | 4,691.1 | 3,664.1 | 4,925.7 |
| transform_latency_ms | T0 | 0.00 | 0.00 | 0.00 | 0.00 |
| transform_latency_ms | T1 | 2.52 | 2.41 | 2.38 | 2.78 |

Context characters: 104,496 → 96,375 for T1 (dedup of the exact duplicate block in request 4).

**Observed cache-miss reduction:** `(924 − 836) / 924 = 9.52%`.

The provider cache was warm from earlier runs, so the ~95% hit ratio is not a cold-start number;
the paired T0/T1 comparison is the meaningful result.

## Reorder CPU benchmark (bounded, 10 s timeout per case)

| case | requests | blocks/req | chars | reorder ms | timed out |
|---|---:|---:|---:|---:|---|
| c1 | 2 | 2 | 8,352 | 1,119.8 | no |
| c2 | 2 | 3 | 12,528 | 1,102.5 | no |
| c3 | 3 | 3 | 18,792 | 1,140.1 | no |
| c4 | 4 | 4 | 33,408 | 1,114.0 | no |
| c5 | 2 | 2 | 16,112 | 1,133.3 | no |
| c6 | 3 | 3 | 36,252 | 1,165.5 | no |

Classification: **C — impractical for the AtlasOS V1 online CPU path.** The smallest batch already
costs ~1.1 s, and the 104 K-char stress batch did not complete within three minutes.

## Reproduce

```bash
# formal T0/T1 runs (clean worktree required for git_dirty=false)
ATLASOS_RUN_LIVE_TESTS=1 uv run python examples/stage3_contextpilot.py

# bounded reorder CPU benchmark
uv run python experiments/benchmarks/reorder_cpu.py
```
