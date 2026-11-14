# Final-4 — Architecture Contract, Context Restoration, Corrected Pilot

Date: 2026-11-14. Branch `chore/final-1-5-repository-baseline`.
Runtime `src/` unchanged from `93cbd98f9407e7358e997878238520dcac15827a`.

| Reference | Value |
|---|---|
| `DESIGN_CONTRACT_SHA` | `37eba87df2aa188d16d24d0c31bd00412d154276` |
| `CORRECTED_EVAL_SHA` | `2a2af55ad08c74723df3fa56c4b8c80acbc57a58` |
| Data files | `experiments/results/final4/pilot_results_v2.json`, `router_comparison_v2.json`, `validation_tasks_v2.json`, `pilot_manifest_v2.json`, `legacy_pilot_classification.json` |

This report separates three things that were previously mixed: the design contract (what the project
is), the implementation status (what is wired), and the measured results (what actually ran).

---

## A. Architecture contract

- New authoritative document: **`docs/architecture-design-contract.md`**. Path note: the repository
  keeps active architecture documents flat under `docs/`, so the contract lives next to
  `architecture.md` rather than in a new `docs/architecture/` directory.
- It fixes, in this order: project positioning, the two cores (Context Engineering, Reliable
  Execution), Adaptive Delegation as an **extension**, the Router/Placement/Context-Plane separation,
  the AnyCode boundary, the local OpenSandbox boundary, the claim boundary, the change-control rule,
  and the "three layers must never be mixed" rule (contract ≠ status ≠ results).
- `AGENTS.md` gained §2 *Architecture Identity and Design Contract* (high priority, 20 lines) and §10
  *Context Handoff* (the six-item restore order after a session switch or compaction). Later sections
  were renumbered; no rule was removed.
- `README.md` now states the two-core/one-extension hierarchy above the pillar table and points at the
  contract first. `docs/README.md` lists it as layer 1. `docs/architecture.md` and
  `docs/runtime-entrypoints.md` were corrected where they claimed the coding and evaluation paths do
  not compose the Context Plane — they do, behind `context.policy="task_scoped"`.
- Nothing in the contract was trimmed to match the implementation. Mechanisms that no coding path uses
  (placement, dedup) stay in the contract and are marked not wired in the status documents.

## B. Actual architecture coverage

`docs/final-4-architecture-coverage.md` is the full matrix (16 mechanisms × workflow / coding /
evaluation, with per-mechanism evidence and file:line references). Headline:

| Core | Implemented | Active by default somewhere | Actually executed through a provider request |
|---|---|---|---|
| Context Engineering | yes, all three compositions | workflow only | **now yes** — the corrected pilot (this stage) |
| Reliable Execution | yes | coding + evaluation | partially: gate, ledger, sandbox binding, budget ran; suspension never triggered |
| Adaptive Delegation | yes | no (evaluation-only, off by default) | yes, but **DIRECT only** (see §F) |

Six gaps were found and recorded. G-1 (manifest declares a Context Plane that the loaded YAML does not
enable), G-2 (unequal public task information) and G-3 (a worker killed by an unrelated tool-error
classification) are fixed in this stage. G-4 (placement) is analysed and left for approval. G-5
(resume-time layout equivalence is test-proven, not asserted at runtime) and G-6 (`config_digest`
differs between router and non-router units by construction) are recorded, not fixed.

## C. Context correction

**Configuration.** `experiments/configs/final4-context.yaml` — `context.policy=task_scoped`,
`renderer=stable_deterministic`, `transform.name=identity`, `placement.policy=bypass`; provider,
pricing, sandbox and trajectory identical to `eval-benchmark.yaml`.

**Fail-closed.** `RunnerSettings.context_plane_expectation` is enforced twice: from the resolved YAML
**before any I/O**, and from the constructed builder **before the first model request**. A unit that
declares `task_scoped` and resolves `upstream_default` now aborts with
`EVALUATION_CONFIG_ERROR`/`EvaluationEnvironmentError` instead of silently running the baseline.

**Real call chain, verified live (not inferred):**

