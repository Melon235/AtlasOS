# Stage R0 — Evidence-Based Selective Delegation Router

Round: `r0-20261111T072827Z` · Policy: `r0.1.0` · Router module: `src/atlasos/workers/routing.py`

Question answered: **is this candidate task plan worth delegating to more than one worker?** Not
"how do we produce a better plan", not "how do we give workers more context", not "how do we retry
failures" — those are R1/R2/R3.

Short answer: the router is deterministic, explainable, switchable and it never touches the existing
execution paths. On the Stage E1 family it refuses **all six** candidate A1 plans for auditable
reasons, and running the refusal (A2) reproduces the single-worker outcome — 6/6 oracle for
$0.0139 instead of 2/6 for $0.0456. That is a routing-quality result, not a model-capability result,
and it comes with a large caveat: E1 contains no plan the router admits, so generalization is only
demonstrated offline (see §15).

---

## 1. AtlasRuntime delegation call chain (real code)

```
Public goal (BenchmarkTask.prompt / StageEDefinition.overall_prompt)
  ↓  experiments/evaluation/stage_e_tasks.py: variants() → A0 (1 WorkerStep) / A1 (N WorkerSteps + depends_on)
  ↓  experiments/evaluation/stage_r0_routing.py: build_proposal()  ← NEW: public text → evidence
  ↓  src/atlasos/workers/routing.py: route_delegation()            ← NEW: pure decision
  ↓  experiments/evaluation/runner.py: a0_steps() | task.workers    ← NEW: selective switch
  ↓  ScopedWorkerFactory.create(WorkerAssignment(profile_id="coding_worker", task=env_note+step.prompt))
  ↓  AgentConfig(system_prompt=render_worker_context(profile, task))  [M2, unchanged]
  ↓  TaskSpec(title, description=step.prompt, assignee, depends_on=[titles])  [AnyCode, unchanged]
  ↓  AtlasContextEngine.run_tasks → AnyCode DAG / wave scheduler / M3/M4/M5/M6/M7 / G1 / G2a
```

Audit answers:

| Question | Finding |
|---|---|
| Who builds `WorkerAssignment`? | Only `experiments/evaluation/runner.py:726` (and test helpers). Production `src/` has no caller. |
| How does a main agent express intent? | It does not today: AtlasOS never calls AnyCode's dynamic planner (`run_team`). Only explicit `run_tasks` specs exist. |
| Where is the profile chosen? | `ScopedWorkerFactory.create` resolves `assignment.profile_id` against the host-owned `WorkerCatalog`; an unknown id raises. The evaluation runner hardcodes `coding_worker` for every step. |
| Is there a dynamic planner? | In AnyCode (`orchestrator.run_team` → coordinator agent), **unused by AtlasOS**. Its parser (`_parse_task_specs`, orchestrator.py:904-926) only understands `ASSIGN: agent \| title \| description` and forces `depends_on = [previous_title]` — i.e. the dynamic path can only express a **linear chain**, never parallelism. |
| Plans' output schema | `ASSIGN: <agent-name> \| <title> \| <description…>`; unknown assignees are dropped silently. |
| Is there a pure boundary before worker creation? | Yes: between the candidate `BenchmarkTask.workers` and `ScopedWorkerFactory.create`. The router sits there and is pure. |
| Entrances that bypass it? | `run_tasks` with hand-written `TaskSpec`s (the evaluation harness); `run_team`'s coordinator plan (unused). Both are recorded in §14. |
| A0/A1 construction | `variants(definition)` builds both from one definition: same visible files, same hidden oracle, same `required_artifacts`; A0 is one `solo` step holding `overall_prompt`, A1 is `definition.workers`. |
| `depends_on` → AnyCode | `runner.py` maps step ids to titles and passes `depends_on=[title_by_id[x]]`; AnyCode builds its own DAG. |
| Waves | A1 plans have 1–2 waves (`e1-05/06/07/11` are pure chains). |
| Where can the route be applied? | Only two existing paths exist: A0 (single `solo` step) and A1 (`definition.workers`). R0 chooses between them; it never edits either. |

Evidence actually available (no invented fields):

