# Stage R1-0 — Multi-Worker Failure Attribution & Review Hardening

Stage R0.5 showed that the router admits genuinely independent plans and that the workers really run
concurrently — and that the result was more expensive, slower and less correct than one worker. This
stage asks *where* that cost and those failures come from, using only the recorded runs. No runtime
behaviour was changed, no benchmark was re-scored, no task was re-run.

**Answer in one paragraph.** The extra cost is not the duplicated system prompt or tool schema: the
fixed per-worker prompt is ~1.9 k tokens, about 4 % of the extra spend. It is the **number of
independent turns**: one worker takes 55 turns for the whole Track A matrix, the split arms take 111
and 125, because every worker re-discovers the same repository and carries its own growing history
(per-turn cost is actually *lower*). Correctness fails for three reasons, none of which is missing
context: the shared 120 k budget cannot fund three independent explorations (21 of 58 worker
executions were stopped by it), workers sometimes revert each other's files in the shared workspace
(2 runs), and one worker looped or ran out of turns debugging its own arithmetic (3 runs). In 27 of
28 required-fact checks the worker had read the source it needed.

---

# Track A — GitHub review hardening

## A1. History and PR audit

Read from the remote, not from memory:

| Item | Value |
|---|---|
| local HEAD | `2183983` on `experiment/r0-5-positive-delegation` |
| `origin/master` | `3fbf3dd` |
| PR #1 before | base `master`, 28 commits, 143 files |
| prerequisite commit | `7bd0965` (G1–R0) |
| R0.5 commits | `1309a18`, `7146937`, `f3d8396`, `35743f4`, `2183983` |
| working tree | clean except the untracked local `_debug_run/` scratch directory |

The 143-file diff was mostly 22 never-pushed stage commits (Stage E1–F and their committed result
artefacts). Rather than rewrite history, the stack was split with a new base branch:

```text
master (3fbf3dd)
  └── feat/stage-g1-r0-prerequisites   = 7bd0965        PR #2  (132 files, 23 commits)
        └── experiment/r0-5-positive-delegation        PR #1  (13 files, 5 commits)
```

* **PR #2** — `feat: land Stage G1-R0 …` → `master`.
* **PR #1** — retargeted to the prerequisite branch. Its diff is now exactly the R0.5 surface:
  `models.py`, `runner.py`, four `stage_r05_*.py` modules, five test modules, two docs.

No commit was rewritten, nothing was force-pushed, `master` is untouched.

| Class | Files |
|---|---|
| prerequisite G1–R0 | PR #2 (132 files, dominated by the 22 earlier stage commits) |
| R0.5 experiment code | PR #1: `stage_r05_tasks/routing/rounds/analysis.py`, `models.py`, `runner.py` |
| R0.5 report | PR #1: `docs/stage-r0-5-positive-delegation.md`, `docs/stage-r0-5-pr-body.md` |
| historical benchmark artefacts | PR #2 (committed by earlier stages) |
| unrelated files | none: `_debug_run/` and every raw run directory stay untracked |

## A2. Recomputable evidence

Raw run directories (`manifest.json`, `result.json`, `stage_e_evidence.json`, `provider_calls.jsonl`,
`workers.json`, trajectories, exported workspaces) stay local and git-ignored. What is published is
sanitized and sufficient to recompute every number in this report:

| Path | Content |
|---|---|
| `experiments/evidence/stage-r0-5/summary.json` | per-arm and per-task aggregates, paired deltas, route audit, parallel evidence, version reconciliation |
| `experiments/evidence/stage-r0-5/runs.csv` | one row per valid run (27 columns, no paths) |
| `experiments/evidence/stage-r0-5/timeline_summary.json` | worker starts, observed concurrency, overlap pairs, wall window |
| `experiments/evidence/stage-r0-5/benchmark_digests.json` | preregistration, family digests, version inclusion/exclusion |
| `experiments/evidence/stage-r1-0/summary.json` | attribution aggregate (outcomes, stop codes, reasons, join quality) |
| `experiments/evidence/stage-r1-0/runs.csv` | per-run ledger row |
| `experiments/evidence/stage-r1-0/workers.csv` | per-worker ledger row (tokens, turns, tools, stop, deliverable state) |
| `experiments/evidence/stage-r1-0/attribution.json` | reason codes with evidence per run |

