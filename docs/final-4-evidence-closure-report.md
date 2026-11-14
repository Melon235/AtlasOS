# Final-4 — Evidence Closure and Placement Review

Date: 2026-11-14. Branch `chore/final-1-5-repository-baseline`.
Runtime `src/` byte-identical to `93cbd98f9407e7358e997878238520dcac15827a` (verified with
`git diff --stat 93cbd98..HEAD -- src` → empty).

| Reference | Value |
|---|---|
| `DESIGN_CONTRACT_SHA` | `37eba87df2aa188d16d24d0c31bd00412d154276` (unchanged this round) |
| `CORRECTED_EVAL_SHA` (rev 2) | `2a2af55ad08c74723df3fa56c4b8c80acbc57a58` |
| `CORRECTED_EVAL_SHA` (rev 3, this round) | `97a7d35b3700d50b209141c4d0323cf2173c2a71` |
| request-evidence commit | `410446c8dae5b8a807c942399f71c1ec8d553672` |
| Data | `experiments/results/final4/{pilot_results_v2,router_comparison_v2,run_inventory,validation_tasks_v3,pilot_manifest_v3}.json` |

---

## A. The design contract did not change

- `docs/architecture-design-contract.md` is untouched this round; the only references to it are the
  new documents listed in `docs/README.md`.
- Two cores and one extension are unchanged: Context Engineering and Reliable Execution remain the
  cores, Adaptive Delegation remains an extension, and every experiment below is reported as
  *evaluation*, not as a change to the design.
- No new mechanism entered `src/`. The only production change is in the evaluation layer
  (`experiments/evaluation/`), which is explicitly outside the runtime under test.

## B. Router after the metadata correction

`BenchmarkTask.required_artifacts` was `()`, so `GATE_0_PROPOSAL_VALIDITY` could not prove goal
coverage and every candidate was blocked with `UNCOVERED_GOAL`. Revision 3 sets it from the public
requirement text (the file each worker is told to create). Nothing else changed:

| Digest | Revision 2 | Revision 3 |
|---|---|---|
| `candidate_plan_digest` (all three tasks) | `d6f1fd39…`, `2dc6a00b…`, `45066b5c…` | identical |
| `shared_context_digest` | — | identical |
| `oracle_digest`, `solution_digest`, `initial_workspace_digest` | — | identical |
| `task_metadata_digest`, `router_proposal_digest` | absent | new |

**Consumer audit** (every reader of the field):

| Consumer | Effect of the change |
|---|---|
| prompt / worker objective | none — `required_artifacts` never enters the task text |
| candidate DAG, worker assignment, resource limits | none |
| `oracle.required_artifacts_present` | previously vacuous, now a real check |
| `result.json → task` block, router evidence | records the declared deliverables |

The one non-trivial effect is the oracle check. **Correction (2026-11-14, G0 review):** the earlier
wording here — "14/14 would still pass" — was wrong twice and is retracted.

- The count was 22, not 14 (`run_inventory.json` covers every run then on disk, including the three
  legacy ones).
- What was re-evaluated is **one oracle sub-check, `required_artifacts_present`, over the exported
  workspaces — a file-presence test, not the oracle.** `eval-c16d3b21a134` (legacy R1) has an empty
  patch (`sha256 e3b0c442…`) and `hidden_tests_passed: false`, yet `required_artifacts_present: true`,
  because the seed files are present with `NotImplementedError` inside. Its oracle verdict remains
  **FAIL** and is not rewritten.
- `run_inventory.json → declared_artifacts_check` now carries `check_kind: FILE_PRESENCE_ONLY` and
  `can_establish_full_oracle_equivalence: false`.

What can be argued without a re-run is narrower and directional: for a run whose **full oracle already
passed**, adding a presence condition cannot turn it into a failure, because the functional checks
import and execute the deliverables and therefore required the files to exist. The converse is not
implied, and no FAIL became a PASS. Revision-2 results stay comparable on that basis — a logical
argument, not a measurement.

**Live decisions** (unchanged `r0.1.0`, no thresholds touched):

| Task | Decision | Reason codes | Executed topology | Oracle |
|---|---|---|---|---|
| `validation-serial` | `direct` | `NO_PARALLEL_WAVE` | 1 worker | PASS |
| `validation-parallel` | `delegate_parallel` | `DELEGATION_ADMITTED` | **3 workers** | PASS |
| `validation-coupled` | `direct` | `NO_PARALLEL_WAVE` | 1 worker | PASS |

`validation-parallel-R2` (`eval-ffc739e09fa6`) is the first time the selective arm actually executed
the delegate branch: 3 `AtlasTaskPromptBuilder` invocations, 3 workers, oracle PASS, 103 369 input
tokens / 6 857 miss / 0.00934 USD. The delegated run consumes exactly the candidate DAG of
`validation-parallel-R1` — same three step ids, same dependencies, same assignment text — because
`plan_route` returns `RoutedPlan.steps == task.workers` on admission.