```text
runner_entry --unit validation-serial-R1
  └─ run_sample(config_path=final4-context.yaml)         resolved policy=task_scoped
      ├─ _context_prompt_builder → AtlasTaskPromptBuilder(store=InMemoryContextStore,
      │        selector=TaskScopedSelector, renderer=StablePromptRenderer(preserve_input_order=True),
      │        transform=IdentityContextTransform)
      ├─ AtlasContextEngine(prompt_builder=builder)      AnyCode is unmodified
      └─ AtlasContextEngine._build_task_prompt
            → AtlasTaskPromptBuilder.__call__
                ├─ _project: completed task result → ContextBlock "task-output:w1" (version 1)
                ├─ TaskScopedSelector.select (boundary = declared direct dependency)
                ├─ IdentityContextTransform
                └─ StablePromptRenderer → user turn
```

**Worker information fairness.** Task set revision 2 splits every task into:

- `shared_context` — the public requirement, the public interface contract, the data constraints and
  the public acceptance (`test_public.py`, "do not edit it", "hidden tests exist"). Every worker of
  the sample receives it verbatim, and the single-worker arm receives the same block.
- `workers[i].assignment` — that worker's objective, owned module and step-level definition of done.
- dependency outputs — never authored; produced by the run and projected by whichever builder the arm
  configures.

`WorkerStep.prompt` deliberately remains the worker-local text, because that is what the router reads
for declared write paths; `shared_context` is prepended to the model-visible turn only. The declared
write sets of all steps are disjoint (asserted in `tests/integration/test_final4_context_plane.py`).

**Digests.** Revision 1 is preserved byte-identically. Revision 2 records, per task,
`candidate_plan_digest`, `shared_context_digest`, and the digests it supersedes. Seed, oracle and
solution digests are unchanged (proven by test), i.e. the task itself did not change — only how much
of it a worker is told and how the reference solution is reached.

**Context evidence.** The runner now persists `run_dir/context.json` with the resolved configuration,
every render (task id, declared dependencies, store block ids, selected block ids and versions,
rendered dependency order, completion order, transform name, layout hash, task-prompt hash, prompt
chars) and, per declared dependency, whether it appeared in the rendered prompt and how many times.
Provider-side hashes are recorded separately; the file states explicitly that the task-prompt hash and
the provider-request hash are different functions and must not be substituted for one another.

**Handoff compatibility.** `handoff_mode="legacy"` in every arm; `task_scoped` + `dual` is refused at
both gates. In the live X1 runs the dependency appeared in the rendered prompt **exactly once**
(`injection_count = 1`, `duplicate_injection = false`) and the trusted worker system prompt was
unchanged (asserted deterministically, observed live).

## D. Context live evaluation (X0 vs X1)

Both arms: `validation-serial`, same candidate DAG (`w1 → w2`), same assignments, same prompts, same
model, tools, budget, sandbox image and oracle. The only difference is which builder constructs the
task prompt.

| Field | X0 (`upstream_default` + legacy handoff) | X1 (`task_scoped` + stable + identity) |
|---|---|---|
| Run id | `eval-324092b7c6e7` | `eval-3826f621c3f5` (replicated: `eval-dad0796bd863`) |
| Context builder | `None` (`handoff_legacy`) | `AtlasTaskPromptBuilder`, 2 invocations |
| Store blocks | — | `["task-output:w1"]`, version 1 |
| Dependency in both prompts | inline, once | selected, rendered, **once**, no duplicate |
| Workers | 2 | 2 |
| Oracle | **PASS** | **PASS** (both runs) |
| Total input tokens | 43 732 | 44 090 |
| Cache hit tokens | 36 096 | 40 832 |
| Cache miss tokens | 7 636 | 3 258 |
| Completion tokens | 2 733 | 2 967 |
| Cost (USD) | 0.00579 | 0.00478 |
| Latency | 56.2 s | 58.2 s |
| Tool calls | 11 | 14 |
| Budget used / limit | 46 465 / 120 000 | 47 057 / 120 000 |
| Sandbox cleanup | destroyed | destroyed |

**What this does and does not say.**

- It says the Context Plane now genuinely participates in a live coding run: the store, the selector,
  the transform and the renderer were all reached, the dependency was projected from a completed task,
  selected as a direct dependency, rendered by the stable renderer and delivered once.
- Cache miss fell 57.3 % (7 636 → 3 258) on this pair, with total input up 0.8 %. This is one pair of
  runs. X0 and X1 differ in the whole prompt-construction path, not in the renderer alone, and a
  single task family cannot separate the mechanism from the workload. No cache-benefit claim is made.
