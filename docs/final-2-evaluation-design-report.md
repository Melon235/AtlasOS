# Final-2 — Evaluation Protocol, Integration Contract and Independent Workload Design

Date: 2026-11-13. Branch: `chore/final-1-5-repository-baseline`. `BASE_SHA` =
`bd33952314ecee8a3e71d4a8b7231d325a2c26fa`.

This stage produced a protocol. It ran no experiment, changed no source file, created no task
fixture, and called no model.

Deliverables:

| Document | Content |
|---|---|
| `docs/evaluation/module-evaluation-matrix.md` | 59 modules × 24 fields: functional check, benefit check, workload, acceptance, readiness |
| `docs/evaluation/final-evaluation-protocol.md` | Track B: N0 vs R0, environment equality, frozen composition, thresholds |
| `docs/evaluation/runtime-integration-contract.md` | Minimal Context-Plane ↔ coding wiring, file allowlist, acceptance gates, metric-fix candidates |
| `docs/evaluation/metrics-contract.md` | One definition per metric, missing-value policy, comparability |
| `docs/evaluation/router-evaluation-spec.md` | Track C: C0/C1/C2, regret, success and stop conditions |
| `docs/evaluation/holdout-design.md` | Development / validation / holdout, task structures, manifest schema, candidate slate |
| `docs/evaluation/environment-protocol.md` | E1 / E2 / E3, environment digest, fault-injection safety |

---

## 1. Executive Summary

The evaluation is now specified at the level where it can be executed without further design
decisions, **except** for the items in §11 that need the owner's approval. The protocol is built around
three independent tracks, one metric contract, one environment protocol, and one rule that no optional
mechanism is switched on to make a number look better.

Three findings shaped the design:

1. **The Context Plane and the coding runtime still do not share a run.** The seam exists
   (`AtlasContextEngine(prompt_builder=…)`), so a minimal wiring is designed — but it is a source
   change and is not implemented.
2. **Semantic tool search has zero tests** despite installed dependencies and a cached model. That is
   the largest coverage gap and it is a test task, not a runtime task.
3. **The native baseline is not environment-equivalent** to Atlas today (host + Windows shell vs Linux
   sandbox). Track B cannot report a cost claim until a thin neutral environment adapter exists; the
   fallback is a documented confound.

No historical number is reused as a Final-2 result.

---

## 2. Frozen design decisions (D1–D8)

| ID | Decision | Consequence in this protocol |
|---|---|---|
| D1 | All five pillars retained | the matrix covers 59 modules across A1–A5; none is declared obsolete |
| D2 | Three independent tracks | Track A (module), Track B (integrated), Track C (delegation); C never substitutes A or B |
| D3 | Software engineering only | all candidate workloads are SWE tasks; no business adapter is reintroduced |
| D4 | No general coding crash recovery | checkpoint wiring and resume-entry work are out of scope; only workflow resume, context rebuild, suspension, approval, reattachment, ledger classification are validated |
| D5 | Limited Context-Plane integration is a candidate | designed in `runtime-integration-contract.md`, not implemented |
| D6 | Router is admission, not planning | Track C uses externally provided candidate plans; no planner, no Router V2, no threshold change |
| D7 | ContextPilot in an isolated environment | E2; never installed into E1; results never merged |
| D8 | Negative results retained | benefit status vocabulary includes NEGATIVE / NEUTRAL / INSUFFICIENT_EVIDENCE; no task or run is deleted |

---

## 3. Module coverage

| Pillar | Modules | Executable now | Needs test | Needs env / integration | Not measurable |
|---|---|---|---|---|---|
| A1 Context Engineering | 10 | 5 | 2 | 2 | 1 |
| A2 Tool Engineering | 10 | 6 | 3 | 1 | 0 |
| A3 Multi-Agent Coordination | 10 | 7 | 0 | 2 | 1 |
| A4 Reliable Execution | 18 | 16 | 0 | 2 | 0 |
| A5 Observability and Evaluation | 11 | 8 | 1 | 2 | 0 |
| **Total** | **59** | **42** | **6** | **9** | **2** |

Largest gaps, in order of risk:

1. **A2-04 / A2-05 — semantic search and FAISS: 0 tests.** Dependency installed, model cached, no
   check that the real backend works or that it beats keyword search on unseen queries.
2. **A5-10 — Inspect AI adapter: 0 tests.** An external harness that has never been exercised could
   mis-score silently.
