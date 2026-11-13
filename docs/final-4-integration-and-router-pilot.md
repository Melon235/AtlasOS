# Final-4 — Integrated Runtime and Router Pilot

Date: 2026-11-13.

| Item | Value |
|---|---|
| `BASE_SHA` | `603e9743b0680ac85def47d919c2241aa960beec` |
| Frozen runtime | `93cbd98f9407e7358e997878238520dcac15827a` (unchanged: `src/` diff empty) |
| `PILOT_EVAL_SHA` | `28428e8d3b6c167a99736aac2dc17d1b61aa3658` |
| Final HEAD | the commit that adds this document |
| Branch | `chore/final-1-5-repository-baseline` |

Machine-readable: `experiments/results/final4/{module_supplement,pilot_results,router_comparison}.json`
and `pilot_results.csv`. Raw runs would live in `experiments/runs/final4/` (git-ignored); none were
produced.

---

## 1. Executive Summary

The offline half of Final-4 is complete and adds real evidence. The live half did not run.

- **ContextPilot was mis-judged in Final-3 and is now diagnosed.** D0–D3 show the transform works
  (31.84 % char reduction on a multi-line payload) and that Final-3's zero result was caused by
  single-line blocks that the chunker cannot split. A1-05 moves from NEUTRAL to POSITIVE under a stated
  condition.
- **Tool retrieval now has scale evidence** at 64 and 128 tools with 32 frozen queries: semantic
  Recall@1 0.81 → 0.91, MRR 0.86 → 0.93, at 6.8–7.3 ms per warm query. Unknown-tool rejection does
  **not** exist in either backend.
- **Context selection/renderer** were measured on five topologies: 0 irrelevant blocks injected,
  stable layout 5/5 vs naive 3/5, and one genuine semantic limit — direct dependencies only.
- **R-10**: the neutral backend is implemented and tested inside unmodified AnyCode; **container
  parity is still blocked** (no sandbox credential).
- **Live Pilot (N0/R0/R1/R2, X0/X1, T3): 0 of 18 planned cells executed** — `LIVE_PROVIDER_BLOCKED`.
  No Router observation exists, so Track C remains unmeasured rather than negative.

## 2. Git Provenance

`BASE_SHA 603e974` → `28428e8` (evaluation infrastructure + results) → docs commit.
`src/`, `configs/`, `examples/`, `.github/`, `pyproject.toml`, `uv.lock` are unchanged from
`BASE_SHA`; the Router source is untouched.

## 3. Environment Readiness

| Item | State |
|---|---|
| Python | 3.12.15, `D:\Work\AtlasRuntime\.venv` |
| AnyCode | 0.9.0 (unchanged) |
| Semantic stack | sentence-transformers 6.3.0, faiss-cpu 1.16.0, `all-MiniLM-L6-v2` cached and loadable offline |
| Provider credential | **missing** (`DEEPSEEK_API_KEY` unset, no `.env`) |
| Sandbox credential | **missing** (`build_sandbox_provider(...).health()` → `SandboxCredentialError`) |
| ContextPilot | isolated E2 only (`D:\Work\AtlasRuntime-e2`, 0.5.0) |

## 4. Native Environment Parity

Adapter: `experiments/evaluation/final4/neutral_adapter.py` — six tools executed by an external
backend inside unmodified AnyCode; ten tests cover file read/write/edit/grep/list/shell, a path-escape
refusal, and the exact six-tool surface.

| Item | Native | Atlas | Equivalent |
|---|---|---|---|
| Tool names | 6 baseline | 8 native (6 + search/dispatch) | capability 1:1 |
| Tool execution backend | local process (adapter) | sandbox | **no** |
| Execution location | host | container | **no** |
| Isolation | directory + subprocess | container, `network=none` | **no** |
| Setup cost | none measured | provision measured | **no** |

**Status: `ADAPTER_READY_ENVIRONMENT_BLOCKED`.** `capability_report()` still returns
`BASELINE_PARITY_BLOCKED`, so no cost or latency comparison is drawn.

## 5. Frozen Runtime Configurations