Track C is now **validated for one cell** (parallel) and **observed as DIRECT** for the other two,
which is the correct decision for a serial chain and for a coupled pair, not a router defect.

## C. Context X0/X1, two pairs plus replications

All arrays use the provider's hit/miss pair; `prompt_tokens` from `usage` is not used across dates
because its meaning changed (it equalled the miss count on 2026-11-14 and hit+miss afterwards).

| Pair | Arm | Run | Oracle | Hit | Miss | Out | Cost USD | Latency |
|---|---|---|---|---|---|---|---|---|
| 1 (X0 first) | X0 legacy | `eval-324092b7c6e7` | PASS | 36 096 | 7 636 | 2 733 | 0.00579 | 56.2 s |
| 1 | X1 plane | `eval-3826f621c3f5` | PASS | 40 832 | 3 258 | 2 967 | 0.00478 | 58.2 s |
| 2 (X1 first) | X1 plane | `eval-63e864171276` | PASS | 46 464 | 3 286 | 2 853 | 0.00469 | 59.2 s |
| 2 | X0 legacy | `eval-5a4e958c9c9f` | PASS | 40 576 | 2 884 | 2 500 | 0.00411 | 50.8 s |
| X1 rep 1 | X1 plane | `eval-dad0796bd863` | PASS | 46 848 | 3 516 | 2 733 | 0.00462 | 61.2 s |
| X1 rep 2 | X1 plane | `eval-32fbbff36188` | PASS | 43 520 | 3 200 | 2 691 | 0.00445 | 61.4 s |
| X1 rep 3 | X1 plane | `eval-db81850a884c` | PASS | 38 656 | 2 764 | 2 471 | 0.00403 | 57.8 s |

**The 57 % cache-miss reduction in pair 1 did not replicate.** In pair 2 the legacy arm had the lower
miss count (2 884 vs 3 286). Across all X0 samples the miss spread is 2 884 – 7 636 for runs that are
byte-identical in configuration, which is larger than any X0/X1 difference. The honest conclusion:
**provider-side cache state dominates at this sample size; no cache benefit from the Context Plane is
demonstrated.** What *is* demonstrated is delivery correctness, uniformly, in every X1 run: the
dependency block is selected, rendered, and injected exactly once.

**Request-boundary evidence (the twelfth integration item).** `provider_calls.jsonl` records only a
hash of the whole request and no task identity, so it could not be tied to a render — two unrelated
hashes, which the stage forbids. The evaluation-layer provider wrapper now observes the outgoing
message list and records, per call, `sha256(first message)`, `sha256(system prompt)`, character and
message counts, tool count and tool-schema size, and for each dependency block in the store whether its
content appears in that request and how many times. No prompt, transcript or completion is stored, and
`chat`/`stream` remain pass-through. Note: AnyCode *streams*, so the first implementation observed
nothing at all — the empty result from `eval-32fbbff36188` is exactly that defect, and it is why
`eval-db81850a884c` exists.

Result for `eval-db81850a884c`:

```text
REQUEST_CONTENT_VERIFIED
requests observed 12 | linked to a render 12 | unlinked 0
task-output:w1 present in w2 requests at call 6..11, count = 1 each, no duplicate
w1 requests: no dependency probe (it has none)
```

The linkage is a re-computation, not a pair of hashes: `sha256(first message of the request)` equals
the `task_prompt_sha256` of the exact string the builder returned, and the worker is identified from
`sha256(system prompt)`. So the render provably reached the provider, and the dependency content
provably travelled once.

Earlier units carry `REQUEST_CONTENT_NOT_VERIFIED` (they ran before the wrapper existed) and their
per-render `context.json` is absent; only what `run.json` recorded is used for them. No field was
reconstructed.

## D. Native arm (N0): blocked, not attempted

**Status `BASELINE_PARITY_BLOCKED`.** The neutral *tool* half exists and is measured
(`final4/neutral_adapter.py`: `NeutralEngine`, `SandboxToolBackend`, six AnyCode tools routed through
an external backend; `final4/parity_check.py`). The missing half is the **sandbox lifecycle** for the
native arm: creating the workspace container, uploading the fixture, exporting artifacts and destroying
it *without* injecting `ProjectSandboxManager`, which the stage forbids in the baseline arm and which
is the only lifecycle implementation in the repository.

Budget arithmetic is the second reason: 22 of 24 task runs were consumed by the Router fix, the second
X0/X1 pair and the request-evidence demonstration. Wiring N0 needs one implementation plus three live
acceptance runs; spending the last two runs on an unvalidated adapter would have produced either
nothing or a mixed host/container comparison, which is the exact artefact this stage forbids.