3. **A1-05 — ContextPilot: executable only in E2.** In-suite coverage is skipped by design.
4. **A1-03/A1-04 in the coding path — not wired.** Without the integration, a context-plane result
   describes the workflow path only.
5. **A3-10 — worker contract delivery** is evaluation-only code with T1 coverage but no independent
   workload.

Every module row states a functional acceptance and a benefit acceptance. Modules whose value is
correctness (gate, ledger, renderer determinism) are scored by absolute invariants and overhead, not by
a percentage improvement.

---

## 4. Runtime integration

**Recommended final composition for Track B (R0):** progressive tools (keyword) + action gate +
SQLite ledger + sandbox binding + trajectory + guarded budget, single worker, context plane at
`upstream_default`. This is the composition that exists today and that has T1 coverage.

**Recommended composition for the integration sub-study:** the same, plus
`context.policy="task_scoped"`, `renderer="stable_deterministic"`, `transform="identity"`, with a
`prompt_builder` wired at `app/coding.py:646` and `runner.py:1003`.

**Minimal wiring (designed, not implemented):** one new module
`src/atlasos/context/task_prompt.py` holding the projection + selector + renderer call, `runtime.py`
delegating to it (pure refactor), and the two compositions passing a builder. Four source files, two
test files. Forbidden: touching the ContextStore, selectors, renderer, tools, workers, gate, ledger,
sandbox, budget, handoff, engine subclass, or `OrchestratorConfig`.

**Blockers to the integration claim:** none of a design nature. The seam exists and the data structures
do not change. `INTEGRATION_DESIGN_BLOCKED` is **not** declared. The one condition that would change
this: if `_build_task_prompt` turns out to be invoked after a conversation has started (it is not,
per the current orchestrator), the design must stop.

**Recovery scope:** unchanged. No resume engine, no CLI, no `OrchestratorConfig` change.

---

## 5. Independent workloads

| Class | Content | Status |
|---|---|---|
| Development | 30-task `atlasos-agentbench-v1`; E1 families; R0/R0.5 families; every task debugged against a result | exists |
| Validation | 3 authored tasks (`v-s1-*`, `v-p1-*`, `v-c1-*`) | PLANNED |
| Holdout | 6 authored tasks (`h-s1`, `h-s2`, `h-p1`, `h-p2`, `h-c1`, `h-c2`) | PLANNED |

No task has been created. No public benchmark task has been imported, named, or licence-checked; the
protocol says explicitly that naming one before intake would create a false record. Candidate plans are
authored from public information only and frozen with a digest before any run.

Task structures S/P/C are structural sampling labels, not router ground truth. The holdout
deliberately includes at least one task that is unsuitable for delegation.

---

## 6. Evaluation groups

| Group | Arms | Scale | Purpose |
|---|---|---|---|
| Track A | per-module T1/T2/T3 | model-free except 4 T3 studies | functional + benefit per mechanism |
| Track B pilot | N0, R0 | 3 tasks × 1 rep = 6 | protocol smoke |
| Track B formal | N0, R0 | 6 tasks × 2 reps = 24 | integrated runtime evidence |
| Track C | C0, C1, C2 | 6 tasks × 2 reps × 3 arms = 36 | delegation evidence |

C0 may reuse R0 samples when everything else is identical, reducing Track C to 24 new runs plus the
reused C0 cells; the report must state which cells were reused.

---

## 7. Metrics

One contract (`metrics-contract.md`) with strict rules: `runtime_status` and `oracle_pass` are separate
facts; missing values are `null`/`UNAVAILABLE`, never `0`; `prompt_tokens = hit + miss` when the
provider reports the split; cost uses a frozen price snapshot; time is reported per phase and never
collapsed into "runtime overhead"; trace coverage needs an independent reference count; native arms
without a trajectory report `NOT_AVAILABLE`, not zero.

Known semantic traps that the contract now forbids:

- `TokenUsage.input_tokens` is not `prompt_tokens` (it excludes cache reads) — the Final-1.6 port's
  `session.prompt_tokens` field is therefore miss-only.
- App status enumerations and evaluation status enumerations are different vocabularies.
- `result.json` undercounts actions (365 vs 591 historically); only the trajectory can be used for
  action coverage.

---

## 8. Router

Track C measures **admission**, using externally provided candidate plans. `DIRECT` collapses a run to
the single-worker plan; `DELEGATE_PARALLEL` runs the candidate DAG; `DELEGATE_ONE` has no distinct
execution semantics today and must be scored by executed worker count. The router reads only public
task information and static analysis, never oracle data.

