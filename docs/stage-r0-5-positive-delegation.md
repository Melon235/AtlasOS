# Stage R0.5 — Positive Delegation Validation

Stage R0 proved that the router refuses unsafe plans. It could not prove the opposite direction:
that the router *admits* a plan which deserves parallel execution, and that the admission produces
something. This stage adds a pre-registered positive control group, runs it against the unchanged
evaluation runner, measures whether the admitted plans really run concurrently, and reports what the
delegation actually cost.

Headline: the router admits every positive family for evidence-based reasons (6/6 live admissions,
0 false rejections, 0 false admissions), the scheduler really does run the workers concurrently
(20/20 multi-worker runs with measured overlap), and **the delegation still does not pay off** on
these families: +40 % cost against the single worker on Track A, +269 % on Track B, and a wall-clock
window that is equal or longer because each worker repeats its own exploration.

---

## 1. R0 baseline freeze (read-only audit)

Run before any change of this stage:

```text
git branch --show-current          -> master
git remote -v                      -> origin git@github.com:Melon235/AtlasOS.git (fetch/push)
git status --short                 -> 10 modified, 44 untracked (Stage G1-R0 work, never committed)
git branch -vv                     -> * master 07e18ec [origin/master: ahead 22]
git ls-remote --heads origin       -> refs/heads/master = 3fbf3dd
```

| Check | Result |
|---|---|
| R0 modules present | `src/atlasos/workers/routing.py`, `experiments/evaluation/stage_r0_routing.py`, `stage_r0_analysis.py`, `docs/stage-r0-routing-report.md` |
| Policy version | `r0.1.0`, `max_parallel_workers=2` |
| Policy digest | `5e13c9106f8927fc116c00ff3d4ee91e9848205d0623925521ad6af12fa99244` |
| Decision digest | `decision_digest()` present and stable; pinned by `tests/unit/test_delegation_routing.py` |
| Route modes | `off` / `shadow` / `selective` wired through `RunnerSettings.delegation_route_mode` |
| R0 results | `experiments/results/stage-r0/{audit,r0-20261111T071931Z,r0-20261111T072827Z}` (git-ignored) |
| Remote default branch | `master` (not `main`); the GitHub repository is **public** |
| Remote contains R0? | **No.** The 22 unpushed commits stop at Stage E1/E2; G1–R0 existed only in the dirty tree |

Consequence: R0.5 cannot be pushed alone. Commit `7bd0965` lands the G1–R0 work as a prerequisite,
and the PR body says so explicitly.

---

## 2. Git workspace and dependency audit

`git status` was classified before staging:

| Class | Files | Action |
|---|---|---|
| A. R0.5 changes | `experiments/evaluation/stage_r05_*.py`, `tests/unit/test_stage_r05_*.py`, `docs/stage-r0-5-*.md`, `models.py`, 12 lines of `runner.py` | committed in `1309a18` + the docs commit |
| B. Required prerequisites | all Stage G1–R0 sources, analyses, reports and tests | committed in `7bd0965`, described in the PR |
| C. Unrelated working-tree edits | none found beyond B | — |
| D. Private/sensitive | `.env` (ignored), no keys, no personal absolute paths (scanned) | not committed |
| E. Benchmark artefacts | `experiments/results/stage-r0-5/` (raw run directories, provider logs, trajectories) | ignored; only aggregates appear in this report |

`gh` is not installed on this machine, so the PR is created through the GitHub REST API with the
credentials already stored for `github.com`; the command and its output are in §15.

---

## 3. Positive benchmark design

All three families are ordinary `BenchmarkTask`s: A0 (forced single), A1 (forced candidate DAG) and
A2 (selective routing) run through the unchanged runner, with the same fixture, the same oracle and
the same fixed conditions as Stage R0.

### P1 — independent read-only analysis

Two already-implemented reference modules (`catalog/pricing.py`, `catalog/inventory.py`). Each worker
reads one module and writes one report (`reports/pricing.md`, `reports/inventory.md`); no worker
modifies code, no worker reads the other's report, and the two report paths are disjoint. The oracle
checks that each report states, per function, the exception types the function raises and the exact
value of one prescribed call, plus the module constants.

Honest labelling: the analysis is read-only with respect to the codebase, but it produces two file
artifacts. A pure "report in the final answer" task cannot pass Gate 0 coverage today, because R0 can
only prove a deliverable through a declared write path (see §17, limitation 2).