| Field | Source | Trust |
|---|---|---|
| `overall_prompt`, `worker.prompt` | public task text | PUBLIC_TASK |
| `depends_on` | public DAG metadata | TRUSTED_METADATA |
| `required_artifacts`, `visible_files`, `allowed_paths` | public benchmark definition | TRUSTED_METADATA |
| write paths per step | **derived** by a deterministic text extractor (§3) | STATIC_ANALYSIS |
| interface coupling, risk class | **derived** (sibling names in text, verb analysis) | STATIC_ANALYSIS / PLANNER_CLAIM |
| `declared_write_paths`, `interface_contract`, `expected_outputs` | **do not exist** as fields anywhere in the repo | UNKNOWN |

---

## 2. What was learned from Oh-My-OpenAgent (`dev` branch)

| # | Question | Finding |
|---|---|---|
| 1 | `AgentPromptMetadata` | `{category: exploration\|specialist\|advisor\|utility, cost: FREE\|CHEAP\|EXPENSIVE, triggers[{domain,trigger}], useWhen[], avoidWhen[], dedicatedSection?, promptAlias?, keyTrigger?}` — it is *prompt-generation* metadata. |
| 2 | How is the Delegation Table built? | Dynamically from each agent's `triggers` (domain+trigger rows), plus `{CATEGORY_SECTION}` / `{DECISION_MATRIX}` placeholders injected upstream; adding an agent updates the table without editing the prompt. |
| 3 | Hephaestus: direct vs delegate | Prompt heuristic: "default bias DELEGATE for complex; work yourself only when TRIVIAL"; quantitative thresholds (<10 lines / single file → self; multi-file / >100 lines → must delegate); intent gate (trivial / explicit / exploratory / open-ended / ambiguous). |
| 4 | `task()` → execution config | `resolveCategoryExecution(args, ctx, …)` → `{agentToUse, categoryModel, categoryPromptAppend, maxPromptTokens, modelInfo, actualModel, fallbackChain, isUnstableAgent, error}`; category aliasing, model availability and fallback chain resolution are real runtime work. |
| 5 | Prompt heuristics | Whether to delegate, which category fits, "parallel fan-out is the default", the 6-section delegation prompt, "never trust subagent self-reports". |
| 6 | Real runtime validation | Category existence/aliasing, model availability + `provider/model` format, fallback chain, and an `error` field instead of exceptions. Nothing validates whether delegation was *justified*. |
| 7 | Atlas parallel criteria and interface coupling | "Sequential ONLY if a NAMED dependency: input dependency (file, value, **schema**) or same-file conflict; anything else → parallel." There is **no interface-coupling concept**: `schema` is folded into input dependency and the planner is trusted to enumerate it. Implicit API coupling (consumer needs producer's exact signature) is not considered. |
| 8 | What to reuse / not port | Reuse: capability metadata as data, decision/execution separation, direct execution as a first-class route, mandatory context in delegation, verify-don't-trust. Not ported: parallel-by-default bias, background manager, OpenCode session layer, category model fallback, dedicated reviewer, unbounded repair loops, line-count thresholds (uncalibrated). |

**Where R0 deliberately diverges:** OmO's default is "delegate unless trivial". R0's default is
"direct unless the evidence proves the plan is independent, coupled-free, context-complete and
parallelisable". G3-4a measured that the opposite bias costs ~3.4× more here.

---

## 3. Router input / output schema

```python
DelegationProposal(proposal_id, overall_goal, candidate_tasks, evidence)
CandidateTask(task_id, description, depends_on, proposed_profile_id)  # no trusted claims allowed
EvidenceItem(
    evidence_id, kind, subject_task_ids, value, source_kind, source_locator, status
)  # PROVEN|DISPROVEN|UNKNOWN
RouteDecision(
    route,
    approved_task_ids,
    reason_codes,
    evidence_refs,
    policy_version,
    gates[7],
    blocked_gates,
    diagnostics,
    proposal_id,
    policy_digest,
    max_parallel_wave_width,
)
RouteKind = DIRECT | DELEGATE_ONE | DELEGATE_PARALLEL
```

`CandidateTask` deliberately has **no** `independent`, `safe` or `low_risk` field: those are exactly
what the router must decide, and a planner must not be able to assert them.

`RouteDecision` is JSON-serialisable (`decision_to_dict`), has a stable `decision_digest`, and holds
no runtime objects, no floats, no free-text reasoning.

---

## 4. Evidence trust model

| source_kind | Meaning | Can it unlock delegation? |
|---|---|---|
| `PUBLIC_TASK` | the task text itself | yes |
| `TRUSTED_METADATA` | benchmark definition / DAG edges | yes |
| `STATIC_ANALYSIS` | deterministic derivation already performed (path extraction, verb analysis) | yes, for the derived fact only |
| `PLANNER_CLAIM` | an unverified assertion | **never** |

Three-valued status: `PROVEN` / `DISPROVEN` / `UNKNOWN`. `DISPROVEN` items are ignored, `UNKNOWN`
never counts as "safe". Concretely: "planned write set = {a.py}" is `PROVEN`; "no other file will be
touched at runtime" stays `UNKNOWN` and is *not* promoted. A planner that says "these are
independent" contributes nothing (unit test `test_planner_claim_cannot_unlock_parallel`).

---

## 5. Gate semantics

| Gate | Checks | Blocks with |
|---|---|---|
| 0 Proposal validity | empty ids, duplicate ids, dangling `depends_on`, cycles, unknown profile, goal coverage | `INVALID_DAG`, `DUPLICATE_TASK_ID`, `MISSING_DEPENDENCY`, `CYCLIC_PLAN`, `UNKNOWN_PROFILE`, `UNCOVERED_GOAL` |
| 1 Necessity | <2 tasks → direct (unless a fully specified single read-only task → `DELEGATE_ONE`) | `TRIVIAL_DIRECT_TASK` |
| 2 Dependency & coupling | data dependencies, shared write paths (concurrent vs serialised), unfrozen interface symbols, unknown semantic coupling | `SHARED_WRITE_RISK`, `UNSTABLE_INTERFACE`, `SEMANTIC_COUPLING_UNKNOWN` |
| 3 Parallel opportunity | real ready-wave width: `<2` → no opportunity, `>max_parallel_workers` → limit | `NO_PARALLEL_WAVE`, `PARALLEL_LIMIT_EXCEEDED` |
| 4 Side-effect risk | per-task `READ_ONLY` / `LOCAL_REVERSIBLE_WRITE` / `SHARED_MUTATION` / `EXTERNAL_SIDE_EFFECT` / `UNKNOWN` | `EXTERNAL_SIDE_EFFECT`, `UNKNOWN_RISK`, `SHARED_WRITE_RISK` |
| 5 Context sufficiency | goal + deliverable + declared upstream per task | `CONTEXT_INSUFFICIENT` |
| 6 Admission | emits `DELEGATION_ADMITTED` (+ `READ_ONLY_INDEPENDENT_WORK` or `INDEPENDENT_DELIVERABLES`) | — |

Two semantics worth calling out:

* **Shared write is judged by wave placement.** Two owners of one path in the *same* wave is a real
  race and stays blocked even with a merge protocol (`test_concurrent_shared_write_is_blocked_…`);
  the same two paths serialised by `depends_on`, with a trusted merge protocol, become
  `CONTROLLED_MERGE_PROTOCOL` — legal, but with no parallel wave left (`test_serialised_…`).
* **`UNKNOWN` is recorded, not armed.** `UNKNOWN_RISK`, `SEMANTIC_COUPLING_UNKNOWN` and
  `INSUFFICIENT_EVIDENCE` distinguish "dangerous" from "planner did not tell us". Only the former
  is a property of the task.

Router refuses to trim a plan: it approves all tasks or none (`route` + `approved_task_ids`). R0
performs no replanning.

---

## 6. DAG and parallel-opportunity analysis

`ready_waves()` levels the declared edges; `max_wave_width()` is the only parallel metric used.
Worker count is never used as a proxy.

| Plan shape | Tasks | Width | Verdict shape |
|---|---|---|---|
| `A` | 1 | 1 | DIRECT (`TRIVIAL_DIRECT_TASK`) |
| `A→B→C` | 3 | 1 | DIRECT (`NO_PARALLEL_WAVE`) |
| `A‖B` independent, read-only | 2 | 2 | DELEGATE_PARALLEL |
| `A→B`, B consumes A's API, no frozen contract | 2 | 1/2 | DIRECT (`UNSTABLE_INTERFACE`) |
| `A‖B`, both write `state.json` | 2 | 2 | DIRECT (`SHARED_WRITE_RISK`) |
| `A,B → merge` with trusted merge protocol and disjoint writes | 3 | 2 | admitted only if interfaces are frozen |

---

## 7. Capability metadata

`capabilities_from_catalog(WorkerCatalog())` derives routing metadata **from the trusted catalog
itself** (no second source of truth):

| profile | domains (from `operating_scope`) | preferred work types | avoid | cost tier |
|---|---|---|---|---|
| `research_worker` | research, diagnostics | read_only_analysis, diagnosis, code_review | module_implementation | cheap |
| `coding_worker` | coding | module_implementation, module_edit, verification | — | standard |
| `data_worker` | data | read_only_analysis, diagnosis, code_review | module_implementation | cheap |
| `general_worker` | general | read_only_analysis, diagnosis, code_review | module_implementation | cheap |

Derivation is tool-list based (`file_write`/`file_edit`/`bash` ⇒ write-capable). It is a *routing*
fact, not an authorization: M3 still authorises every action. A planner cannot add a profile, a
tool, a scope or `max_turns`; an unknown `proposed_profile_id` is rejected at Gate 0 instead of being
silently replaced.

---

## 8. Minimal code change

New:

* `src/atlasos/workers/routing.py` — types, evidence resolution, waves, 7 gates, decision rendering
  (~600 lines, no I/O, no imports from `experiments`).
* `experiments/evaluation/stage_r0_routing.py` — proposal builder (public text → evidence), frozen
  policy, `plan_route()` / `a0_steps()`.
* `experiments/evaluation/stage_r0_analysis.py` — `audit` (offline shadow) and `runs` (round report).
* `tests/unit/test_delegation_routing.py` (34 tests), `tests/integration/test_stage_r0_routing.py`
  (4 tests), `docs/stage-r0-routing-report.md`, `experiments/results/stage-r0/`.

Modified:

* `experiments/evaluation/runner.py` — `delegation_route_mode`; routing block before worker
  creation; `metrics.routing`; two latent `UnboundLocalError`s in the metrics path made defensive
  (`task_texts`, `contract_overhead_chars`) — they only surfaced when a run failed early.
* `experiments/evaluation/stage_e_cli.py` — `--delegation-route-mode`.
* `experiments/evaluation/stage_e_rounds.py` — `r0` suite (A0/A1/A2).
* `.gitignore` — `experiments/results/stage-r0/`.

Not touched: AnyCode, scheduler, TaskQueue, M2 factory semantics, WorkerProfile tools, M3–M7, G1,
G2a, G3-1…G3-4a, fixtures, hidden oracles, worker prompts.

**Why `mode="coding"` on a routed DIRECT run**: the executed plan is the A0 plan, so the task must
be graded like an A0 sample (single-worker checks). `BenchmarkTask` also rejects `multi_worker` with
one worker. The routing decision is still recorded with the full 3-task candidate DAG.

---

## 9. Unit and integration tests

Unit (34): determinism, repeated-call purity, JSON stability, gate-by-gate behaviour (single task,
chain, independent read-only, disjoint writes, shared writes concurrent vs serialised, frozen vs
unfrozen interface, context/deliverable gaps, cycle, dangling dependency, duplicate ids, unknown
profile, missing coverage, planner claim, unknown risk, disproven evidence, parallel limit,
`DELEGATE_ONE`), capability/catalog alignment, no mutation of proposal/tasks, text extraction.

Integration (4, real `AtlasContextEngine` + scripted provider): a DIRECT decision runs **one** worker
whose opening message carries the full public goal; a PARALLEL decision keeps the original two
prompts and DAG titles; shadow mode runs the two-worker plan while the decision says DIRECT; building
a proposal leaves the executed plan byte-identical.

```text
.\.venv\Scripts\python.exe -m pytest   → 635 passed, 1 skipped, 1 deselected in 209.34s
ruff check .                            → All checks passed!
ruff format --check .                   → 340 files already formatted
pyright                                 → 0 errors, 0 warnings, 0 informations
```

---

## 10. Stage E1 shadow audit (offline, no model, no sandbox)

`python -m experiments.evaluation.stage_r0_analysis audit` → `experiments/results/stage-r0/audit/`.

| sample | tasks | width | route | reasons |
|---|---|---|---|---|
| e1-01-config | 3 | 3 | direct | PARALLEL_LIMIT_EXCEEDED |
| e1-02-report | 3 | 3 | direct | UNSTABLE_INTERFACE |
| e1-03-rbac | 3 | 3 | direct | PARALLEL_LIMIT_EXCEEDED |
| e1-04-cli | 3 | 3 | direct | UNSTABLE_INTERFACE |
| e1-05-pairs | 2 | 1 | direct | NO_PARALLEL_WAVE |
| e1-06-kv | 2 | 1 | direct | NO_PARALLEL_WAVE |
| e1-07-export | 2 | 1 | direct | UNSTABLE_INTERFACE |
| e1-08-stats | 3 | 2 | direct | UNSTABLE_INTERFACE |
| e1-09-etl | 3 | 2 | direct | UNSTABLE_INTERFACE |
| e1-10-search | 3 | 2 | direct | UNSTABLE_INTERFACE |
| e1-11-fallback | 2 | 1 | direct | UNSTABLE_INTERFACE |
| e1-12-conflict | 3 | 2 | direct | SHARED_WRITE_RISK |

Distribution: `direct` 12/12. Reasons: `UNSTABLE_INTERFACE` 7, `PARALLEL_LIMIT_EXCEEDED` 2,
`NO_PARALLEL_WAVE` 2, `SHARED_WRITE_RISK` 1.

**Degeneracy analysis (required).** All-DIRECT is not "success", so it was decomposed:

1. *Is the family unsuitable?* Partly yes. Seven plans have a consumer that names a sibling module
   ("using parser and stats", "using format_row", "Read … then implement …") with no frozen interface
   anywhere in the repo — an interface dependency the DAG cannot express.
2. *Is policy too conservative?* Partly yes. Two plans (`e1-01-config`, `e1-03-rbac`) are genuinely
   independent, disjoint-write, context-complete, and blocked **only** by
   `max_parallel_workers=2` versus a width of 3. The offline sensitivity run at width 3 admits
   exactly those two: `{delegate_parallel: 2, direct: 10}`. That is the concrete cost of the frozen
   threshold, and it is a policy decision, not a router bug.
3. *Is evidence missing?* Partly yes. `interface_contract` cannot be proven for any E1 task because
   no field or artifact declares one; the router therefore reports `UNSTABLE_INTERFACE`, not
   "unsafe interface". That is a **planner-information gap**, and it is exactly what R1 (context
   pack / declared contracts) could close.

No rule references a sample id, no oracle input is used, and the extractor is a documented text
function (unit-tested against the four real prompt shapes).

---

## 11. Real A0 / A1 / A2 comparison (Phase A)

18 runs: 3 tasks × {A0 forced single, A1 forced DAG, A2 selective} × 2 repetitions, alternating arms.
Pinned: `handoff=dual`, `budget=guarded`, `tool_error=corrected`, `compaction=off`,
`tool_result_mode=off`, `worker_contract_mode=legacy`, G3-4a isolation, DeepSeek chat, temperature 0,
OpenSandbox, 120 k tokens, 14 turns, 60 tool calls.

| Metric | A0 | A1 | A2 (selective) |
|---|---|---|---|
| Oracle pass | 5/6 | 2/6 | **6/6** |
| `TASK_SUCCESS` | 5/6 | 2/6 | **6/6** |
| Worker success | 6/6 | 8/16 | 6/6 |
| Prompt tokens | 107,792 | 456,683 | 107,182 |
| Completion tokens | 8,486 | 21,510 | 8,875 |
| Cache hit ratio | 0.9215 | 0.8734 | 0.9184 |
| Cost (USD) | 0.013318 | 0.045556 | 0.013866 |
| Avg cost / run | 0.002220 | 0.007593 | 0.002311 |
| Model turns | 39 | 122 | 39 |
| Tool calls | 45 | 141 | 45 |
| Budget exceeded | 0 | 3 | 0 |
| Probe turns | 4 | 29 | 4 |
| Routes | — | — | `direct` 6/6 |

Per task:

| Task | A0 | A1 | A2 | A2 decision |
|---|---|---|---|---|
| e1-02-report | 1/2 ($0.0062) | 0/2, 2× budget ($0.0219) | 2/2 ($0.0062) | `UNSTABLE_INTERFACE` → A0 |
| e1-07-export | 2/2 ($0.0050) | 0/2 ($0.0168) | 2/2 ($0.0053) | `UNSTABLE_INTERFACE` → A0 |
| e1-12-conflict | 2/2 ($0.0022) | 2/2 ($0.0069) | 2/2 ($0.0023) | `SHARED_WRITE_RISK` → A0 |

Attribution, per the required separation:

* **Gain from avoiding a bad delegation** — A2 vs A1: +4 oracle passes, −348 k prompt tokens (−76 %),
  −$0.0317 (−70 %), −83 model turns, −3 budget exhaustions, −25 probe turns. A2 equals A0 in
  execution, so this is a *delegation-avoidance* gain, not a model gain.
* **Gain from real parallelism** — none this round: 0 of 6 A2 runs were admitted, because 0 of 12
  E1 plans pass the gates. The parallel path is exercised only by the integration test.
* **A2 vs A0 (+1 oracle pass)** is **noise**: same plan, same turns, same cost; A0 lost
  `e1-02-report` rep 0 to a hidden-test failure while A2 passed it. Reported so it is not misread as
  a routing advantage.

---

## 12. Over-rejection and mis-approval cases

Over-rejected (defensible but costly):

* `e1-01-config`, `e1-03-rbac` — three genuinely independent modules. Blocked only by
  `max_parallel_workers=2`. At width 3 the router admits them; the frozen policy does not.
* `e1-02-report` / `e1-04-cli` — blocked by `UNSTABLE_INTERFACE` for a *prose* reference ("using
  parser and stats"). A declared, frozen interface would flip them.

Correctly rejected, with evidence:

* `e1-12-conflict` — three tasks, two of them concurrent writers of `state.json`; the merge worker
  depends on both (`DATA_DEPENDENCY` + `SHARED_WRITE_RISK`). A1 still "succeeds" on the oracle but at
  3.1× the cost and with 3 workers doing redundant work.
* `e1-05/06/07/11` — chain-shaped; the scheduler serialises them anyway, so the extra worker buys
  nothing (`NO_PARALLEL_WAVE` / `UNSTABLE_INTERFACE`).

Mis-approvals: **none observed** (no plan reached `DELEGATE_PARALLEL`).

---

## 13. Oracle / cost / latency attribution

* Oracle: A1's 4 failures are `hidden_tests_passed` (e1-02, e1-07) and `all_workers_produced_output`
  (e1-07 rep 1); 3 of the 6 A1 runs were also budget-exhausted. None of this is fixed by routing — it
  is *avoided* by not delegating.
* Cost: A1 spends 4.2× A0's prompt tokens for 1/3 of the oracle passes. The router's refusal lifts
  the average run cost from \$0.00759 to \$0.00231 while raising oracle passes from 2/6 to 6/6.
* Latency: not used as a decision input. Wall-clock per run is recorded in `result.json`
  (`latency_ms`); parallelism never ran, so no scheduling-speedup figure exists (UNAVAILABLE).
* Cache: A2/A0 both ~0.92; A1 pays 5 pp lower hit ratio for its longer, more divergent conversations.

---

## 14. Known limitations

1. **No live parallel run.** Every A2 decision was DIRECT, so `DELEGATE_PARALLEL` is proven only by
   the integration test and the offline audit. Phase B (72 runs) and Phase C (a new positive-control
   task family) were **not executed** — this round's budget covered Phase A only, and the report does
   not claim otherwise.
2. **n = 2 per cell.** Directional only.
3. **Evidence is text-derived.** Write paths, risk class and interface references come from a
   documented extractor over public prose, not from a code graph. A misleading task description
   produces misleading evidence; the router records it as `STATIC_ANALYSIS`, never as truth about
   runtime behaviour.
4. **`interface_contract` can never be PROVEN today** — nothing in the repo declares one. The
   router's `UNSTABLE_INTERFACE` therefore means "not declared", which is why 7/12 plans are refused
   on that basis.
5. **`max_parallel_workers=2` is uncalibrated.** It is the binding constraint for 2 of 12 plans.
   Changing it requires a new policy version and a new round (a rule, not a tweak).
6. **Weaker check set on the routed arm.** A routed DIRECT run is graded in `coding` mode, so the
   `distinct_workers` / `all_workers_produced_output` checks do not apply (they cannot: one worker).
   Artifact, forbidden-path and hidden-test checks are unchanged.
7. **Production path untouched.** `run_team`'s dynamic planner is still unused and still
   chain-only; R2 is where an online gate belongs.
8. **`variant` label.** A2 samples end in `-a2`, and the runner's variant heuristic labels them
   `A1`; the authoritative arm is in `manifest.json` / `metrics.routing.mode`.

---

## 15. Verdicts

**A. Mechanism correctness — PASS.**
Pure, deterministic, no I/O, no clock, no provider, JSON-serialisable, one frozen policy
(`r0.1.0` + digest). Off mode leaves the candidate plan untouched (integration test), shadow records
without changing execution, selective swaps only the worker set. All 18 runs completed with the
recorded decision attached; no test was weakened.

**B. Routing quality — PASS (for rejection), INCONCLUSIVE (for admission).**
Six of six A1 plans were refused for specific, evidence-backed reasons, and the refusal produced the
single-worker outcome at single-worker cost. Discrimination is demonstrated: the same router admits
independent read-only and frozen-interface plans in unit tests, and admits 2 of the 12 E1 plans once
the width limit is raised. But **no live admission happened**, so "can it preserve a genuinely
parallel task" is answered only offline.

**C. Task quality — PASS (by avoidance, not by capability).**
Oracle 6/6 vs 2/6 against A1; 5/6 vs 2/6 against nothing — i.e. equal to A0 within noise. The
improvement comes from not running the failing decomposition. No claim of better worker output.

**D. Efficiency — PASS.**
Per-run cost \$0.00231 vs \$0.00759 (−70 %), prompt tokens −76 %, turns −68 %, budget exhaustions
3 → 0, probe turns 29 → 4, versus A1. Against A0 the numbers are identical (+4 % cost, −0.6 %
prompt) as expected for the same execution.

**E. Generalization — INCONCLUSIVE.**
The router is not the degenerate `if len(tasks) > 1: parallel` function (it refuses all of them), nor
a constant DIRECT function (it admits 2/12 at width 3 and 2 synthetic plans in tests). But without
Phase B/C there is no *live* evidence that it preserves a parallel benefit on tasks it has not seen.
The honest next step is a small, pre-registered positive-control family (two independent read-only
analyses, one frozen-interface pair) run as A1 vs A2 only — that is R1/R2 work, not R0.

**Recommendation:** keep `delegation_route_mode=off` as the production default; ship
`shadow` for online observation when a real planner exists (R2); do not raise
`max_parallel_workers` on this evidence alone.

---

## Files

Added: `src/atlasos/workers/routing.py`, `experiments/evaluation/stage_r0_routing.py`,
`experiments/evaluation/stage_r0_analysis.py`, `tests/unit/test_delegation_routing.py`,
`tests/integration/test_stage_r0_routing.py`, `experiments/results/stage-r0/`,
`docs/stage-r0-routing-report.md`.

Modified: `experiments/evaluation/runner.py`, `experiments/evaluation/stage_e_cli.py`,
`experiments/evaluation/stage_e_rounds.py`, `.gitignore`.

Read-only `git diff --stat` (the working tree also carries the uncommitted G1–G3-4a work; no Git
write operation was performed):

```text
 .gitignore                                 |   8 +
 experiments/evaluation/runner.py           | 291 ++++++++-
 experiments/evaluation/stage_e_cli.py      | 129 ++++
 experiments/evaluation/stage_e_rounds.py   | 911 ++++++++++++++++++++++++++++-
 src/atlasos/integrations/anycode_engine.py |  93 +-
 src/atlasos/sandbox/tools.py               |  49 +-
 src/atlasos/tools/conversation.py          |  33 +
 src/atlasos/tools/dispatch.py              |  44 +-
 src/atlasos/tools/runtime.py               |   8 +-
 tests/integration/test_anycode_dag.py      | 705 +++++++++++++++++++++-
 10 files changed, 2250 insertions(+), 21 deletions(-)
```
