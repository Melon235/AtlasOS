# Final-3 — Module Acceptance Report

Date: 2026-11-13. `INTEGRATED_SHA` = `93cbd98f9407e7358e997878238520dcac15827a` (runtime frozen).
Evidence commit: `97fa2d4230f6204309247efcfaa06f35bdf385c1`.

Machine-readable results: `experiments/results/final3/module_acceptance.{json,csv}`.
Raw acceptance record: `experiments/runs/final3/` (git-ignored).

---

## 1. Executive Summary

All 59 retained modules have an explicit, evidence-backed status on the frozen candidate.

- **Functional: 59 PASS.** Every module was exercised by an executed check: 925 offline tests, five
  new T2 microbenchmarks, one real-embedding retrieval study, one real MCP stdio run, an isolated
  ContextPilot environment, and the existing deterministic suites.
- **Benefit: 20 POSITIVE, 3 CONDITIONAL, 1 NEGATIVE, 1 NEUTRAL, 8 INSUFFICIENT_EVIDENCE,
  26 NOT_APPLICABLE.** No module was given a benefit status without a measured comparison.
- **Live model: 0 runs.** No provider credential exists in this workspace, so every live-model study
  is `BLOCKED`. This is the main gap and it is not hidden behind a fake provider.
- **Native environment parity: BLOCKED.** Tool capability maps 1:1, execution location does not.

---

## 2. Runtime Version

| Item | Value |
|---|---|
| Source | `93cbd98` (+ acceptance commit `97fa2d4`) |
| Python | CPython 3.12.15, `D:\Work\AtlasRuntime\.venv` |
| AnyCode | `anycode-py==0.9.0` (unchanged) |
| Key deps | pydantic 2.14.3, numpy 2.5.4, opensandbox 1.2.0, inspect-ai 0.3.289, mcp 1.30.0, sentence-transformers 6.3.0, faiss-cpu 1.16.0, torch 2.15.0 |
| Test config | `-m 'not live'`, `ATLASOS_RUN_LIVE_TESTS=0`, `ATLASOS_RUN_LIVE_SANDBOX=0` |
| Regression | 925 passed, 6 skipped, 1 deselected (241.57 s) |
| E2 | `D:\Work\AtlasRuntime-e2`, Python 3.12.15, contextpilot 0.5.0, pytest 9.2.0 + pytest-asyncio |

---

## 3. Full Module Acceptance Matrix