Reported: oracle pass, cost, wall time, delegation rate, actual multi-execution rate, harmful /
beneficial / avoided delegation, routing regret, both-fail and inconclusive cases.

Success requires that the router both avoids a harmful delegation **and** actually delegates somewhere
with a measurable win, without losing quality. One-hundred-percent `DIRECT` is reported as
"degenerate to always-single", not as adaptive success. No Router V2 follows a negative result.

---

## 9. Environment

- **E1** — the main `.venv`, CPython 3.12.15, AnyCode 0.9.0, extras app/dev/evaluation/sandbox/tools,
  no ContextPilot, 891 passed / 6 skipped / 1 deselected.
- **E2** — a separate venv for the real ContextPilot transform; same commit; its results are never
  merged into E1 counts; the `.pth` startup error is recorded verbatim.
- **E3** — live model + OpenSandbox; frozen provider/model/sandbox/image/oracle/budget; fresh
  workspace per run; environment digest in every result header.

Fault injection is permitted only with ownership records, a resource inventory, a verifiable cleanup
step, and an unknown-side-effect rule. Operator cleanup is acceptable; an unplanned kill is not.

---

## 10. Resource budget

| Block | Runs | Basis | Estimated cost |
|---|---|---|---|
| Track A T1/T2 | ~60 suites/cells | no model | USD 0 |
| Track A T3 (dedup live, tool discovery, handoff, budget) | ~24 live runs | historical `$0.0027`/run (E1) | ≈ USD 0.07 |
| Track B pilot | 6 | 1 task ≈ `$0.002–0.004` | ≈ USD 0.02 |
| Track B formal | 24 | same | ≈ USD 0.08 |
| Track C formal (C1/C2 new + reused C0) | 24–36 | multi-worker historically 4.12× per run | ≈ USD 0.20–0.45 |
| Integration sub-study | 6 | 2 tasks × 3 reps | ≈ USD 0.02 |
| **Total** | **≈ 90–110 runs** | | **≈ USD 0.4–0.7 (upper bound ≈ USD 2)** |

Basis and error: historical E1 cost `0.0987 USD / 36 runs`, 30-task benchmark `0.19115 USD / 90 runs`
(peak-rate upper bound), R0.5 multi-worker `+40 %…+269 %` per run. Estimate error is at least ×3 because
token counts depend on task difficulty and multi-worker fan-out. No estimate here was produced by
running a model; the numbers are re-scaled historical measurements and must be re-checked after the
pilot.

Limits per task (frozen): 120 000 tokens, 40 tool calls, 14 turns per worker, 900 s wall time, max 2
concurrent workers, retry only pre-declared infrastructure classes.

---

## 11. Required changes

| # | Problem | Location | Suggested files | Not to touch | Acceptance test | Owner decision |
|---|---|---|---|---|---|---|
| R-1 | Real semantic retrieval has no test (A2-04, A2-05) | `tools/search.py` | `tests/unit/test_semantic_tool_search.py`, `tests/integration/test_tool_search_semantic.py` (new), a new tool catalog fixture inside `tests/fixtures/` | `tools/search.py` behaviour; ranking formula | 384-dim embeddings, Top-K, id mapping, empty/ambiguous query, rebuild after registry change | no |
| R-2 | MCP is only covered offline | `tools/runtime.py` | one live stdio test in `tests/integration/test_tool_mcp.py` | MCP bridge | discovery + disclosure + dispatch against a local server | no |
| R-3 | Inspect adapter has no test (A5-10) | `experiments/evaluation/inspect_adapter.py` | `tests/integration/test_inspect_adapter.py` (new) | adapter semantics | dataset→solver→scorer mapping; exception ⇒ non-PASS; no raw run data committed | no |
| R-4 | Context Plane not wired into coding | `app/coding.py:646`, `runner.py:1003`, `runtime.py` | `context/task_prompt.py` (new), `runtime.py` (delegate), `app/coding.py`, `runner.py`, 2 test files | ContextStore, selectors, renderer, tools, workers, gate, ledger, sandbox, budget, handoff | `runtime-integration-contract.md` §3.4 (8 tests) + §4 gates | **yes** |
| R-5 | Shared budget does not cover the coding path | `app/coding.py` | `app/coding.py` (+15 lines) | `budget.py` | one `RunBudgetState` id for the whole run; restriction obeyed by every worker | yes (bundled with R-4) |
| R-6 | `MODEL_UNFINISHED` can mask `SANDBOX_PROVIDER_FAILURE` | `app/coding.py:687-724` | `app/coding.py` (M-1) | gate/ledger/sandbox semantics | compound-failure test: both facts retained | yes |
| R-7 | `session.prompt_tokens` is cache-miss only | `app/coding.py:687-700` | `app/coding.py` (M-2), `app/service.py` if it surfaces the value | metric names already published | `total = miss + hit` on a recorded payload | yes |
| R-8 | `patch_changed_files` miscounts | `sandbox/export.py` | `sandbox/export.py` (M-3) | patch generation | 2-file patch ⇒ `changed_files == 2` | no |
| R-9 | App has no `input_digest_coverage` | `app/coding.py`, `trajectory/reader.py` | M-4 | trajectory schema | scripted app run reports the metric | no |
| R-10 | Native baseline is not environment-equivalent | `experiments/evaluation/baseline.py` | `experiments/evaluation/neutral_env.py` (new, evaluation-only) | AnyCode loop, any Atlas mechanism | N0 runs inside the same sandbox image with the same 6 tools | **yes** |
| R-11 | R1-0 analysis test skips without raw runs | `tests/unit/test_stage_r1_0_analysis.py:542` | — | historical data | either ship a sanitized fixture or mark the test as environment-gated in the report | no |
| R-12 | Ordinary coding crash recovery | — | — | everything | none | **out of scope (D4)** |
| R-13 | Automatic planner | — | — | everything | none | **out of scope (D6)** |