Reproduce with:

```text
.\.venv\Scripts\python.exe -m experiments.evaluation.stage_r05_analysis --write
.\.venv\Scripts\python.exe -m experiments.evaluation.stage_r1_0_analysis analyse
.\.venv\Scripts\python.exe -m experiments.evaluation.stage_r1_0_analysis publish
```

No API key, no sandbox and no network are involved. No raw provider conversation, key, host path or
sandbox archive is published.

**The R0.5 oracle remains public.** `stage_r05_tasks.py` contains the hidden tests; this stage keeps
saying so and does not present the family as a held-out benchmark.

## A3. Continuous integration

`.github/workflows/ci.yml` (new; the repository had no workflows): Python 3.12, `pip install -e
".[dev,sandbox]"`, then

```text
python -m pytest                 (live API tests are deselected by the pytest addopts marker filter)
python -m ruff check .
python -m ruff format --check .
python -m pyright
```

No secrets: `tests/integration/test_live_deepseek.py` is marked `live` and deselected, and
`test_stage_g3_4a_isolation_live.py` skips unless `ATLASOS_RUN_LIVE_SANDBOX=1`. CI proves the
offline regression only; it does not reproduce the 30 paid model runs. The PR is not auto-merged.

---

# Track B — Failure attribution

## 1. Input data integrity

| Check | Result |
|---|---|
| run directories on disk | 36 (6 are the invalidated `r0.5.0` round) |
| runs analysed | **30** (Track A 18, Track B 12) |
| excluded runs | 6 × `r05-p2-modules` from `r05-a-20261111T083830Z`, reason `STALE_BENCHMARK_DEFINITION` |
| benchmark versions in use | `r0.5.1` (P1, P3, Track B) and `r0.5.2` (P2); family digests match the frozen definitions |
| per-run identity captured | `run_id`, benchmark version + digest, policy version + digest, git commit, provider, model, config digest, fixture digest |
| telemetry present and complete | `result.json`, `stage_e_evidence.json`, `provider_calls.jsonl`, `workers.json`, trajectory, exported workspace — 36/36 |

**A data defect was found and worked around.** `result.json` loses the tool calls of a worker that
the budget guard stopped: `worker_results[...].tool_calls` is 0 and the calls never reach the
top-level list. Across the 30 runs the trajectories record **591 actions** while `result.json`
records **365** — 226 actions (38 %) are missing, exactly in the failing workers. Counting only
`result.json` would have produced the false finding "the worker did nothing".

The ledger therefore rebuilds the action list from the trajectory (`action_proposed` +
`execution_outcome`) and fills in the input text by `sha256(canonical_json(input))`:

| Recovery | Actions |
|---|---|
| input recovered from the same run | 365 |
| input recovered from an identical command in another run (digest identifies the input exactly) | 39 |
| input genuinely unavailable (unique, unrecorded) | 187 |

Every number below states which of the two sources it came from. Budget, token and cache numbers are
per-request and do not depend on the tool inventory.

## 2. Worker execution ledger

58 worker executions: 32 `success`, 21 `budget_exceeded`, 2 `doom_loop`, 1 `max_turns`,
1 `provider_unavailable`, 1 `side_effect_unknown`.

| Arm | Workers | Turns | Prompt tokens | Tokens / turn | Fixed overhead | Tool calls |
|---|---|---|---|---|---|---|
| Track A A0 | 6 | 55 | 277,494 | 5,045 | 12,510 | 68 |
| Track A A1 | 12 | 125 | 535,449 | 4,284 | 23,854 | 89 |
| Track A A2 | 12 | 111 | 457,330 | 4,120 | 23,854 | 118 |
| Track B A0 | 4 | 28 | 81,668 | 2,917 | 7,374 | 36 |
| Track B A1 | 12 | 106 | 409,744 | 3,866 | 21,002 | 26 |
| Track B A2 | 12 | 112 | 424,688 | 3,792 | 21,002 | 28 |

