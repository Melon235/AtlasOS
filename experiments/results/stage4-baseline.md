# Stage 4 Snapshot — Placement Planning (Formal)

Frozen 2026-10-22. Code commit `391eb8c`, `git_dirty=false` for all formal offline records.

## Offline deterministic placement-stress results

Workload: A1→A2→A3, B1→B2→B3, Merge; workers worker_a/b/c (research) plus reviewer.

| variant | policy | switches | mean locality | handoff refs | distribution |
|---|---|---|---:|---:|---|
| S0 | static_baseline | 6 | 0.000 | 6 | a=2, b=2, c=2, reviewer=1 |
| S1 | capability_only | 6 | 0.000 | 6 | a=2, b=2, c=2, reviewer=1 |
| S2 | context_locality | 2 | 0.800 | 2 | a=3, b=3, reviewer=1 |

Relative reductions (S2 vs S1): switches 66.7%, logical handoff refs 66.7%.

## Live functional validation

| variant | status | switches | locality | prompt | hit | miss | latency_ms |
|---|---|---|---:|---:|---:|---:|---:|
| S0 | succeeded | 2 | 0.0 | 910 | 128 | 782 | 4,109.6 |
| S1 | succeeded | 2 | 0.0 | 887 | 128 | 759 | 4,445.0 |
| S2 | succeeded | 2 | 0.0 | 890 | 128 | 762 | 4,228.3 |

The functional workload has fixed roles; variants coincide. This run validates correctness and
integration only; no provider-cache improvement is claimed.

## Reproduce

```bash
uv run python examples/stage4_formal.py
ATLASOS_RUN_LIVE_TESTS=1 uv run python examples/stage4_live.py
```