| Module | Functional | Benefit | Primary metric | Baseline → Treatment | Evidence |
|---|---|---|---|---|---|
| A1-01 ContextBlock/Ref | PASS | NOT_APPLICABLE | invariant | — | `test_context_models.py` |
| A1-02 ContextStore | PASS | NOT_APPLICABLE | invariant | — | `test_context_store.py` |
| A1-03 Selector | PASS | POSITIVE | irrelevant blocks injected | 2 → 0 | T2 `final3-selector-scope` |
| A1-04 Stable renderer | PASS | POSITIVE | layout equality rate | 0.0 → 1.0 | T2 `final3-stable-order` |
| A1-05 ContextPilot dedup | PASS (E2) | NEUTRAL | deduplicated blocks | 0 → 0 | E2 `final3-e2-dedup-12x3` |
| A1-06 Placement | PASS | POSITIVE | logical handoff refs | 3 → 0 | T2 `final3-placement-chain4` |
| A1-07 Context rebuild | PASS | NOT_APPLICABLE | invariant | — | `test_resume.py` |
| A1-08 History compaction | PASS | INSUFFICIENT_EVIDENCE | — | — | `test_history_compaction.py` |
| A1-09 Tool-result offload | PASS | INSUFFICIENT_EVIDENCE | — | — | `test_tool_result_offload.py` |
| A1-10 Cache metrics | PASS | NOT_APPLICABLE | invariant | — | `test_deepseek_usage.py` |
| A2-01 Registry | PASS | NOT_APPLICABLE | invariant | — | `test_tools.py` |
| A2-02 Catalog | PASS | NOT_APPLICABLE | invariant | — | `test_tools.py` |
| A2-03 Keyword search | PASS | INSUFFICIENT_EVIDENCE | Recall@1 | 0.80 | `final3-retrieval-24x10` |
| A2-04 Semantic search | PASS | POSITIVE | Recall@1 / R@3 / MRR | 0.80/0.90/0.85 → 0.90/1.00/0.95 | `final3-retrieval-24x10` |
| A2-05 FAISS index | PASS | NOT_APPLICABLE | invariant | — | `test_final3_semantic_search.py` |
| A2-06 Progressive disclosure | PASS | POSITIVE | native schema chars | 8 796 → 4 207 (−52.2 %) | T2 `final3-tool-surface-24` |
| A2-07 Native surface | PASS | NOT_APPLICABLE | invariant | — | `test_tools.py` |
| A2-08 RunToolState | PASS | NOT_APPLICABLE | invariant | — | `test_tools.py` |
| A2-09 dispatch_action | PASS | NOT_APPLICABLE | invariant | — | `test_tools.py` |
| A2-10 MCP | PASS | NOT_APPLICABLE | invariant | — | `test_tool_mcp.py` (real stdio) |
| A3-01 Scoped worker | PASS | NOT_APPLICABLE | invariant | — | `test_scoped_workers.py` |
| A3-02 Trusted identity | PASS | NOT_APPLICABLE | invariant | — | `test_scoped_workers.py` |
| A3-03 Profile/assignment | PASS | NOT_APPLICABLE | invariant | — | `test_scoped_workers.py` |
| A3-04 Workflow compiler | PASS | NOT_APPLICABLE | invariant | — | `test_workflow.py` |
| A3-05 AnyCode DAG | PASS | NOT_APPLICABLE | upstream | — | `test_anycode_dag.py` |
| A3-06 Dual handoff | PASS | INSUFFICIENT_EVIDENCE | — | — | `test_handoff.py` |
| A3-07 Shared budget | PASS | CONDITIONAL | overrun count | 1 rejection enforced | `test_budget.py`, `test_final3_coding_metrics.py` |
| A3-08 Router | PASS | NOT_APPLICABLE | decision determinism | — | `test_delegation_routing.py` (34) |
| A3-09 Ownership | PASS | NEGATIVE | oracle pass | 7/16 → 1/16 (historical) | `test_r1_1_ownership_*.py` |
| A3-10 Worker contract | PASS | INSUFFICIENT_EVIDENCE | violations | — | `test_worker_contract.py` |
| A4-01 Gate | PASS | POSITIVE | unauthorised effects | 0 | `test_action_safety.py` |
| A4-02 Policy | PASS | NOT_APPLICABLE | invariant | — | `test_action_safety.py` |
| A4-03 Binding digest | PASS | NOT_APPLICABLE | invariant | — | `test_action_ledger.py` |
| A4-04 Precedence | PASS | POSITIVE | hard-deny overrides | 0 | `test_control_plane_e2e.py` |
| A4-05 Ledger | PASS | POSITIVE | replayed effects | 0 | `test_durable_actions.py` |
| A4-06 Idempotency | PASS | POSITIVE | duplicate effects | 0 (cross-process) | `test_sqlite_idempotency_process.py` |
| A4-07 UNCERTAIN | PASS | POSITIVE | blind retries | 0 | `test_m1_m4_reliability.py` |
| A4-08 SQLite durability | PASS | POSITIVE | committed rows survive | 100 % | `test_m6_sqlite_process.py` |
| A4-09 Sandbox binding | PASS | NOT_APPLICABLE | invariant | — | `test_project_sandbox.py` |
| A4-10 Workspace tools | PASS | POSITIVE | host mutations | 0 | `test_project_sandbox.py` |
| A4-11 Ownership lock | PASS | NOT_APPLICABLE | invariant | — | `test_sandbox_ownership.py` |
| A4-12 Reattachment | PASS | INSUFFICIENT_EVIDENCE | — | — | `test_verified_reattachment.py` |
| A4-13 Preflight barrier | PASS | POSITIVE | partial batches executed | 0 | `test_m6_runtime_suspension.py` |
| A4-14 Suspension | PASS | POSITIVE | lost suspensions | 0 | `test_m6_runtime_suspension.py` |
| A4-15 Approval | PASS | POSITIVE | stale reuses | 0 | `test_m6_failure_matrix.py` |
| A4-16 TrustedRecoverySession | PASS | INSUFFICIENT_EVIDENCE | — | — | `test_stage_d_recovery.py` |
| A4-17 Recovery cleanup | PASS | POSITIVE | silent leaks on close | 0 | `test_stage_d_cleanup.py` |
| A4-18 Tool error semantics | PASS | CONDITIONAL | oracle pass | 2/4 → 4/4 (historical) | `test_stage_g3_1_agent_tool_errors.py` |
| A5-01 Trajectory | PASS | POSITIVE | observed actions | 365 → 591 (historical) | `test_m7_trajectory.py` |
| A5-02 Pairing | PASS | POSITIVE | orphan proposals | 0 | `test_r1_1_trace_integrity.py` |
| A5-03 Input digest | PASS | POSITIVE | digest coverage | 1.00 (historical) | `test_r1_1_trace_integrity.py` |
| A5-04 Execution integrity | PASS | NOT_APPLICABLE | invariant | — | `test_r1_1_trace_integrity.py` |
| A5-05 Reconciliation | PASS | NOT_APPLICABLE | invariant | — | `test_r1_1_trace_integrity.py` |
| A5-06 Metrics collector | PASS | NOT_APPLICABLE | invariant | — | `test_metrics.py` |
| A5-07 Failure attribution | PASS | POSITIVE | classified failures | 4/4 compound cases | `test_final3_coding_metrics.py` |
| A5-08 Verification | PASS | CONDITIONAL | false positives | 0 (historical, 66) | `test_r1_2_verification_*.py` |
| A5-09 Oracle sandbox | PASS | INSUFFICIENT_EVIDENCE | — | — | `test_r1_2h_oracle_sandbox.py` |
| A5-10 Inspect adapter | PASS | NOT_APPLICABLE | invariant | — | `test_final3_inspect_adapter.py` |
| A5-11 Evaluation runner | PASS | NOT_APPLICABLE | invariant | — | `test_evaluation.py` |