`validation-{serial,parallel,coupled}-N0` remain `NOT_RUN` with that reason in
`pilot_results_v2.json`. Two runs were deliberately left unused.

## E. Tool disclosure: measured, degenerate for this workload

| Arm | Run | Tools exposed at start | Schema chars | Model requests | `tool.search` | `tool.dispatch` | Tools called | Hit | Miss | Out | Cost | Oracle |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| progressive (`R0`) | `eval-06d7aa923b1c` | 3 core + search | n/a (pre-dates the field) | 8 | **0** | 0 | 8 | 26 752 | 4 291 | 2 321 | 0.00423 | PASS |
| full exposure (`D0`) | `eval-72a4ced3cc5e` | 6 | 3 718 | 7 | 0 | 0 | 10 | 23 040 | 2 789 | 2 468 | 0.00394 | PASS |

Both arms use the same catalog, action policy and permissions; only disclosure differs (`D0` is `R0`
with `progressive_tools=False`). The remaining difference is timing noise: full exposure opened one
fewer request and cost 0.0003 USD less.

**The contrast is degenerate, and that is the finding.** A three-module refactor needs `file_read`,
`file_write`, `file_edit` and `bash`, all of which are *core* and therefore never hidden; the
progressive arm performed **zero** searches. The measured numbers therefore say nothing about
disclosure, and no benefit or harm is claimed. A meaningful disclosure ablation needs a task that
requires a searchable (non-core) tool, e.g. the 64/128-tool retrieval corpus — out of scope here.
Recorded as `ABLATION_DEGENERATE` rather than as a result.

## F. Placement core integration

`docs/final-4-placement-core-integration-approval.md` — `PLACEMENT_CORE_INTEGRATION_APPROVAL_REQUIRED`.
Summary of the review: a pooled worker in the evaluation layer alone would reuse *identity* only.
AnyCode starts a fresh task transcript per task, so identity sharing does not reuse conversation
history or provider KV state, and `ScopedWorkerFactory.create` binds `task_id` into the
`ExecutionContext`, so two tasks on one worker would mis-attribute actions, ledger rows and ownership
declarations unless that call gains a per-task context. A real placement experiment therefore needs a
small `src/` change, which this stage does not authorise. Two options are laid out for approval:
`A` (identity-only slots, evaluation layer, no performance claim) and `B` (per-task `ExecutionContext`
under a shared worker principal, `src/` change, real measurement).

## G. Architecture coverage update

Two further corrections from the G0 review, both about claims rather than runs:

- **N0 blocker.** The earlier text said "no lifecycle layer exists". The accurate statement is that
  every *capability* exists (`SandboxProvider.create/execute/read_file/write_file/destroy`,
  `export_workspace`, `GitProjectProvisioner`) and what is missing is a ~40-line **composition** that
  calls them in order without using `ProjectSandboxManager`. See
  `docs/final-4-evidence-handoff.md` §B for the call chain.
- **Request evidence.** `REQUEST_CONTENT_VERIFIED` rests on re-computing `sha256` of the observed
  first message and matching it to the builder's `task_prompt_sha256`; it does not prove that the
  provider *accepted* the body, only that the observed request reached the adapter boundary with that
  content. §C of the handoff document lists the exact predicate and its limits.

`docs/final-4-architecture-coverage.md` is updated:

| Mechanism | Before this round | After |
|---|---|---|
| Context plane on coding/evaluation | `IMPLEMENTED_OPT_IN`, executed once | `IMPLEMENTED_AND_ACTIVE` **in evaluation**, 7 live runs, request-level evidence |
| Adaptive delegation | `WIRED_BUT_NOT_TRIGGERED` | `IMPLEMENTED_AND_ACTIVE` for one cell (delegate executed), DIRECT for two |
| Progressive disclosure | `IMPLEMENTED_AND_ACTIVE` | unchanged in code; the ablation is `ABLATION_DEGENERATE` for this workload |
| Context-locality placement | `IMPLEMENTED_BUT_NOT_WIRED` | unchanged; approval document added |
| Native neutral baseline | `NOT_RUN` | `BASELINE_PARITY_BLOCKED` (lifecycle layer missing) |
| Context-aware recovery | workflow-only | unchanged |

## H. Every real run, token, cost and resource

22 task runs, 44 containers (1 shared workspace sandbox + 1 independent oracle sandbox per run), all
destroyed, 0 cleanup failures, 0 host-isolation violations.

| Task runs | Composition |
|---|---|
| 22 of 24 | 3 legacy (v1) + 15 corrected matrix (v2/v3) + 4 replications |

Per-run detail lives in `experiments/results/final4/run_inventory.json`. Totals over all 22:

```text
prompt tokens 880 588 (hit 790 272 + miss 90 316)
completion tokens 53 750
estimated cost 0.09634 USD of the 2.00 cap
containers 44, destroyed 44, oracle cleanup failures 0
```

Cost basis: the per-run `estimated_cost_usd` computed by the existing pricing table in the run's YAML;
no figure is re-derived here.

The nine corrected matrix cells plus X0 and the extra X1 samples:

| Unit | Run | Workers | Oracle | Hit | Miss | Out | Cost |
|---|---|---|---|---|---|---|---|
| serial-X0 | `eval-5a4e958c9c9f` | 2 | PASS | 40 576 | 2 884 | 2 500 | 0.00411 |
| serial-R0 | `eval-988f23e87d5e` | 1 | PASS | 9 856 | 3 315 | 817 | 0.00203 |
| serial-R1 (= X1) | `eval-63e864171276` | 2 | PASS | 46 464 | 3 286 | 2 853 | 0.00469 |
| serial-R2 | `eval-04fea0852a8d` | 1 | PASS | 24 320 | 1 433 | 1 208 | 0.00203 |
| parallel-R0 | `eval-06d7aa923b1c` | 1 | PASS | 26 752 | 4 291 | 2 321 | 0.00423 |
| parallel-R1 | `eval-e005a3b199d0` | 3 | PASS | 62 720 | 12 332 | 3 709 | 0.00853 |
| parallel-R2 | `eval-ffc739e09fa6` | 3 | PASS | 96 512 | 6 857 | 5 586 | 0.00934 |
| parallel-D0 | `eval-72a4ced3cc5e` | 1 | PASS | 23 040 | 2 789 | 2 468 | 0.00394 |
| coupled-R0 | `eval-e56fb933c797` | 1 | PASS | 28 416 | 4 655 | 2 906 | 0.00505 |
| coupled-R1 | `eval-b3d2da57bfec` | 2 | PASS | 43 008 | 8 473 | 3 979 | 0.00757 |
| coupled-R2 | `eval-8b6fead25320` | 1 | PASS | 39 424 | 2 007 | 2 469 | 0.00380 |

## I. Provenance

| Item | Value |
|---|---|
| `DESIGN_CONTRACT_SHA` | `37eba87` (unchanged) |
| `CORRECTED_EVAL_SHA` rev 2 | `2a2af55` |
| `CORRECTED_EVAL_SHA` rev 3 | `97a7d35` (metadata) |
| request-evidence commit | `410446c` |
| Runtime under test | `93cbd98`, `src/` untouched |
| Offline suite | 953 passed, 0 failed, 6 skipped; ruff, format, pyright clean |
| Modified this round | `experiments/evaluation/{runner.py,baseline.py untouched}`, `final4/{runner_entry,validation_tasks,collect_v2,inventory}.py`, `tests/integration/test_final4_context_plane.py`, docs |

## J. Final-5 entry

Not ready, and the blockers are specific:

1. **N0 lifecycle layer.** Implement a neutral sandbox lifecycle (create → upload → exec → export →
   destroy) that does not use `ProjectSandboxManager`, then run 3 N0 units. 2 task runs are already
   reserved for it.
2. **Cache claim.** n≤5 per arm and the provider's cache state dominates. Final-5 needs interleaved
   repetitions (e.g. X0,X1,X0,X1,…) and a fixed provider warm-up rule before any cache statement.
3. **Disclosure.** Needs a workload that requires a non-core tool; otherwise the ablation is vacuous.
4. **Placement.** Awaiting `PLACEMENT_CORE_INTEGRATION_APPROVAL_REQUIRED`; option B needs `src/`.
5. **Holdout.** Still not designed and still forbidden.

Ready now: the Context Plane executes with request-level evidence, the Router has a validated delegate
path, and every corrected unit passes the independent oracle.

---

### What is design, what is wiring, what is measured, what is not

| Claim | Status |
|---|---|
| Context Engineering is a core subsystem | design (contract) |
| It is wired into coding and evaluation behind a switch | wiring, verified in `src` + fail-closed gate |
| It executes live, delivers each dependency once, and reaches the provider | **measured** (7 runs, 1 with request-level proof) |
| It reduces cache misses or cost | **not demonstrated** — pair 1 said yes, pair 2 said no |
| The router can admit a decomposition and actually delegates | **measured once** (parallel, 3 workers, PASS) |
| Delegation is beneficial | **not demonstrated** — n=1 per cell, and it costs 2.2× the single arm |
| Placement reduces logical handoff | measured earlier in isolation (planner unit tests, S6 Table C) |
| Placement improves runtime cost or cache | **not measured, not wired**, and identity sharing would not deliver it |
| Native arm runs in an equivalent container | **blocked**, no lifecycle layer |
| Progressive disclosure saves tokens | **not demonstrated, degenerate** for this workload |