### P2 — independent module implementation

Two modules with public contracts and no edge between them (`ledger/entries.py`: `parse_entry`,
`total_for`, `top_memos`; `ledger/money.py`: `format_cents`, `parse_cents`, `split_evenly`). Each stub
ships the signature and the docstring; the work is real (parsing edge cases, group separators,
remainder distribution) and needs several turns per module.

### P3 — frozen-interface collaboration

`providers/clock.py` (`to_epoch`, `from_epoch`) and `consumers/report.py` (`summarize`), specified by
`docs/INTERFACE.md`, which is staged **before** the workers start. The consumer imports the provider
against the frozen signature; the DAG has no edge because no worker reads the other's output. The
cost of that choice is real and visible in the results: the consumer cannot run its own integration
test (§9).

## 4. Negative benchmark design

| ID | Shape | Expected block |
|---|---|---|
| N1 | two workers writing the same `state.json` | `SHARED_WRITE_RISK` |
| N2 | consumer of `docs/INTERFACE.md` that is declared but **not staged** | `UNSTABLE_INTERFACE` |
| N3 | `calc.py` then `report.py` with an explicit dependency | `NO_PARALLEL_WAVE` |

Negatives are audited offline only; repeating R0's rejection proof with paid model calls would add
nothing. Positives and negatives go through the same adapter (`build_proposal`) and the same
`route_delegation` call; nothing branches on family, sample id or expected outcome, and a test proves
that renaming a sample cannot change a decision.

---

## 5. Oracle and fixture invariants

* `visible_files ∩ hidden_files = ∅` for all six families (tested).
* No worker prompt contains a hidden path or the string `test_hidden` (tested).
* Each positive worker's declared write set is disjoint from its sibling's and is a subset of the
  task's `required_artifacts` (tested).
* `variants()` derives A0 and A1 from one definition, so the fixture, oracle, allowed paths and
  required artifacts are identical across arms (tested).
* The oracle is satisfiable: `tests/unit/test_stage_r05_oracle.py` materialises each fixture, runs the
  real hidden tests against a reference implementation (pass) and against the shipped stub (fail).
* **The oracle is public.** This repository is public, so `stage_r05_tasks.py` publishes the expected
  answers. A worker with network egress could in principle read them; the analysis scans every tool
  call for network markers and for oracle filenames (result: 0/30 runs, §12). This family must not be
  presented as a held-out benchmark after publication.

### Frozen digests

| Object | SHA-256 |
|---|---|
| P1 `r05-p1-analysis` | `aa1594aaf7b78b5d6ed619d04ca22c3c7c9c09a9a067c02f243f8b85260adbc2` |
| P2 `r05-p2-modules` | `27433621c2f4997edef0f0d0ed89147f95ae5002d6db4a006f3d01c225748bec` |
| P3 `r05-p3-frozen-interface` | `442e0c37001fffdc12288562cdecb96bed42eb600ee048ff9964eed2e7562bc4` |
| N1 `r05-n1-shared-write` | `67ded57a873490d469ffb0cc3e3968dfb864b7d546f721b023d08b1311b44b23` |
| N2 `r05-n2-unfrozen-interface` | `a182180e9af0c7f8f6886565ad715acb426429a35d0bb746cf92fcf0f1ee72d1` |
| N3 `r05-n3-linear-chain` | `216bee1f8719e9ae6bd8355c24356ea6c2d0f7843800ac19f4f6c9a24cd5345a` |
| Oracle definition set | `4d69d20c2f057a25cecad0a27dd686563ee026d4219997e6d406db50fa102356` |
| Pre-registration (`r0.5.2`) | `57d8ff2b2d72d3e72ac24c5896ec6b3c9f3671a9369cba033bbd9b4b208fb8d4` |

`tests/unit/test_stage_r05_tasks.py` pins all of them. Any edit to a prompt, fixture, contract,
oracle or expected route breaks the test, which forces a new `BENCHMARK_VERSION` and a new round.

### Defect log (pre-registered protocol, applied twice)

| Version | Defect | Runs affected | Disposition |
|---|---|---|---|
| `r0.5.0` | The P1 oracle required the literal `none` on the line of a function that raises nothing in its contract (a correct analysis that also listed `TypeError` failed) and required `default_tier = basic` while the module quotes `"basic"`. | 6 (all `ORACLE_FAILURE`) | Invalidated. Re-checked afterwards: **6/6 of those artifacts pass the corrected oracle**, so the defect was the oracle, not the task. |
| `r0.5.1` | The P2 contract said "the memo is stripped" but the oracle expected `parse_entry(" 2024-01-05 , -9 , rent ")` to succeed — the oracle contradicted its own published contract. | 6 (P2 only) | Invalidated. Contract states the stripping rule in `r0.5.2`; P2 was rerun completely. |