| Config | Definition | Status |
|---|---|---|
| N0 | native AnyCode 0.9.0 single, neutral backend | blocked (no sandbox, no key) |
| R0 | Atlas single: progressive tools (keyword), scoped worker, gate, ledger, sandbox, guarded budget, trajectory | blocked (no key) |
| R1 | same core + frozen candidate DAG, no ownership/compaction/offload/ContextPilot | blocked |
| R2 | same core, Router decides between the R0 path and the R1 DAG | blocked |
| X0/X1 | `upstream_default` vs `task_scoped + stable_deterministic + identity` | blocked |

No configuration was executed, so none of the above is an observation.

## 6. Validation Tasks

Planned: `validation-serial` (S), `validation-parallel` (P), `validation-coupled` (C). None was created
in this stage because a live run was impossible; they stay `PLANNED` with no fabricated digests. The
holdout (6 tasks, 2 per shape) remains untouched and `HOLDOUT_NOT_READY`.

## 7. Module Live Ablations

Planned 12 cells (tool disclosure 4, context 4, budget/handoff 4). **0 executed.** The offline
counterparts that did run are recorded in `docs/final-4-module-evidence-supplement.md`.

## 8. Integrated Runtime Results

No live execution. What can be stated from executed offline evidence:

- the context plane renders real prompts through the production builder (Final-3 DAG test, still green);
- the neutral backend executes real file/shell work under native AnyCode (new, ten tests);
- 935 offline tests pass, 6 skipped, 1 deselected.

Nothing here demonstrates a live integrated run.

## 9. Router Pilot Results

No run. The mechanism statements below come from source reading, not execution:

| Claim | Evidence |
|---|---|
| `DIRECT` replaces the plan with the single-worker plan | `runner.py:847-859` |
| `DELEGATE_PARALLEL` runs the candidate DAG unchanged | `runner.py:860-862` |
| `DELEGATE_ONE` has no distinct execution semantics | runner branches on `is_direct` only |
| Router never reads oracle data | `workers/routing.py` has no oracle import |
| Router is not wired into application coding | no `src/` importer |
| Policy version | `r0.1.0`, unmodified |

## 10. Failure Attribution

Every planned live cell is recorded as `NOT_RUN` with `failure_class=INFRASTRUCTURE_BLOCKED` and the
exact blocker (`LIVE_PROVIDER_BLOCKED` for model work, `SANDBOX_BLOCKED` for native container work).
No cell is silently missing, and no cell is reported as a model failure.

## 11. Resource Accounting

| Item | Value |
|---|---|
| Task runs | 0 |
| LLM requests | 0 |
| Cost | USD 0.00 |
| Sandboxes created | 0 |
| Sandboxes destroyed | 0 |
| Offline suites | 1 full run (935 passed) |
| Offline studies | 4 (topologies, tool scale, E2 diagnosis, neutral backend) |

## 12. Routing Assessment

There is **no evidence** for or against the router on live tasks: no run happened, so
`delegation_rate`, `actual_multi_execution_rate`, harmful/beneficial/avoided delegation and
`routing_regret` are all `null`. The router is therefore neither degenerate nor proven — it is
**unmeasured**. No threshold, rule or policy change is proposed, because any such change would need
data that does not exist.

## 13. Holdout Readiness

`HOLDOUT_NOT_READY`: the six holdout tasks are not created, no oracle exists, no candidate plans are
frozen. The selection rules and manifest schema in `docs/evaluation/holdout-design.md` stand unchanged.

## 14. Final Readiness

| Dimension | Verdict |
|---|---|
| Track A (module evidence) | **PARTIAL** — 4 mechanisms gained controlled benefit evidence; 7 remain deterministic-only or historical; live ablations blocked |
| Track B (integrated runtime) | **BLOCKED** — no provider, no sandbox |
| Track C (router) | **BLOCKED** — no run; mechanism verified by reading only |
| Context integration | **PARTIAL** — deterministic + builder-level evidence; no live DAG |
| Native parity | **BLOCKED** — adapter ready, container absent |
| Formal evaluation | **BLOCKED** — holdout absent, credentials absent |

## 15. Recommended Next Action

1. Owner reviews this report.
2. Provide a provider credential and a sandbox credential (or an approved alternative endpoint).
3. Re-run Final-4 Phase 6–9 exactly as specified on `93cbd98` / `28428e8` — no code change needed.
4. Only then freeze the formal version and enter Final-5.

No router change, no planner, no new runtime feature is requested by this stage.