Per-worker detail (first/last request, tools by type, first/last write, public-test attempts, stop
code, deliverable state) is in `experiments/evidence/stage-r1-0/workers.csv`. Notable shapes:

* 32 of the 49 writing workers wrote exactly once, 9 wrote twice and 8 three times (write + edits);
* `file_read` is rare (1–4 per worker) because reading happens through `cat` in `bash`;
* no worker ever ran a public test — see §7;
* the last write is followed by 0–7 further tool calls (median 3): self-review, `git status`, or
  `rm -rf __pycache__`.

## 3. Token and cache attribution

Method, fully reproducible from the published rows:

* `prompt_tokens` / `completion_tokens` per request are exact provider measurements;
* they are attributed to a worker through the budget ledger sample (`request_id` → call index,
  verified per run): **533 exact, 4 multiset, 0 ambiguous, 0 unmatched**;
* "fixed overhead" is the **first** request of each worker: system prompt + tool schema + task
  prompt + environment note, before any history exists;
* "growth" is `last_request_prompt_tokens − first_request_prompt_tokens`, i.e. accumulated history
  and tool results. Splitting that growth further into history vs tool results is **not** possible
  from the provider data; the conversation files are used only for character shares, and no token
  count is inferred from characters.

| Quantity | Track A A0 | Track A A1 | Track A A2 |
|---|---|---|---|
| Prompt tokens per run (mean) | 46,249 | 89,242 | 76,222 |
| Fixed overhead per worker-instance | 2,085 | 1,988 | 1,988 |
| Share of spend that is fixed overhead | 4.5 % | 4.5 % | 5.2 % |
| Turns per run | 9.2 | 20.8 | 18.5 |
| Growth inside a worker (example) | 2,309 → 6,100 | 2,146 → 5,401 | 2,161 → 4,254 |

So: **duplicated system prompt and tool schema are not the cost driver.** Adding one worker adds
~1.9 k tokens of fixed prompt and 5–8 extra turns of 4–8 k tokens each. The per-turn price is
*lower* in the split arms because each worker's own history is shorter than the single worker's.

Cache: hit ratio stays at 0.917–0.924 in Track A regardless of arm; Track B drops from 0.928 (one
worker) to 0.867–0.882 (three workers) because three conversations diverge earlier. More workers
means less prefix reuse, not more.

## 4. Budget competition

Shared budget: 120,000 tokens, `guarded` mode, one ledger for the whole run.

| Run | Used | Share per worker | First worker stopped |
|---|---|---|---|
| `e1-01-config` A0 | 22,555–23,329 (19 %) | solo 100 % | — |
| `e1-01-config` A1/A2 | 101,641–112,029 (85–93 %) | 0.20–0.53 each | `validate` (needed 9,893 more, 7,971 left) |
| `e1-03-rbac` A0 | 21,541–22,058 (18 %) | solo 100 % | — |
| `e1-03-rbac` A1/A2 | 110,957–113,709 (92–95 %) | 0.25–0.39 each | 2–3 of 3 workers |

Answers to the questions the stage asks:

* **Who hits the limit first?** The worker with the largest remaining demand, not a fixed one; in
  `e1-01` it was `validate` (32 % of spend, needed 9.9 k more), in `e1-03` all three in two runs.
* **Who consumes most?** No single hog: shares cluster at 0.24–0.53. `roles`/`loader` are usually
  the largest, `merge`/`service` the smallest.
* **Is one worker starving the others?** No evidence. The sum of demand simply exceeds the budget:
  three workers each demand roughly **1.5×** what the single worker needs for the entire task
  (`e1-01`: 34.9 k per worker vs 22.9 k for the whole task).
* **Does the budget run out before delivery?** In `e1-01` no: `required_artifacts_present` and
  `hidden_tests_passed` are true in 2/2 A1 and 2/2 A2 runs, but `budget_exceeded` marks the run as
  failed. **This is a harness verdict, not a functional failure** — exactly the distinction the
  stage demands. In `e1-03` the budget stopped workers before their artifacts were correct
  (0/2 oracle in both split arms).
