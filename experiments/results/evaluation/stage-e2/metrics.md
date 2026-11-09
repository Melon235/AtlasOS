# Stage E2 - Context Plane Track A

Source manifest: `D:\Work\AtlasOS\experiments\results\stage-e2\e2-20261109T104850Z\manifest.json`
Git commit: `85b3918ed902fe5b712748a4f3560a90f72335a4`; dirty: `False`

## Aggregate

| configuration | runs | correct | prompt tokens | cache hit | cache miss | hit ratio | cost USD | p50 latency ms | dedup calls | block chars removed | transform ms | switches | locality |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| context:C0:bypass | 24 | 23/24 | 17367 | 8448 | 8919 | 0.4864 | 0.003540 | 2695.5 | 0 | 0 | 0.00 | 0 | None |
| context:C1:bypass | 24 | 23/24 | 17505 | 8448 | 9057 | 0.4826 | 0.003581 | 2598.6 | 0 | 0 | 0.03 | 0 | None |
| context:C2:bypass | 24 | 22/24 | 17505 | 8448 | 9057 | 0.4826 | 0.003581 | 2838.6 | 0 | 0 | 45.38 | 0 | None |
| placement:C1:capability_only | 12 | 12/12 | 6021 | 0 | 6021 | 0.0000 | 0.002483 | 4473.1 | 0 | 0 | 0.03 | 48 | 0.0 |
| placement:C1:context_locality | 12 | 12/12 | 6021 | 0 | 6021 | 0.0000 | 0.002483 | 4176.1 | 0 | 0 | 0.02 | 24 | 0.6666666666666666 |

## Notes

- C0 is upstream_default; C1 adds task-scoped selection + stable rendering + identity transform; C2 adds ContextPilot dedup. Only this factor changes.
- Correctness is an exact-result token check, not an LLM judge.
- C0 executes before C1/C2 for each task; provider cache is warm-order sensitive. Hit ratio differences are observations, not causal cache improvements.
- Dedup calls and block-char removal are measured from real inference records; zero means the transform did not remove a block in these workloads.
- P0/P1 use C1 context with capability_only vs context_locality placement; 4 tasks x 3 repetitions.
