# Stage 5 — Smoke, Pilot, and Formal Runs

All capability runs used real DeepSeek (`deepseek-chat`, provider-resolved `deepseek-flash`) and
real self-hosted OpenSandbox (`atlas-uat-python:3.12`, network `none`). No fake provider is
reported as a real evaluation.

## Smoke

- Round: `smoke-20261106T141747Z`.
- Git commit `18e37f8`, `git_dirty=false`.
- 5 tasks across bugfix, feature, tool discovery, multi-worker, and long context; 1 repetition.
- Result: **5/5 task success**, 0 scored-sample errors, 174 s.
- Latency/score evidence: `experiments/results/evaluation/rounds/smoke-20261106T141747Z/`.
- The first smoke attempt exposed a task contradiction in `bugfix-005-date-range` (public test
  asserted the old exclusive-end behavior while the prompt required inclusive end). The public
  test was corrected; no oracle was weakened. The clean re-run above is the recorded smoke round.

## Pilot

- Round: `pilot-20261106T142055Z`.
- Git commit `18e37f8`, `git_dirty=false`.
- 6 tasks, each run twice; 12 runs.
- Result: **11/12 task success**; repetition 0 was 5/6, repetition 1 was 6/6.
- The single failure was `longcontext-003-module-map`: the model read the authoritative module
  map and pipeline skeleton, then stopped after two tool calls without implementing
  `pipeline_total()`. This is a model self-termination failure, not an oracle or sandbox failure.
- Framework fixes motivated by pilot/smoke: Inspect `Store` API, silent sample errors, task prompt
  ambiguity for duplicate keys, provider retry/timeout bounding, and per-sample subprocess
  isolation.

## Formal

- Round: `formal-20261106T131632Z`.
- Git commit `18e37f8`, `git_dirty=false`.
- 30 tasks × 3 independent repetitions = 90 runs.
- Per-repetition: 26/30, 29/30, 29/30.
- Overall: **84/90 = 93.33%**.
- Failure classification: 84 `TASK_SUCCESS`, 6 `ORACLE_FAILURE`; 0 model/runtime/sandbox/
  cleanup/host-isolation/timeout failures.
- Tool behavior: Recall@1 15/15, Recall@3 15/15, selection 15/15.
- Multi-worker: 14/15 tasks delivered all worker artifacts; 13/15 workers self-reported success.
- Safety: 0 unauthorized side effects / 90; 0 Host isolation violations / 90.
- Performance: P50 E2E 29.63 s, P95 42.70 s; provision P50 7.76 s; cache hit 1,531,889 /
  1,702,051 prompt tokens (90.0%); estimated peak-rate cost $0.19115; mean 7.01 turns and 7.54
  tool calls.
- Failure samples: `bugfix-007-toposort` (3/3), `feature-003-line-diff` (1/3),
  `longcontext-003-module-map` (1/3), `multiworker-001-handoff` (1/3). Root causes are documented
  in the final report; all are incomplete-agent outcomes, not framework/oracle defects.

## Comparison runs

- AnyCode baseline: `baseline-20261106T143211Z`, commit `5acc98f`, `git_dirty=false`, 25 non-tool
  tasks × 1 repetition, **20/25 = 80%**. Tool tasks are `NOT_EVALUATED` for this baseline.
- Progressive-discovery ablation: `ablation-tools-20261106T144147Z`, code commit `5acc98f`,
  `git_dirty=true` (untracked analysis/report artifacts were present at start). 10-task subset ×
  1 repetition, **7/10 overall**; on the 8 non-tool tasks it was **7/8 = 87.5%** while full
  AtlasOS on the same 8 tasks was **18/24 = 75%** (3 repetitions). The one-repetition subset is
  too small to claim a discovery benefit or penalty. The two tool tasks' direct native MCP calls
  were correct but are `NOT_EVALUATED` because the progressive-contract oracle requires
  `tool.search`.

## Raw artifacts

- `experiments/results/evaluation/rounds/<round>/manifest.json` — provenance and pass summary.
- `experiments/results/evaluation/rounds/<round>/runs/.../result.json` — one raw result per run.
- `experiments/results/evaluation/rounds/<round>/inspect-logs/*.eval` — Inspect AI logs.
- `experiments/results/evaluation/formal-analysis/` — metrics, failure cases, charts.
- `experiments/results/evaluation/comparison.json` — baseline/ablation summary.
- `experiments/results/evaluation/reliability_matrix.json` — 19-case safety/recovery matrix.

The `rounds/` and `inspect-logs/` directories are gitignored to keep the repository small; the
committed analysis files are generated from those raw results by
`experiments/evaluation/analysis.py`.