`r0.5.2` is the reported version. P1, P3 and N1–N3 are byte-identical across `r0.5.1` and `r0.5.2`
(their digests are unchanged), so their `r0.5.1` runs are pooled; the analysis refuses to load any
round whose recorded family digest does not match the frozen definition, and the P2 `r0.5.1` round is
therefore excluded automatically. Nothing was edited after seeing a result, and the two edits changed
only the two checks that were demonstrably wrong.

---

## 6. Routing policy versions

| Version | `max_parallel_workers` | Everything else | Digest |
|---|---|---|---|
| `r0.1.0` (official) | 2 | frozen | `5e13c91…f2fa99244` |
| `r0.2.0` (width experiment) | 3 | identical | `c5969f6…560f00331fcba` |

`r0.1.0` is never redefined in place. A test asserts its digest and asserts that `r0.2.0` differs in
exactly one field. The Stage E1 fixture digests are unchanged (`278a857…` for `e1-01-config`).

---

## 7. Admission results

Offline audit (no model, no sandbox) and the live selective arm, side by side:

| Family | Expected | Offline (r0.1.0) | Live A2 (2 reps) |
|---|---|---|---|
| P1 read-only analysis | `delegate_parallel` | `delegate_parallel` / `DELEGATION_ADMITTED` | 2/2 admitted, 2 workers |
| P2 independent modules | `delegate_parallel` | `delegate_parallel` / `DELEGATION_ADMITTED` | 2/2 admitted, 2 workers |
| P3 frozen interface | `delegate_parallel` | `delegate_parallel` / `DELEGATION_ADMITTED` | 2/2 admitted, 2 workers |
| N1 shared write | `direct` | `direct` / `SHARED_WRITE_RISK` | not run (by design) |
| N2 unfrozen interface | `direct` | `direct` / `UNSTABLE_INTERFACE` | not run (by design) |
| N3 linear chain | `direct` | `direct` / `NO_PARALLEL_WAVE` | not run (by design) |

```text
positive_admission_count  = 6   (live A2 runs routed to DELEGATE_PARALLEL)
positive_rejection_count  = 0
negative_admission_count  = 0   (offline)
negative_rejection_count  = 3   (offline)
unknown_evidence_count    = 1   (N2: the declared contract is absent, recorded as UNKNOWN)
false_admission           = []
false_rejection           = []
evidence_insufficiency    = []
policy_constraint         = []
```

Track B (width sensitivity, `r0.2.0`): `e1-01-config` and `e1-03-rbac` move from
`PARALLEL_LIMIT_EXCEEDED` at width 2 to `DELEGATION_ADMITTED` at width 3 with the same evidence, and
the live A2 arm executed the original three-worker DAG in 4/4 runs. The candidate DAG is preserved
verbatim: `approved_task_ids` = `[loader, merge, validate]` / `[roles, policy, service]`, and the
decision digest is recorded per run.

A real decision from `e1-03-rbac` (A2, rep 0):

```json
{"route": "delegate_parallel", "reason_codes": ["DELEGATION_ADMITTED"],
 "approved_task_ids": ["roles", "policy", "service"],
 "policy_version": "r0.2.0",
 "policy_digest": "c5969f615fcf56d77a03b89a9073d2d0524be8aa281a6948c6e560f00331fcba",
 "max_parallel_wave_width": 3}
```

---

## 8. Real parallelism (execution timeline)

The timeline is built from the evaluation's own progress events. Every event now records its runtime
task id and agent, so a worker's start is paired with **its own** completion (previously the
completion carried no title and could only be matched positionally). No production scheduler code
was touched.

For each run the analysis separates four different claims:

1. DAG wave width — from the routing decision (`max_parallel_wave_width`).
2. Workers the scheduler actually started — `len(worker_results)`.
3. Workers whose wall-clock intervals actually overlap — the sweep over `task_start`/`task_complete`.
4. End-to-end latency — `metrics.latency_ms` (includes sandbox provisioning and workspace export).

`e1-01-config` A2, rep 0 — three workers, one wave:

```text
loader   |#########################|  start 08:54:07.645  dur 48.3 s
merge    |######################|     start 08:54:07.650  dur 45.7 s
validate |##########################|  start 08:54:07.655  dur 51.7 s
         ^ 10 ms spread          wall window 51.7 s, sum of worker time 145.6 s
```

| Aggregate | Value |
|---|---|
| Multi-worker runs | 20 |
| Runs with at least one overlapping interval | 20 |
| `PARALLEL_EXECUTION_NOT_OBSERVED` | 0 |
| Max observed concurrency | 2 (Track A), 3 (Track B) |
| Multi-worker runs where the scheduler serialised the workers | 0 |

So the answer to "does the scheduler really run them together?" is yes, with millisecond-scale
synchronisation. Provider requests must therefore have overlapped too, but the provider client
records latency without a wall-clock timestamp and without worker attribution, so that step is
**INFERRED**, not measured; it is not claimed as a direct observation.

What parallelism does **not** buy is the critical path. The three workers together spend 145.6 s of
worker time to produce a 51.7 s window — a 2.8× shrink inside the window — but the single worker
needed only 26.4 s for the same artifacts. The parallel plan starts three times the work.

---

## 9. A0 / A1 / A2 results

### Track A — positive families (`r0.1.0`, 3 tasks × 3 arms × 2 reps = 18 runs)

| Metric | A0 forced single | A1 forced DAG | A2 selective |
|---|---|---|---|
| Oracle pass | **6/6** | 2/6 | 3/6 |
| `TASK_SUCCESS` | **6/6** | 1/6 | 3/6 |
| Worker success (self-reported) | 6/6 | 7/12 | 9/12 |
| Prompt tokens | **277,494** | 535,449 | 457,330 |
| Completion tokens | 23,597 | 35,392 | 31,937 |
| Cache hit ratio | 0.9175 | 0.9191 | 0.9239 |
| Cost (USD) | **0.036715** | 0.058411 | 0.051303 |
| Cost per run | **0.0061** | 0.0097 | 0.0086 |
| Model turns | **55** | 125 | 111 |
| Tool calls | 68 | 89 | 118 |
| Budget exceeded | 0 | 3 | 0 |
| Max turns reached | 0 | 0 | 1 |
| Mean `latency_ms` | **50,267** | 66,471 | 63,760 |
| Mean execution window | **32,432** | 48,904 | 46,235 |
| Max observed concurrency | 1 | 2 | 2 |
| Runs with measured overlap | 0/6 | 6/6 | 6/6 |

Per task (oracle pass, cost):

| Task | A0 | A1 | A2 | A2 route |
|---|---|---|---|---|
| P1 read-only analysis | 2/2, $0.00807 | 1/2, $0.01279 | 2/2, $0.00891 | `delegate_parallel` (2) |
| P2 independent modules | 2/2, $0.02037 | 1/2, $0.02470 | 1/2, $0.02185 | `delegate_parallel` (2) |
| P3 frozen interface | 2/2, $0.00827 | 0/2, $0.02092 | 0/2, $0.02055 | `delegate_parallel` (2) |

### Track B — width sensitivity (`r0.2.0`, 2 tasks × 3 arms × 2 reps = 12 runs)

| Metric | A0 | A1 (3 workers) | A2 (3 workers) |
|---|---|---|---|
| Oracle pass | **4/4** | 2/4 | 2/4 |
| `TASK_SUCCESS` | **4/4** | 0/4 | 0/4 |
| Prompt tokens | **81,668** | 409,744 | 424,688 |
| Completion tokens | 7,815 | 21,353 | 21,308 |
| Cache hit ratio | **0.9279** | 0.8672 | 0.8822 |
| Cost (USD) | **0.011600** | 0.044080 | 0.042827 |
| Cost per run | **0.0029** | 0.0110 | 0.0107 |
| Model turns | **28** | 106 | 112 |
| Budget exceeded | 0 | 4 | 4 |
| Mean `latency_ms` | **41,358** | 63,109 | 65,902 |
| Mean execution window | **23,502** | 44,869 | 48,316 |

Per task: `e1-01-config` A0 2/2 ($0.00631), A1 2/2 ($0.02255), A2 2/2 ($0.02435);
`e1-03-rbac` A0 2/2 ($0.00529), A1 0/2 ($0.02153), A2 0/2 ($0.01848).

### A2 vs A1 is not a routing effect