Classes: R-1/R-2/R-3 = REQUIRED_TEST_COVERAGE; R-4/R-5 = REQUIRED_INTEGRATION (needs approval);
R-6/R-7 = metric correctness (DEFERRED until owner decides, since they change published fields);
R-8/R-9 = DEFERRED_TECHNICAL_DEBT; R-10 = REQUIRED before a Track B cost claim, optional with a
documented confound; R-11 = documentation; R-12/R-13 = OUT_OF_SCOPE.

No change in this table is implemented in Final-2.

---

## 12. Limitations

- An independent workload is not production validation. Six holdout tasks describe six tasks.
- `n = 6` per arm cannot establish statistical equivalence; results are descriptive.
- Public tasks may be in a model's training data. No claim of contamination-free evaluation is made.
- The router is an admission policy, not a planner; the runtime cannot decompose a task on its own.
- Ordinary coding crash recovery is unsupported and is not measured. Only the durability mechanisms
  listed in D4 are validated.
- The Context Plane is not integrated into the coding runtime today; until R-4 lands, context results
  describe the workflow path only.
- Historical metrics were produced on other commits and task sets; none of them is a Final-2 result.
- The native baseline's environment differs from Atlas until R-10 is decided.
- Cache measurements are provider-controlled; only "first-in-run" vs "warm" is claimed.

---

## 13. Readiness

| Dimension | Verdict | Basis |
|---|---|---|
| Module Protocol | **PASS** | 59 modules with functional and benefit checks; 6 need tests, 9 need env/integration, 2 not measurable |
| Runtime Integration Design | **PASS** (design) / **NOT_VALIDATED** (implementation) | seam identified, file allowlist fixed, acceptance gates written; nothing implemented |
| Holdout Design | **PARTIAL** | rules, schema and slate defined; no task created or verified |
| Native Baseline | **PARTIAL** | baseline exists and runs, but is not environment-equivalent (R-10) |
| Router Protocol | **PASS** | arms, plan source, metrics, regret, success and stop conditions defined; 57 tests exist |
| Environment | **PASS** | E1 measured, E2/E3 specified with digests and prohibitions |
| Formal Evaluation | **NOT_VALIDATED** | no formal run has occurred; thresholds await owner confirmation |

Protocol completion is not evaluation success. Nothing here is PASS as a measurement.

---

## 14. Next action

1. Project owner reviews this report and the seven documents under `docs/evaluation/`.
2. Owner decides R-4/R-5 (integration + budget), R-6/R-7 (metric correctness), R-10 (neutral
   environment), and confirms the Track B thresholds in `final-evaluation-protocol.md` §4.1.
3. **Final-3** then performs, in order: the approved minimal integration with tests, real semantic
   search coverage, the module acceptance matrix run (T1/T2 first), the E2 ContextPilot study, the
   Track B/C pilot on the validation set, and only then the frozen formal run.

Final-3 is not started here.