- The replication (`repetition 1`) reproduced oracle PASS, 2 workers, one dependency injection, 50 364
  total input / 3 516 miss / 0.00462 USD. Two runs are not a distribution either.

`CONTEXT_INTEGRATION_PASS` for X1 (all twelve checks in the stage instruction hold except "the
dependency entered the request" being hash-only at provider level: the rendered prompt is the value
returned to AnyCode's seam, and the provider-side hashes are recorded but are not comparable
functions). `TASK_SUCCESS` for both arms.

## E. Placement

`docs/final-4-placement-integration-proposal.md` answers the eight questions with code references.
Short form: the planner is implemented, deterministic and correct, but its only consumer is
`AtlasRuntime._place` (workflow composition). The coding app is single-task, and the evaluation runner
constructs one worker per frozen step, so `placement.policy` in an evaluation YAML is inert. A genuine
placement arm needs a worker **pool**, which changes `worker_id` semantics, `workers.json`,
`consumer_keys` and the ownership manifest — core assignment, out of scope here.

Every Final-4 unit ran with `placement BYPASS` and records it. No P0/P1 live experiment was run.
`PLACEMENT_INTEGRATION_PROPOSAL` is filed for approval.

## F. Corrected pilot

The three 2026-11-14 runs are reclassified as `EXPLORATORY_LEGACY_PILOT`
(`docs/final-4-legacy-pilot-reclassification.md`, machine-readable in
`legacy_pilot_classification.json`) and are **not** reused. Their files are unchanged. A third defect
was found while auditing them: under `tool_error_mode="legacy"` both R1 workers were terminated with
`side_effect_unknown` after a failing **read-only** `git` command, so the multi-worker oracle failure
was not a delegation or context result at all. Every corrected arm therefore runs
`tool_error_mode="corrected"` identically — a measurement-hygiene setting, not a treatment.

Corrected matrix (`task_set_revision 2`, `dataset_version final4-validation-2`):

| Unit | Workers | Oracle | Input | Hit | Miss | Out | Cost USD | Latency |
|---|---|---|---|---|---|---|---|---|
| serial-X0 | 2 | PASS | 43 732 | 36 096 | 7 636 | 2 733 | 0.00579 | 56.2 s |
| serial-R0 | 1 | PASS | 13 171 | 9 856 | 3 315 | 817 | 0.00203 | 34.7 s |
| serial-R1 (= X1) | 2 | PASS | 44 090 | 40 832 | 3 258 | 2 967 | 0.00478 | 58.2 s |
| serial-R2 | 1 (DIRECT) | PASS | 13 230 | 11 776 | 1 454 | 849 | 0.00153 | 31.9 s |
| parallel-R0 | 1 | PASS | 31 043 | 26 752 | 4 291 | 2 321 | 0.00423 | 43.0 s |
| parallel-R1 | 3 | PASS | 75 052 | 62 720 | 12 332 | 3 709 | 0.00853 | 69.0 s |
| parallel-R2 | 1 (DIRECT) | PASS | 35 984 | 33 408 | 2 576 | 2 083 | 0.00347 | 44.0 s |
| coupled-R0 | 1 | PASS | 33 071 | 28 416 | 4 655 | 2 906 | 0.00505 | 47.8 s |
| coupled-R1 | 2 | PASS | 51 481 | 43 008 | 8 473 | 3 979 | 0.00757 | 62.9 s |
| coupled-R2 | 1 (DIRECT) | PASS | 38 874 | 35 968 | 2 906 | 2 401 | 0.00397 | 46.0 s |

`N0` is still `NOT_RUN` (the native arm runs on the host; the neutral sandbox adapter is not wired).

**Honest reading.**

1. **Multi-worker did not degrade quality on this task set.** All 10 corrected units passed the oracle,
   including the three multi-worker cells and the 3-worker parallel DAG. This contradicts the older
   "multi loses" pattern — but the older arms had one-line worker prompts and a worker-killing tool
   error mode. The correct statement is narrower: *with fair public information and a tool-error mode
   that does not terminate a worker on a failed read, decomposition passed on these three tasks.* It is
   not evidence that decomposition is generally safe, and three tasks cannot establish that.
2. **Multi is still more expensive.** Input tokens 1.6×–3.3× the single arm, cost 1.5×–2.0×.
3. **The router took DIRECT in all three cells.** That is a harness defect, not a router failure — see
   below.