Totals: functional 59 PASS; benefit 20 POSITIVE, 3 CONDITIONAL, 1 NEGATIVE, 1 NEUTRAL,
8 INSUFFICIENT_EVIDENCE, 26 NOT_APPLICABLE.

---

## 4. Context Engineering Results

### 4.1 Selector (A1-03)

New workload: one task with one declared dependency and three completed predecessors. Before: every
completed output is available ⇒ 2 irrelevant blocks. After: `TaskScopedSelector` injects the task
block plus the declared dependency only ⇒ **0 irrelevant blocks**, dependency text present.

### 4.2 Stable renderer (A1-04)

Same dependency set, completion timestamps inverted. `naive_execution_order`: layout hash equality
**0.0**; `stable_deterministic`: **1.0**. This is layout determinism, not a cache claim.

### 4.3 Dedup (A1-05) — E2, NEUTRAL

Real `contextpilot==0.5.0` in a separate venv (`D:\Work\AtlasRuntime-e2`, same source commit). 12
blocks of ~900 chars at 0 / 25 / 50 % exact duplicates:

| Duplicate ratio | blocks in | blocks out | chars in | chars out | reduction | latency |
|---|---|---|---|---|---|---|
| 0.00 | 12 | 12 | 10 922 | 10 922 | 0 % | 1.49 ms |
| 0.25 | 12 | 12 | 10 920 | 10 920 | 0 % | 0.10 ms |
| 0.50 | 12 | 12 | 10 920 | 10 920 | 0 % | 0.09 ms |

Direct API probe: `dedup_chat_completions` returned `blocks_deduped=0, blocks_total=2, chars_before=0`
for three messages of which two were byte-identical. The adapter passes one message per block and
zips results 1:1, so it can rewrite content but **cannot drop a block**. The historical 44 % was
measured with a different payload shape; it is not reproduced and must not be quoted as this result.
The unique tail fact survived in every cell. 8 ContextPilot tests pass in E2 (`.pth` startup error
still printed and recorded).