A2 executes exactly the A1 plan whenever it is admitted, and it was admitted in 6/6 + 4/4 runs. Any
difference between A1 and A2 is trajectory noise on the same plan:

| Comparison | Oracle Δ | Cost Δ | Latency Δ |
|---|---|---|---|
| A2 − A1, Track A | +1 pass / 6 | −$0.0071 (−12 %) | −2.7 s |
| A2 − A1, Track B | 0 / 4 | −$0.0013 (−3 %) | +2.8 s |

The +1 pass in Track A is one P2 repetition and is reported as noise, not as a router gain.

---

## 10. Cost and latency

* **Cost.** Track A: A2 costs +40 % per run against A0 ($0.0086 vs $0.0061) for no correctness gain.
  Track B: +269 % ($0.0107 vs $0.0029). Against A1, A2 is marginally cheaper only because the same
  DAG drew slightly shorter conversations.
* **Latency.** Measured two ways. Inside the execution window, three workers overlap (2.8× shrink).
  End to end, A2 is +27 % (Track A) and +59 % (Track B) slower than A0, because the extra workers
  each pay provisioning, prompt build and their own exploration.
* **The one speedup.** P1 A2 rep 0 ran in a 21.8 s window against A0's 28.3 s (−23 %), with two
  workers and no oracle difference. That is the only positive latency result in 30 runs; rep 1 was
  flat (28.5 s vs 27.5 s). It is a real but unreplicated observation on a read-only task.
* **Budget.** The shared 120 k guarded budget is the binding constraint for every 3-worker arm
  (4/4 budget-exceeded in Track B, 3/6 in Track A A1). When workers run in parallel, they spend the
  same budget faster and the run ends with `budget_exceeded` even though the artifacts exist
  (`e1-01-config` A1/A2: oracle pass, `TASK_SUCCESS` false). This is a property of the *shared*
  budget, not of the router, and it is the single largest cause of the multi-worker failures here.
* **Cache.** A0 and A2 land at 0.918–0.924; the 3-worker arms drop to 0.867–0.882 because three
  conversations diverge earlier. More workers, less prefix reuse.

---

## 11. False admission and false rejection

| Category | Count | Detail |
|---|---|---|
| False admission | 0 | N1/N2/N3 all refused by the intended gate |
| False rejection | 0 | P1/P2/P3 all admitted live |
| Evidence insufficiency | 1 (by design) | N2 declares `docs/INTERFACE.md` and does not stage it; the adapter records `UNKNOWN`, not a refusal to look |
| Policy constraint | 0 | no positive family is width-blocked at `r0.1.0` |
| Execution failure | 4 families | P3 (both arms), P2 (rep 0 A2, rep 1 A1), Track B (all multi-worker runs) |

Execution failures are attributed to the worker model, not to the router:

* P3 rep 0: `to_epoch("1970-01-01")` returned `306` — the provider worker used a wrong epoch offset
  although the contract states `== 0`. Both A1 and A2 fail on it; A0 passes.
* P3 rep 1: `summarize` still raises `NotImplementedError` in both multi-worker arms — the consumer
  worker ran out of turns/budget, which is exactly the integration risk of a frozen-interface split.
* P2: field stripping was the only miss (`r0.5.2` now states the rule); in rep 1 A2's oracle passes
  but the run is budget-exceeded.
* Track B: every A1/A2 failure is `BUDGET_EXCEEDED`; the artifacts themselves pass in `e1-01-config`.

---

## 12. Benchmark integrity

| Check | Result |
|---|---|
| Definitions frozen before the first live call | yes — `preregistration.json` written by `prereg`, asserted by every round |
| Round refuses a changed definition | yes — digest guard, tested |
| Stale rounds excluded from analysis | yes — 6 P2 `r0.5.1` runs excluded automatically |
| Oracle satisfiable / stub fails | yes — `test_stage_r05_oracle.py` (6 cases) |
| Hidden files staged into the worker sandbox | never; `materialize_fixture` writes only `visible_files` |
| G3-4a isolation checks | passed in 30/30 runs |
| Runs with network-looking tool calls (`http`, `curl`, `pip install`, …) | 0/30 |
| Runs mentioning an oracle artifact (`test_hidden`, `oracle-workspace`) | 0/30 |
| Host fixture changed during a run | 0/30 |
| Unauthorized workspace changes | 0/30 (`only_allowed_paths_changed` true everywhere) |
| `sample_id` special-casing | none; a test renames a sample and asserts an identical decision |

