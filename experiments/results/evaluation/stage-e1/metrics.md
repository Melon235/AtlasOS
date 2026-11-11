# Stage E1 - Paired Multi-Worker Evaluation

Source manifest: `D:\Work\AtlasOS\experiments\results\stage-e1\e1-20261109T102030Z\manifest.json`
Git commit: `50759ed3fc81783ab33816af0b8833c4bbb58010`; dirty: `False`

## Overall

| variant | runs | success | prompt tokens | completion tokens | turns | cache ratio | cost USD | p50 latency ms | tool calls |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| A0 | 36 | 28/36 | 757828 | 61598 | 257 | 0.9094 | 0.098655 | 37534.3 | 310 |
| A1 | 36 | 3/36 | 4992381 | 171656 | 987 | 0.8839 | 0.406338 | 72747.1 | 1075 |

## Paired difference (A1 - A0)

- pairs: 36
- mean difference: -0.6944444444444444
- 95% normal-approximation CI: [-0.8828208734698022, -0.5060680154190866]
- A1-only successes: 2; A0-only: 27; both same: 7

Small paired samples; the CI is descriptive, not a significance claim.

- infrastructure interruptions (excluded from adjusted A0 denominator): 1; A0 adjusted: 28/35

## Collaboration evidence

- A1 worker steps observed: 96
- worker successes / failures: 48 / 48
- dependency edges: 36
- handoff complete edges: 21
- handoff consumed edges: 21
- transmitted handoff bytes (actual prefix): 9390
- worker profiles: coding_worker
- mean DAG nodes / critical path: 2.6666666666666665 / 1.6666666666666667
- all workers reported success but task failed: 8 runs
- upstream success -> downstream failure edges: 6
- worker stop codes: {'max_turns': 28, 'success': 48, 'side_effect_unknown': 18, 'doom_loop': 2}

## Families

| family | A0 | A1 |
|---|---:|---:|
| decompose_merge | 8/9 | 0/9 |
| dependency_error | 3/6 | 2/6 |
| integration | 7/9 | 0/9 |
| multi_module | 10/12 | 1/12 |

## Failure classes

- a0:None: 1
- a0:ORACLE_FAILURE: 7
- a1:BUDGET_EXCEEDED: 20
- a1:ORACLE_FAILURE: 13

Figures: `figures\paired_success.png`, `figures\family_success.png`, `figures\paired_tokens.png`