### 4.4 Placement (A1-06)

New 4-node chain with declared dependency context: `capability_only` produced 3 logical handoff
refs, `context_locality` produced **0** (mean locality 1.0). Logical refs are not network or
inference cost.

### 4.5 Compaction / offload (A1-08, A1-09)

Functional only. Historically rolling compaction raised prompt tokens (+9.3 %) and ingress offload
was cost-neutral while increasing `max_turns` workers; this stage did not re-run them live, so both
are `INSUFFICIENT_EVIDENCE`, not "improved".

---

## 5. Tool Engineering Results

### 5.1 Retrieval (A2-03 / A2-04 / A2-05)

New 24-tool catalog and 10 new labelled queries (not the historical 24-query set):

| Metric | Keyword | Semantic (MiniLM + FAISS) |
|---|---|---|
| Recall@1 | 0.80 | **0.90** |
| Recall@3 | 0.90 | **1.00** |
| MRR | 0.85 | **0.95** |

Embedding dimension verified (384). Index rebuild after a registry change changes the retrievable
set. Unknown queries return three plausible tools without inventing ids. End-to-end within the
runtime: `tool_search` discloses `github.issue.get`, `dispatch_action` executes it, and an undisclosed
tool is refused.

### 5.2 Progressive disclosure (A2-06)

24 searchable tools: full exposure **8 796** schema chars vs **4 207** for the 8-tool native surface
(−52.2 %). This is a schema-footprint measurement, not a task-cost measurement.

### 5.3 MCP (A2-10)

Real stdio server (`tests/fixtures/mcp_policy_server.py`): discovery, hidden by default, search,
dispatch, teardown. No external business account involved.

---

## 6. Multi-Agent Coordination Results

- Scoped worker, trusted identity, profile/assignment, DAG integration, router: functional PASS with
  the existing deterministic suites (12, 12, 12, 10, 57 tests respectively).
- Shared budget: run-scoped admission rejects an over-limit request (`admission_rejections=1`,
  `stop_reason` set) and the coding path can now opt in via `ATLASOS_CODING_TOKEN_BUDGET`.
- Ownership: functional PASS, **benefit NEGATIVE** — the historical arm scored 1/16 against 7/16 and
  blocked `python3 -m pytest`; not re-run live here, and not rescued by tuning.
- Handoff and worker contracts: functional only (`INSUFFICIENT_EVIDENCE` for benefit).

## 7. Reliable Execution Results

18 modules, all functional PASS on deterministic evidence: gate outcomes and precedence, canonical
binding digests, ledger state machine, cross-process idempotency, `UNCERTAIN` without retry, SQLite
durability across two OS processes, binding conflicts, single-handle workspace tools, ownership lock,
verified reattachment, whole-response barrier, durable suspension, persistent approval with
revalidation, trusted recovery + cleanup, and tool-error semantics.

Benefit is `POSITIVE` only where an invariant was measured in this run (0 unauthorised effects, 0
replayed effects, 0 partial batches, 0 stale approvals). `INSUFFICIENT_EVIDENCE` is used for
reattachment and the trusted-recovery session because their evidence remains deterministic and no live
model run occurred.

## 8. Observability and Evaluation Results

Trajectory, pairing, input digest, integrity, reconciliation, metrics, verification, oracle, Inspect
adapter and the runner are functional PASS. Measured in this stage:

- Failure attribution: 4/4 compound cases classified, neither fact lost, no failed run turned into a
  completed one.
- Token semantics: `total = miss + hit` for a known payload; `prompt_tokens` keeps its historical
  miss-only meaning and is no longer confused with the benchmark field.
- Inspect adapter: success → 1.0, failure → 0.0 with the class preserved, missing result → 0.0
  (`NOT_EVALUATED`), never a pass.
- Verification and oracle remain `CONDITIONAL` / `INSUFFICIENT_EVIDENCE`: their strong historical
  numbers (0 FP on 66 deliverables, 28/28 oracle parity) were not re-measured here.