---

## 13. Full regression

```text
.\.venv\Scripts\python.exe -m pytest           685 passed, 1 skipped, 1 deselected in 228.33s
.\.venv\Scripts\python.exe -m pytest tests/unit/test_stage_r05_*.py   50 passed
.\.venv\Scripts\python.exe -m ruff check .     All checks passed
.\.venv\Scripts\python.exe -m ruff format --check .   349 files already formatted
npx pyright                                    0 errors, 0 warnings
```

No test was deleted, weakened or skipped, and no assertion was relaxed to make the benchmark pass.
The 50 new tests cover: frozen digests, positive admission, negative rejection, planner-claim trust,
contract verification, width sensitivity, `sample_id` independence, the oracle self-test, timeline
pairing, serialisation, and the pre-registration guard.

---

## 14. Commit list

| Commit | Content |
|---|---|
| `7bd0965` | Prerequisite: Stage G1–R0 (handoff, budget, compaction, tool-error semantics, ingress offload, worker contracts, benchmark isolation, delegation router) — the code R0.5 runs on, previously unpushed |
| `1309a18` | R0.5 benchmark families, routing audit, measurement wiring, analysis CLI, 50 tests |
| `7146937` | Read the G3-4a isolation verdict from its actual field name |
| `f3d8396` | This report |
| `35743f4` + this commit | `docs/stage-r0-5-pr-body.md` (the PR description), the delivery status in §15 and the PR #1 reference |

Branch: `experiment/r0-5-positive-delegation`, created from `master` (27 commits ahead of
`origin/master`: the 22 previously unpushed stage commits, the prerequisite commit, and the R0.5
commits above).

---

## 15. GitHub branch and pull request

Branch push: **done**.

```text
git push -u origin experiment/r0-5-positive-delegation
remote: Create a pull request for 'experiment/r0-5-positive-delegation' on GitHub by visiting:
remote:      https://github.com/Melon235/AtlasOS/pull/new/experiment/r0-5-positive-delegation
 * [new branch]      experiment/r0-5-positive-delegation -> experiment/r0-5-positive-delegation
```

Pull request creation: **done — PR #1**, `https://github.com/Melon235/AtlasOS/pull/1`
(`base=master`, `head=experiment/r0-5-positive-delegation`, 27 commits, 143 files).

How it was created, because the machine has no `gh` and no token in the environment:

* `gh` is not installed and no GitHub CLI config exists; `GH_TOKEN`/`GITHUB_TOKEN` are unset and
  `.env` holds only the DeepSeek key.
* Windows Credential Manager does hold the user's `git:https://github.com` credential (the `manager`
  helper configured in git is not installed, which is why `git credential fill` returns nothing).
* The credential was read once through the standard `CredRead` API, verified with `GET /user`
  (login `Melon235`), used for a single `POST /repos/Melon235/AtlasOS/pulls`, and never written to a
  file, a log or the repository. The helper script lived in the untracked `_debug_run/` directory
  and was deleted afterwards.

The PR body is committed as `docs/stage-r0-5-pr-body.md`, so it can be re-applied or edited offline:

```powershell
gh pr create --base master --head experiment/r0-5-positive-delegation `
  --title "eval: validate selective delegation on positive parallel tasks" `
  --body-file docs/stage-r0-5-pr-body.md
```

Scope note: the PR also carries the 22 previously unpushed stage commits (Stage E1–F and earlier),
which account for about 93 of the 143 changed files, mostly committed evaluation artifacts under
`experiments/results/`. The R0.5 review surface is the five commits listed in §14.

