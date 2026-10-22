# Stage 3 Snapshot — ContextPilot P0–P3 (exploratory)

Recorded 2026-10-22. Source tree was dirty (`git_dirty=true`) during these runs, so they are
exploratory rather than formal frozen results. Raw data: `experiments/runs/runs.jsonl` and
`experiments/runs/stage3-stress.jsonl` (gitignored).

## Functional workload (AtlasRuntime, P0–P3, one run each)

| variant | optimizer | reorder | dedup | status | prompt | hit | miss |
|---|---|---|---:|---:|---|---:|---:|---:|
| P0 | identity | false | false | succeeded | 940 | 128 | 812 |
| P1 | contextpilot | true | false | succeeded | 927 | 128 | 799 |
| P2 | contextpilot | false | true | succeeded | 901 | 128 | 773 |
| P3 | contextpilot | true | true | succeeded | 900 | 128 | 772 |

All tasks completed; stable-prefix hashes identical across variants; P0 pre/post layout equal.

## Cache-stress workload (small scale, 2 repetitions)

| variant | calls | prompt | hit | miss |
|---|---:|---:|---:|---:|
| P0 identity | 8 | 39,736 | 18,944 | 20,792 |
| P2 dedup | 8 | 36,744 | 33,280 | 3,464 |
| P1 reorder | — | — | — | — |
| P3 full | — | — | — | — |

Dedup effect: request 4 (`E A B`, where E is an exact copy of A) fell from 4,967 to 3,471
prompt tokens; the duplicate was replaced by the official reference hint. Block ids and count
were preserved.

## Why P1/P3 stress are absent

Batched `ContextPilot.reorder()` over 4 requests × 4 blocks (~5 KB each) did not complete within
three minutes in two separate probes. Single-context reorder is effectively free but is a no-op.
The reorder stress variants were therefore not run live; no silent fallback was used.

## Reproduce

```bash
ATLASOS_RUN_LIVE_TESTS=1 uv run python examples/stage3_contextpilot.py
```

(The script runs functional P0–P3 and the stress P0–P3 loop; on this vendor build the P1/P3 stress
reorder step must be expected to spend minutes in CPU, which is why the recorded run omitted it.)