## 9. Integrated Context Evidence

`tests/integration/test_final3_context_dag.py` is the integration evidence: real engine, real DAG,
real prompt seam, recording provider. A's output reaches B exactly once inside `<dependency>` markers,
the system prompt stays out of the task body, both renders carry layout hashes, and the dependency
order is `["a"]`.

Not done: a live multi-stage coding DAG with a real model (see §11).

## 10. Environment Comparison

| Item | Native (N0) | Atlas (R0) | Equivalent |
|---|---|---|---|
| Tool names | 6 built-ins | 8 native (6 + search/dispatch) | capability 1:1 |
| Execution location | host (Windows) | sandbox (Linux, `network=none`) | **no** |
| Shell | `cmd` | POSIX | **no** |
| Setup cost | none | container provision | **no** |

Status: `BASELINE_PARITY_BLOCKED`. No cost/latency comparison is drawn.

## 11. Live Model Results

**0 task runs, 0 LLM requests, USD 0.00.** No `DEEPSEEK_API_KEY` exists in this workspace (no `.env`,
no process variable), and this stage does not read credentials out of another worktree. Live
sandbox acceptance, T3 ablations and the X0/X1 context comparison are therefore `BLOCKED`, not
"passed".

## 12. Negative, Neutral and Unproven Results

| Module | Status | Why |
|---|---|---|
| A3-09 ownership | NEGATIVE | enforce 1/16 vs 7/16; blocked legitimate `pytest` |
| A1-05 dedup | NEUTRAL | real transform removed nothing in the new workload; adapter cannot drop blocks |
| A3-06 handoff, A3-10 contracts, A1-08 compaction, A1-09 offload, A4-12 reattachment, A4-16 recovery session, A5-09 oracle | INSUFFICIENT_EVIDENCE | deterministic or historical evidence only |
| A3-07 budget, A4-18 tool errors, A5-08 verification | CONDITIONAL | benefit holds only under stated conditions |
| 26 modules | NOT_APPLICABLE | correctness mechanisms with no meaningful benefit ratio |

## 13. Blocked Mechanisms

1. Live model studies (all T3, X0/X1, native/Atlas acceptance) — credential absent.
2. Native environment parity — no neutral sandbox lifecycle layer.
3. Coding crash recovery — out of scope by decision (D4), not attempted.

## 14. Historical vs Current Evidence

| Metric | Historical | Current |
|---|---|---|
| Tool retrieval R@1 (24 tools) | 0.875 (M1 catalog/queries) | 0.90 (new catalog, new queries) |
| Tool schema chars | 8 558 → 4 207 (M1) | 8 796 → 4 207 (24 tools) |
| Dedup prompt reduction | up to 44.0 % (Stage 6 shape) | 0 % (new shape; NEUTRAL) |
| Placement handoff | −50/66.7/80 % (chain 2/3/5) | 3 → 0 refs (chain 4) |
| Failure attribution | `MODEL_UNFINISHED` added (Final-1.6) | precedence and token semantics fixed |
| Offline suite | 891 passed (Final-1.6) | 925 passed (+34) |

Differences come from new workloads and from added tests; historical numbers were not reproduced
by design and are not required to match.

## 15. Final Readiness

| Dimension | Verdict |
|---|---|
| Module acceptance | **PASS** (59/59 functional; benefits separated and mostly conservative) |
| Runtime integration | **PASS** (R-4/R-5/R-6/R-7 implemented and tested) |
| Native baseline | **BLOCKED** (environment parity) |
| Context integration | **PASS** (deterministic, real prompt seam) / **NOT_VALIDATED** live |
| Tool engineering | **PASS**, including real semantic retrieval and MCP |
| Reliability | **PASS** functional, deterministic evidence only |
| Router functional readiness | **PASS** (mechanism), Track C protocol pending |
| Formal evaluation readiness | **PARTIAL** (live track blocked; thresholds unconfirmed) |

## 16. Recommended Next Action

Proceed to **Final-4 (Integrated Runtime and Router Pilot)** only after providing a provider
credential and deciding the native-environment question. Final-4 is not started here.
