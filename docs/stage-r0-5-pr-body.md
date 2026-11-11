# eval: validate selective delegation on positive parallel tasks

## Summary

Stage R0 proved the delegation router can *refuse* a plan. This PR adds the missing control group
and answers the opposite question: when a task really is independent, does the router admit it, do
the workers actually run at the same time, and does the delegation produce anything worth its cost?

It does not change the production scheduler, the worker factory, the action gates, the ledger, the
sandbox, the handoff, the budget guard, or the R0 router semantics. It adds benchmark families, a
measurement wiring for the execution timeline, and an honest negative result.

## Motivation

R0's 18-run round admitted **0 of 6** plans, so the parallel path was only ever exercised by a unit
test. "It refuses bad plans" is half of a router; without a positive control it is also
indistinguishable from "it refuses everything". This stage supplies the positive control that R0's
own report asked for.

## Architecture

```text
Candidate DAG (frozen benchmark definition)
        |
        v
R0 Routing Gate  (src/atlasos/workers/routing.py — unchanged)
        |
        v
DIRECT  /  DELEGATE_ONE  /  DELEGATE_PARALLEL
        |
        v
Existing AnyCode runtime (wave scheduler, M2 factory, M3 gate, M4 ledger, M5 sandbox,
                          M6 recovery, M7 trajectory, G1 handoff, G2a budget)
```

Untouched by this PR: `src/atlasos/workers/routing.py` (byte-identical to R0),
`src/atlasos/integrations/anycode_engine.py`, the AnyCode package, `src/atlasos/sandbox/`,
`src/atlasos/actions/`, the shared budget and the worker prompts.

Changed in the evaluation layer only:

* `experiments/evaluation/stage_r05_tasks.py` — six benchmark families and their frozen digests,
* `experiments/evaluation/stage_r05_routing.py` — offline route audit,
* `experiments/evaluation/stage_r05_rounds.py` — A0/A1/A2 round runner with a pre-registration guard,
* `experiments/evaluation/stage_r05_analysis.py` — per-run metrics, route audit, measured timeline,
* `experiments/evaluation/stage_r0_routing.py` — `routing_policy(version)` plus contract evidence
  read from public fixture files (additive; `r0.1.0` is unchanged and its digest is asserted),
* `experiments/evaluation/models.py` — optional `frozen_contracts` on `BenchmarkTask` (absent for
  every existing task, so no existing fixture digest moves),
* `experiments/evaluation/runner.py` — 12 lines: the policy version reaches the router, and every
  progress event records its runtime task id and agent so a start can be paired with its own end,
* five new unit-test modules (50 tests).

## Evaluation

Fixed conditions (same as R0): DeepSeek chat, temperature 0, OpenSandbox, guarded budget,
corrected tool errors, dual handoff, compaction off, tool-result off, legacy worker contracts,
120 k tokens, 14 turns, 60 tool calls, G3-4a isolation.

Families: **P1** independent read-only analysis, **P2** independent module implementation,
**P3** frozen-interface collaboration. Negatives (offline audit only): **N1** shared write,
**N2** unfrozen interface, **N3** linear chain.

30 runs: Track A (3 families × 3 arms × 2 reps) at `r0.1.0`; Track B (`e1-01-config`, `e1-03-rbac`
× 3 arms × 2 reps) at `r0.2.0`, which differs from `r0.1.0` in `max_parallel_workers` only.

| Track A | A0 single | A1 forced DAG | A2 selective |
|---|---|---|---|
| Oracle | 6/6 | 2/6 | 3/6 |
| Cost (USD) | 0.0367 | 0.0584 | 0.0513 |
| Prompt tokens | 277 k | 535 k | 457 k |
| Mean latency | 50.3 s | 66.5 s | 63.8 s |
| Observed concurrency | 1 | 2 | 2 |

| Track B | A0 | A1 (3 workers) | A2 (3 workers) |
|---|---|---|---|
| Oracle | 4/4 | 2/4 | 2/4 |
| Cost (USD) | 0.0116 | 0.0441 | 0.0428 |
| Budget exceeded | 0 | 4 | 4 |
| Mean execution window | 23.5 s | 44.9 s | 48.3 s |