* **Does a worker keep spending after the artifact is correct?** Yes, in the self-review phase:
  every run continues for 2–9 tool calls after the last write, and in `e1-01` that review is what
  pushes the run over the limit.

## 5. Repeated work

| Family | Runs | Shared file touches per run | Shared exact commands per run |
|---|---|---|---|
| P1 read-only analysis | 6 | 1.0 | 0.2 |
| P2 independent modules | 6 | 2.5 | 0.3 |
| P3 frozen interface | 6 | 3.2 | 0.0 |
| `e1-01-config` | 6 | 1.2 | 0.2 |
| `e1-03-rbac` | 6 | 0.0 | 0.0 |

"Touch" means the path appears in a worker's read/write/edit/bash input. Examples:

* P3: both workers touch `README.md`, `docs/INTERFACE.md`, **both** modules and both `__init__.py`
  files — the whole discovery phase is duplicated, including the sibling's file (3/3 split runs).
* P2: in 3 of 6 runs both workers touch both modules (5 shared paths each); in the other 3 each
  stayed inside its own file.
* P1: duplication is limited to `README.md`; the two report files are disjoint.

The duplicated exploration is **small in tokens** (a `cat` of two 3 kB files is ~1.5 k tokens
once) but it costs **turns**, and turns are what compound. Exact command duplication (0–0.3 per
run) is negligible; the duplication is semantic, not literal, which is why a naive "same command"
detector would report almost nothing.

## 6. Context sufficiency audit

28 required-fact checks across the three positive families (fact → public source → delivered →
read → failure relation):

| Case | Count |
|---|---|
| `READ` (source was read by the worker) | 27 |
| `AVAILABLE_BUT_NOT_READ` | 1 |
| `NOT_DELIVERED` | 0 |

The single non-read case is `p2-money-contract` in `r05-p2-modules` A1 rep 0: the money worker never
opened its own stub's docstring section (it read the file through `cat` later) — and the rule it
missed is also stated in the stub docstring it edited. There is no case where the information the
worker needed was withheld.

The P3 case is the clearest refutation of a context explanation: in `r05-p3-frozen-interface` A1
rep 0 and A2 rep 0, the `clock` worker **read `docs/INTERFACE.md` first** (its very first action),
then implemented a March-based civil-date algorithm, tested it, saw `to_epoch("1970-01-01") == 306`,
computed the correct offset (`719162`), and kept debugging until it hit `doom_loop` (rep 0) or the
14-turn limit (A2 rep 0). That is `CONTEXT_READ_BUT_MISUNDERSTOOD` at best, and mostly an
implementation/verification economics problem.

No hidden-oracle content was used to derive a worker-visible requirement: every `required_fact`
above is stated in a file that was staged into the sandbox (`catalog/*.py`, `ledger/*.py`,
`docs/INTERFACE.md`).

## 7. Deliverable and integration audit

Distinct states, computed per worker from the exported workspace and the action log:

| State | Meaning | Count |
|---|---|---|
| `EXECUTION_COMPLETED` | worker stop code `success` | 32 |
| `DELIVERABLE_PRESENT` | declared artifact exists at export | 35 |
| `DELIVERABLE_VERIFIED` | artifact changed from the stub and no `NotImplementedError` remains (AST) | 21 |
| `INTEGRATION_VERIFIED` | some worker executed a cross-module command successfully | 12 runs |
| `ORACLE_PASSED` | hidden tests pass | 19 |

Deliverable states: 21 `IMPLEMENTED`, 11 `PRESENT` (markdown reports), 3 `STUB`, 1 `MISSING`.

Three of the four `STUB`/`MISSING` cases are **write-then-disappear**: the worker's `file_write`
event is `COMPLETED` with a byte count, yet the file is the untouched stub at export:

| Run | Victim artifact | What actually happened |
|---|---|---|
| `r05-p2-modules` A2 rep 0 | `ledger/money.py` | `entries` ran `git checkout -- ledger/money.py` at 08:00.1 ("--- money.py restored ---"); `money` then failed its next edit with *"The target string was not found"* and stopped `side_effect_unknown` |
| `r05-p3-frozen-interface` A2 rep 1 | `consumers/report.py` | `clock` ran `git checkout -- consumers/report.py` ("--- restoring consumer ---") after its own sandbox check |
| `r05-p3-frozen-interface` A1 rep 1 | `consumers/report.py` | the same pattern, but the 14 actions of the `clock` worker have no recorded input, so the exact command is `UNAVAILABLE`; the artifact state and the completed write are still provable |
| `r05-p1-analysis` A1 rep 0 | `reports/pricing.md` | never written: the `pricing` worker stopped `doom_loop` ("Detected repeated tool-call pattern … x3") after 12 turns |

Both visible reverts are unambiguous in the trajectory and in the exported workspace; they are
**authorised actions** (the path is inside the workspace, M3 allows it) that destroy a sibling's
work. The router's `SHARED_WRITE_RISK` gate models declared write paths and cannot see this: the
victim's path was never declared by the reverting worker.

Integration: no fixture ships a public test suite, so `PUBLIC_TEST_NOT_RUN` is **not** raised (0/30
runs had anything to run). What workers do instead is ad-hoc `python3 - <<'EOF'` probing:

| Arm | Ad-hoc verification attempts | Of which failed | Runs whose commands touch ≥2 modules |
|---|---|---|---|
| Track A A0 | 26 | 13 | 4/6 (P2 2/2, P3 2/2; P1 has nothing to integrate) |
| Track A A1 | 43 | 12 | 5/6 |
| Track A A2 | 42 | 15 | 5/6 |
| Track B A0 | 5 | 0 | 4/4 |
| Track B A1 | 4 | 0 | 2/4 |
| Track B A2 | 6 | 3 | 2/4 |

The single worker can always run the composition because it wrote both modules; the split workers
often can too — the `summary` worker even injected a reference provider stub into a temporary
directory to test its consumer independently. But in P3 the *provider* was wrong, and only the
consumer tested the composition: it caught nothing, because its own stub was the reference.

## 8. Per-task analysis

### P1 — independent read-only analysis

* Two workers cost +10 % over one ($0.00891 vs $0.00807 per run) for the same 2/2 oracle pass.
* Prompt tokens rise from 38.1 k to 51.0 k per run (+34 %). Fixed overhead doubles (4.3 k vs 2.3 k),
  ~16 % of that increase; the rest is turns (15.5 per run vs 9).
* Duplication is confined to `README.md`; the two analysis targets are genuinely disjoint.
* Wall window: 21.8 s vs 28.3 s in rep 0 (−23 %), 28.5 s vs 27.5 s in rep 1 (+4 %). One favourable
  repetition out of two — not a stable advantage, but the only family where the split is close to
  break-even.

### P2 — independent module implementation

* The two modules are independent in the DAG and in the artifact set, but in 3 of 6 runs both
  workers touched both files (5 shared paths per run), so the exploration was duplicated even
  though the contracts were not coupled.
* A1 rep 1 and A2 rep 0 both fail on `parse_entry(" 2024-01-05 , -9 , rent ")`: a field-stripping
  detail that the `r0.5.2` contract states explicitly — an implementation miss in both arms, not a
  split-specific failure.
* The destructive case is A2 rep 0: `entries` reverted `ledger/money.py`, `money` then died with
  `side_effect_unknown`. Attribution: `WORKSPACE_INTERFERENCE` (HIGH), `DELIVERABLE_INCOMPLETE`,
  `REDUNDANT_EXPLORATION`.

### P3 — frozen interface

Per-worker trace of the decisive runs:

| Run | `clock` | `summary` | Outcome |
|---|---|---|---|
| A0 rep 0 | — | — | solo worker implements both, runs 2 integration checks, passes |
| A1 rep 0 | reads `docs/INTERFACE.md` first, 11 turns, epoch offset 306, self-tests 8×, `doom_loop` | 9 turns, injects a reference provider in `/tmp`, tests itself, `success` | oracle fails on `to_epoch` |
| A2 rep 0 | 14 turns, same epoch error, self-tests, `max_turns` | 9 turns, `success` | oracle fails on `to_epoch` |
| A1 rep 1 | 13 turns, `budget_exceeded`, artifact implemented | writes its file, `success`; its artifact is later reverted | oracle fails on `summarize` |
| A2 rep 1 | 12 turns, `success`, artifact implemented | 7 turns, `provider_unavailable` (connection error), artifact reverted by `clock` | oracle fails on `summarize` |

**Does P3 support Context Pack or deliverable verification?** Deliverable/integration verification,
and specifically *provider correctness plus workspace ownership*. The consumer had the contract, read
it, and implemented against it; the provider also read it and still shipped a wrong epoch. No worker
could verify the composition reliably (the consumer's only reference was its own stub). Adding more
context to either worker would not have fixed `306`. A deterministic per-worker deliverable check
(does `to_epoch("1970-01-01")` return 0?) would have caught it before the turn limit; an ownership
guard would have prevented the revert that removed the consumer's finished file.

### `e1-01-config` / `e1-03-rbac` — budget

* Three workers spend 85–95 % of the shared budget; the solo worker spends 18–19 %.
* Per-worker demand is ~1.5× the total demand of the solo worker, so the budget is exhausted by
  *aggregate* work, not by an unfair split.
* `e1-01`: the artifacts are correct in all six split runs (oracle 4/4 across A1+A2) but the run is
  marked failed by `budget_exceeded`. `e1-03`: the artifacts are wrong in all four split runs and
  the workers are stopped mid-implementation.
* Fixed overhead for three workers is 21.0 k (5.1 % of spend). These tasks are simply too small for
  three independent explorations — the same conclusion as P2/P3, from the other direction.

## 9. Failure mechanism classification

Eleven runs failed the oracle; each carries several symptoms and one dominant mechanism.

| Primary | Runs | Codes also present |
|---|---|---|
| `BUDGET_EXHAUSTED` | 5 | `IMPLEMENTATION_ERROR`, `INTEGRATION_NOT_VERIFIED`, `CONTEXT_READ_BUT_MISUNDERSTOOD` |
| `WORKSPACE_INTERFERENCE` | 2 | `DELIVERABLE_INCOMPLETE`, `INTEGRATION_FAILED`, `REDUNDANT_EXPLORATION`, `CONTEXT_READ_BUT_MISUNDERSTOOD` |
| `DELIVERABLE_INCOMPLETE` | 2 | `BUDGET_EXHAUSTED` / `CONTEXT_READ_BUT_MISUNDERSTOOD`, `INTEGRATION_FAILED` |
| `CONTEXT_READ_BUT_MISUNDERSTOOD` | 1 | `IMPLEMENTATION_ERROR`, `INTEGRATION_FAILED` |
| `MAX_TURNS` | 1 | `CONTEXT_READ_BUT_MISUNDERSTOOD`, `IMPLEMENTATION_ERROR`, `REDUNDANT_EXPLORATION` |

Every reason in `experiments/evidence/stage-r1-0/attribution.json` carries `symptom`, `evidence`,
`confidence` and `counterevidence`. Confidence is `HIGH` only for facts read directly from records
(stop codes, artifact bytes, action log, budget ledger); mechanism-level claims such as
`INTEGRATION_NOT_VERIFIED` are `MEDIUM`.

What is a fact, what is an inference:

* **Fact** — 21/58 worker stops are `budget_exceeded`; 591 actions happened, 365 were persisted;
  three artifacts were a stub at export after a completed write; no worker ran a public test;
  `to_epoch("1970-01-01")` returned 306.
* **Inference (supported)** — the budget is exhausted by aggregate per-worker demand, not by one
  greedy worker (shares are balanced and the ledger records the exact rejection).
* **Inference (supported)** — the two visible `git checkout` commands caused the two artifacts to
  disappear (command, timestamp, artifact state and the sibling's subsequent failure all align).
* **Hypothesis (unverified)** — that the third vanished artifact (P3 A1 rep 1) had the same cause;
  the `clock` worker's commands are not recorded and no other worker touched that path.

## 10. Where the evidence is insufficient

1. **187 of 591 actions have no input text** (31 %). They belong to workers the budget guard
   stopped; their effect is only visible through artifact state and outputs recovered from a sibling
   run. Command-level attribution for those workers is impossible.
2. **No wall-clock timestamps on provider calls.** Token/cache attribution is exact, but "which
   provider request overlapped which" remains `INFERRED` from worker intervals.
3. **Per-message token counts do not exist.** The split between conversation history and tool
   results inside a prompt is a character share, not a token count. Error direction: unknown.
4. **The oracle cannot attribute a hidden-test failure to a worker.** `hidden_tests_passed` is a
   single boolean for the whole composition; the worker-level link in P3 is established through the
   worker's own probes and the artifact AST, not through per-test results.
5. **No counterfactual.** These are the runs that happened; nothing was re-run to test a hypothesis.
6. **n = 2 per cell.** Every statement above is directional.

## 11. Minimal fix options

### Direction A — Context Pack

```text
observed_problem   both workers re-discover the same repository and re-read the sibling's files
evidence           P3: 3.2 shared file touches/run; P2: 2.5; P1: 1.0. In 27/28 checks the worker
                   read what it needed, so the loss is cost, not correctness
proposed_mechanism a task-scoped, pre-rendered context pack (repo map + the files the task names)
                   delivered as a stable prefix
minimal_change     renderer/selector only; no scheduler, no budget, no oracle change
predicted_benefit  turns per worker could drop; NOT quantified here (hypothesis only)
possible_regression worse cache locality if the pack differs per worker; risk of stale context
ablation_design    A1 vs A1+pack on P3 and e1-01, same policy, same budget, 2 reps
```

Verdict: **not the next step.** It addresses a cost that is real but small per run (a few thousand
tokens of duplicated reads) and does not explain any observed failure.

### Direction B — Deliverable and ownership verification

```text
observed_problem   3 of 4 stub/missing artifacts were written and then vanished; 2 of them because
                   a sibling ran `git checkout -- <file>`; the third worker looped without writing
evidence           completed file_write + STUB at export + visible revert command (2 runs); AST
                   detects the stub; 0 public tests exist to catch it
proposed_mechanism (1) per-worker ownership check on write/revert actions (reject a command that
                   touches a path outside the worker's declared set), and (2) a deterministic
                   post-run deliverable check (artifact exists, AST has no NotImplementedError,
                   declared callable answers one contract example)
minimal_change     M3 rule scope + an evaluation-side check; no prompt change, no extra worker
predicted_benefit  catches the 3 stub/lost artifacts and the 2 interference runs; NOT quantified
possible_regression a worker legitimately cleaning up its own scratch files could be blocked;
                   the deliverable check could duplicate the oracle if it grows
ablation_design    re-run P2/P3 A1/A2 with the guard on/off, 2 reps, same budget and prompts
```

Verdict: **the cheapest correctness win**, and it is a runtime invariant rather than a model change.

### Direction C — Budget and worker economics

```text
observed_problem   21/58 worker stops are budget_exceeded; three workers spend 85-95% of the shared
                   budget for 3x the work and no correctness gain; each worker demands ~1.5x what
                   the solo worker needs for the whole task
evidence           Track B used 101,641-113,709 of 120,000; solo 21,541-23,329; per-worker shares
                   0.20-0.53 (no single hog); e1-01 artifacts correct but run marked failed
proposed_mechanism per-worker budget envelopes (or a reservation that reserves the remaining work
                   of a worker already in its final phase) plus a minimum task-size threshold
                   before delegation is attempted
minimal_change     G2a budget policy + router threshold; no new agent, no prompt change
predicted_benefit  prevents "correct artifact, failed run" and stops spending on splits that cannot
                   amortise their own exploration; NOT quantified
possible_regression per-worker envelopes can starve the worker that does the integration; a size
                   threshold is another tuned constant
ablation_design    e1-01/e1-03 A1/A2 with per-worker envelopes vs shared, 2 reps
```

Verdict: **the largest measured effect** and the only direction that changes the verdict of runs
whose artifacts were already correct.

## 12. Recommended next stage

**Fix worker economics before context.** Concretely, R1 should:

1. add a deterministic deliverable/ownership check (Direction B) because it is a correctness
   invariant that costs nothing at inference time;
2. make the budget per-worker or reservation-aware (Direction C), so a correct artifact is not
   reported as a failed run and a 3-worker split cannot spend 5× the single-worker cost;
3. only then measure whether a Context Pack reduces turns (Direction A), as a **cost** experiment,
   not as a correctness fix.

Shrinking multi-worker use further is also justified by this data: P1 (read-only, no integration)
is the only family where two workers came close to break-even. P2/P3 and the width-3 tasks are below
the size at which independent exploration amortises. The router should therefore keep refusing
plans that lack evidence *and* the scheduler/economics layer should refuse plans that cannot pay for
their own context — a threshold the R0 gates do not currently express.

---

## 13. Tests and regression

```text
.\.venv\Scripts\python.exe -m pytest                      701 passed, 1 skipped, 1 deselected (…s)
.\.venv\Scripts\python.exe -m pytest tests/unit/test_stage_r1_0_analysis.py   16 passed
.\.venv\Scripts\python.exe -m ruff check .                All checks passed
.\.venv\Scripts\python.exe -m ruff format --check .       352 files already formatted
python -m pyright                                         0 errors, 0 warnings
```

The 16 new tests cover the two joins (exact/multiset/ambiguous), the stub-with-docstring AST case,
the three deliverable states, path normalisation, sibling-revert detection (true and false
positives: `git stash list` and own files must not match), the whole-tree reset, duplicate-touch
counting, a synthetic end-to-end run that loses a worker's calls and flags the interference, and a
read-only guarantee that `collect()` leaves every run directory unchanged.

No existing test was weakened, deleted or skipped. The raw run directories are untouched: the
read-only test compares mtimes before and after collection.

## 14. GitHub delivery

* Track A: PR #2 (prerequisite, base `master`) and PR #1 retargeted onto it (13-file R0.5 diff).
* Track B: new branch `analysis/r1-0-failure-attribution` with the analyzer, its tests and this
  report, plus the sanitized evidence directories.
* No force-push, no history rewrite, no automatic merge, `master` untouched.

---

## Verdicts

| Dimension | Verdict | Basis |
|---|---|---|
| A. Input data integrity | **PASS with a documented defect** | 30/30 runs complete; 226/591 actions missing from `result.json`, recovered from the trajectory; 187 inputs `UNAVAILABLE` and labelled |
| B. Worker execution ledger | **PASS** | 58 workers reconstructed with tokens, turns, tools, stops, budget share and deliverable state |
| C. Token attribution | **PASS** | 533 exact + 4 multiset request joins; fixed overhead vs growth separated; no estimated token is presented as measured |
| D. Context sufficiency | **FAIL as a hypothesis** | 27/28 facts read; the single gap is a stub docstring the worker edited anyway; P3 read the contract and still shipped `306` |
| E. Deliverable/integration | **FAIL** | 3 stub/missing artifacts after completed writes, 2 proven sibling reverts, 0 public tests, 5 integration failures |
| F. Budget economics | **FAIL** | 21/58 worker stops; 85–95 % budget for 3 workers; per-worker demand 1.5× the whole solo task |
| G. Review readiness | **PASS** | stacked PRs, reproducible evidence, offline CI, no secrets |

**Architecture decision: fix worker economics and deliverable ownership before context.** The data
does not support building a Context Pack next: workers already receive and read the information they
need. It supports making delegation pay for itself — per-worker budget accounting, a deterministic
deliverable check, and workspace ownership — and it supports keeping multi-worker delegation
narrower than the current formality-based independence test allows.