4. **No context-benefit claim.** Only the X0/X1 pair isolates the context-construction path, and it is
   n=1 (plus one replication).

**Why the router is always DIRECT.** Every R2 run recorded `worker_count = 1`, i.e. the collapse
branch. A deterministic replay of the unchanged frozen policy (`router_comparison_v2.json →
router_replay`) shows the reason: `GATE_0_PROPOSAL_VALIDITY` → `UNCOVERED_GOAL`, because
`build_task` declares `required_artifacts = ()`, so goal coverage can never be proven and the router
never reaches gate 1.

Sensitivity probe (deterministic replay, **not** a live arm, no arm was changed after the fact): if
`required_artifacts` were declared as the task's `.py` deliverables, the same frozen policy returns
`DIRECT (NO_PARALLEL_WAVE)` for `validation-serial`, `DELEGATE_PARALLEL` (3 workers) for
`validation-parallel`, and `DIRECT (NO_PARALLEL_WAVE)` for `validation-coupled`. That is the behaviour
Track C is supposed to measure, and it was never exercised. Track C is therefore **NOT_VALIDATED** in
this round; the R2 cells are reported as `DIRECT` with the recorded reason.

## G. Provenance and resources

| Item | Value |
|---|---|
| Frozen runtime SHA | `93cbd98f9407e7358e997878238520dcac15827a` (`src/` byte-identical at HEAD) |
| `DESIGN_CONTRACT_SHA` | `37eba87df2aa188d16d24d0c31bd00412d154276` |
| `CORRECTED_EVAL_SHA` | `2a2af55ad08c74723df3fa56c4b8c80acbc57a58` |
| Model | `deepseek-chat` (resolves to `deepseek-flash`), temperature 0.0 |
| Sandbox | local OpenSandbox, `localhost:8080`, image `atlas-uat-python:3.12`, `network=none` |
| Task runs consumed | **14 of 24** (3 legacy + 10 primary + 1 replication) |
| Cost consumed | **≈ 0.05996 USD of 2.00** (legacy 0.00838 + primary 0.04696 + replication 0.00462) |
| Sandbox cleanup | confirmed destroyed for all 14 runs |
| Provider requests | serial-X0 13, serial-R1 12, serial-R0 4, serial-R2 4, parallel-R1 21, plus the rest |
| Offline suite | 951 tests, 0 failures, 6 skipped; ruff, format, pyright clean |
| New deterministic tests | `tests/integration/test_final4_context_plane.py` (10 tests) covering the nine Gate-1 requirements |

Metric semantics caveat: the provider's `usage.prompt_tokens` was cache-miss-only in the 2026-11-14
runs and hit+miss in the corrected runs. Only `cache_hit_tokens` / `cache_miss_tokens` and their sum
are used for comparison. `collect_v2` publishes `prompt_tokens_total` and
`prompt_tokens_uncached` separately for this reason.

Evidence caveat: `metrics.extra` is not serialised by `RunMetrics.as_dict`, so the per-render evidence
of the eight matrix runs exists in the live transcript and in each run's `run.json`
(`environment.context_resolved`: builder, policy, renderer, transform, worker ids), but the deep
per-task artifact (`context.json`) was only introduced during this stage and exists for the
replication run. Final-5 must not repeat this: the runner now writes `context.json` for every run.

## H. Next step

**Final-5 is not ready yet.** The blocking items, in order:

1. **Track C is untested.** `build_task` must declare `required_artifacts` so goal coverage is
   provable; otherwise the router is blocked at gate 0 and R2 is just R0 with a different digest.
   This is a harness metadata defect, not a router change. Owner decision needed: declare the
   deliverables and re-run the three R2 cells (3 runs), or close Track C as `NOT_VALIDATED`.
2. **Native arm (N0) still blocked.** `baseline.py` runs on the host; a neutral in-sandbox native
   adapter is required before Track B can be claimed. Not started here.
3. **Placement** awaits approval of `docs/final-4-placement-integration-proposal.md`.
4. **n=1 per cell.** Every cell ran once (X1 twice). Final-5 needs repetitions before any difference
   is reported as a difference, especially for cache metrics.
5. **Holdout.** Not designed, not run, and explicitly out of scope for this round.

Not blocked: the Context Plane is now genuinely exercised end to end in a live coding run, and that was
the primary defect this stage was created to fix.