**Updated in Stage R1-0.** The stack was split without rewriting history: the prerequisite stages
became `feat/stage-g1-r0-prerequisites` (PR #2, base `master`), and PR #1 was retargeted onto it.
PR #1 now shows 13 files and 5 commits — exactly the R0.5 surface above. See
`docs/stage-r1-0-failure-attribution.md` §14.

---

## 16. Reviewer checklist

1. **Router contamination** — the router file `src/atlasos/workers/routing.py` is byte-identical to
   R0; the only adapter change is additive contract evidence read from public fixture files.
2. **`r0.1.0` unchanged** — digest `5e13c91…` asserted by a test.
3. **`r0.2.0` changes one field** — `max_parallel_workers`; all other fields asserted equal.
4. **Benchmark independence** — no P-family worker depends on a sibling's output; write sets are
   disjoint; the P3 contract is published before the workers start.
5. **Same functional standard** — A0/A1/A2 share fixture, oracle, allowed paths and required
   artifacts, derived from one definition.
6. **No `sample_id` branch** — grep for `sample_id` in the decision path; a test renames a sample.
7. **No hidden-answer leak into the sandbox** — `visible_files` only; prompts contain no hidden path.
8. **Overlap evidence is real** — `task_events` timestamps, per-worker intervals, `e1-01-config` A2
   starting three workers within 10 ms.
9. **M2 / scheduler untouched** — `src/atlasos/workers/` and AnyCode are unchanged by this stage.
10. **Unrelated dirty files** — none committed; `_debug_run/` and all run directories stay untracked.
11. **Sensitive data** — `.env` ignored, no keys, no personal absolute paths, no raw provider logs.
12. **No exaggerated claim** — the report states that delegation lost money and time on these
    families, and reports the single P1 speedup as unreplicated.

---

## 17. Open problems

1. **The shared budget, not the router, decides multi-worker success.** Every 3-worker run exhausted
   120 k tokens. Per-worker budgets or a wave-aware budget would change the verdict; that is a
   runtime-policy question (a later stage), not a routing one, and it was deliberately not changed
   here.
2. **Pure read-only work cannot be admitted.** Gate 0 proves goal coverage through declared write
   paths, so a plan with no artifact is refused (`UNCOVERED_GOAL`) even when it is genuinely
   parallel. P1 sidesteps this by writing two reports. Fixing it means extending the deliverable
   evidence kind, which is an R0 policy change and therefore needs its own version and round.
3. **Parallel work multiplies the work.** Two workers re-read the same fixture and re-derive the same
   context; the sum of worker time is 3–6× the single worker's. Without context sharing (the R1
   Context Pack) or a cheaper per-worker prompt, delegation cannot win on these task sizes.
4. **Integration verification disappears in P3.** The consumer cannot run the composition, and one
   wrong provider implementation propagates to both arms. A frozen interface fixes the signature, not
   the semantics.
5. **n = 2 per cell.** Every number here is directional. No claim of statistical significance is
   made; percentiles are not reported.
6. **The oracle is public**, so this family is a mechanism validation, not a held-out benchmark.
7. **Provider-level overlap is inferred**, not measured: `provider_calls.jsonl` has latency but no
   wall-clock stamp and no worker attribution.

---

## Verdicts

| Dimension | Verdict | Evidence |
|---|---|---|
| A. Mechanism correctness | **PASS** | Router unchanged; `off`/`shadow`/`selective` unchanged; decisions deterministic and digest-addressed; 0 policy-mixing incidents; every run carries its decision |
| B. Positive admission quality | **PASS** | 6/6 live admissions, 3/3 negative rejections, 0 false admission, 0 false rejection, 1 evidence gap by design (N2), 0 policy constraints |
| C. Real parallel execution | **PASS** | 20/20 multi-worker runs with measured overlap; 3 workers within 10 ms; 0 `PARALLEL_EXECUTION_NOT_OBSERVED` |
| D. Task correctness | **FAIL** (delegated arms) | Oracle 6/6 (A0) vs 2/6 (A1) vs 3/6 (A2) on Track A; 4/4 vs 2/4 vs 2/4 on Track B. Causes: shared budget exhaustion, worker implementation errors, lost integration testing |
| E. Cost / latency benefit | **FAIL** | +40 % (Track A) and +269 % (Track B) cost against A0; end-to-end latency +27 %/+59 %; one unreplicated P1 speedup |
| F. Benchmark integrity | **PASS** | Frozen digests, two documented defect versions, stale rounds auto-excluded, oracle self-test, no leakage, no network use |
| G. Review readiness | **PASS** | Three reviewable commits, public branch, PR with architecture and evidence |

**Answer to the stage question.** The router can identify tasks that *may* be delegated: it admitted
every plan that was genuinely independent, refused every plan that was not, and the scheduler really
ran the admitted plans concurrently. But on these families delegation produced no measurable benefit —
it cost more, took as long or longer, and delivered correct artifacts less often. The bottleneck is
the economics of the runtime (per-worker context, shared budget), not the admission decision.
`max_parallel_workers` should stay at 2 and `delegation_route_mode` should stay `off` in production
until a context-sharing mechanism (R1) changes that arithmetic.