Router record: 6/6 live positive admissions, 3/3 negative rejections, 0 false admission,
0 false rejection, 0 policy constraints, 1 declared-but-absent contract recorded as UNKNOWN.

Real parallelism: 20/20 multi-worker runs show measured wall-clock overlap. In `e1-01-config` A2 the
three workers start within 10 ms and run 45–52 s each; the window is 51.7 s for 145.6 s of worker
time. Provider-level overlap is **inferred** from those intervals, not timestamped.

Conclusion: admission is correct and parallelism is real, but delegation cost 1.4× (Track A) to
3.7× (Track B) more and delivered correct artifacts less often. The binding constraint is the
economics of the runtime — per-worker context and one shared budget — not the routing decision.

## Risks & Limitations

* n = 2 per cell. Directional only; no significance claim, no percentiles.
* The benchmarks are public, including their oracles. A worker with network egress could read them;
  tool calls were scanned (0 hits) but this must not be presented as a held-out benchmark.
* Worker prompts see only public fixtures; hidden tests are overlaid on the Host only, and the
  G3-4a isolation check passed in 30/30 runs.
* Two benchmark defects were found and are documented rather than hidden: `r0.5.0`'s P1 oracle
  rejected a correct analysis for listing `TypeError`, and `r0.5.1`'s P2 oracle contradicted its own
  contract about field stripping. Both were invalidated, versioned and (for P2) fully rerun. The
  report lists the six invalidated runs and proves with digests that the retained runs used an
  unchanged definition.
* `UNSTABLE_INTERFACE` now means "no *frozen, public* contract", not "no contract anywhere": the
  evidence is only PROVEN when the declared document is part of the public fixture.
* Pure read-only plans without an artifact still cannot be admitted (Gate 0 proves coverage through
  declared write paths); P1 sidesteps this by writing two reports. Recorded as an open problem, not
  fixed here.
* No online Main Agent planner, no Context Pack, no scheduler change: out of scope by design.

## Testing

```text
.\.venv\Scripts\python.exe -m pytest                     685 passed, 1 skipped, 1 deselected (228 s)
.\.venv\Scripts\python.exe -m pytest tests/unit/test_stage_r05_*.py    50 passed
.\.venv\Scripts\python.exe -m ruff check .               All checks passed
.\.venv\Scripts\python.exe -m ruff format --check .      349 files already formatted
npx pyright                                              0 errors, 0 warnings
```

Not run: live API tests (`-m live`, opt-in) and the TUI/UI suites. Nothing was skipped to make the
benchmark pass.

## Reviewer Checklist

1. Is the router free of result-driven tuning? (`src/atlasos/workers/routing.py` unchanged)
2. Is `r0.1.0` unchanged? (digest asserted in `test_stage_r05_routing.py`)
3. Does `r0.2.0` change exactly one field? (same test)
4. Are the positive families genuinely independent? (disjoint write sets, no DAG edges, tested)
5. Do all arms share one functional standard? (one definition derives A0/A1; fixture, oracle,
   allowed paths and required artifacts identical)
6. Any `sample_id` branch in the decision path? (test renames a sample and asserts an identical
   decision)
7. Any hidden-answer leak into the sandbox? (prompts contain no hidden path; 0 oracle markers in 30
   runs)
8. Is the overlap evidence real? (per-worker intervals from timestamped events, not DAG width)
9. Are M2 / the scheduler / the budget untouched? (diff scope above)
10. Are unrelated dirty files excluded? (`_debug_run/` and all run directories remain untracked)
11. Any sensitive data? (`.env` ignored, no keys, no personal paths, no raw provider logs)
12. Does any claim exceed the data? (the report's verdict table marks correctness and cost as FAIL)

## Commit stack

1. `feat: land Stage G1-R0 runtime safety, handoff, budget and delegation router` — prerequisite.
   These stages were never pushed; R0.5 cannot run or be reviewed without them.
2. `test(evaluation): add positive delegation benchmarks and measure real parallelism`
3. `fix(evaluation): read the G3-4a isolation verdict from its actual field`
4. `docs(runtime): report R0.5 positive delegation validation`
