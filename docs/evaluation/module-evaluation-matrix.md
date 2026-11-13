# Module Evaluation Matrix — Track A

Scope: every retained module in `src/atlasos/` plus the evaluation layer, with a functional check and a
benefit check that can actually be executed. This document is a **protocol**, not a result. No module
below has been re-validated against the Final-2 protocol.

Sources of truth for the claims in this file: `docs/runtime-entrypoints.md` (what is wired where),
`docs/evaluation-summary.md` (historical numbers, frozen at their own commits),
`docs/final-1-6-evaluation-baseline.md` (measured environment and coverage).

---

## 1. How to read this matrix

### 1.1 Field template

Every module carries the 24 required fields. To stay readable they are split into two places:

| Location | Fields |
|---|---|
| Table row | ID, Module, Source: symbol, Entry point / status, Functional method (T), Functional acceptance, Benefit baseline → primary metric, Benefit acceptance, Existing coverage, Readiness |
| Detail block | Problem, Benefit hypothesis, Independent workload, Secondary metric, Runtime overhead, Negative / boundary cases, Dependencies, Missing coverage, Environment, Planned sample, Evidence output |

### 1.2 Origin labels

| Label | Meaning |
|---|---|
`ATLAS` | written in this repository, no upstream equivalent |
`ATLAS-ON-ANYCODE` | an Atlas seam that overrides or wraps a specific AnyCode class/method |
`ANYCODE` | upstream capability that Atlas only configures or observes — **never** counted as an Atlas contribution |
`3P` | third-party dependency (ContextPilot, Inspect AI, OpenSandbox, FAISS/SentenceTransformers) |

### 1.3 Validation types

| Code | Meaning | Typical cost |
|---|---|---|
`T1` | Deterministic functional test — fake adapter, fake sandbox, real SQLite/OS process where the semantic is durability | seconds, no model |
`T2` | Controlled microbenchmark — real parser/index/transform, no model | seconds, no model |
`T3` | Live-model ablation — only where the benefit depends on model behaviour | USD, sandbox |

Preference order is T1 → T2 → T3. A module gets T3 only if its hypothesis cannot be measured without a
model. No module gets a full 30-task benchmark of its own.

### 1.4 Status and readiness vocabulary

`Functional Status`: PASS / FAIL / PARTIAL / NOT_VALIDATED / BLOCKED.
`Benefit Status`: POSITIVE / CONDITIONAL / NEUTRAL / NEGATIVE / INSUFFICIENT_EVIDENCE.
`Readiness`: `READY` (protocol executable now) / `NEEDS_TEST` (no valid check exists yet) /
`NEEDS_ENV` (blocked by environment, not by code) / `NEEDS_INTEGRATION` (needs the wiring designed in
`runtime-integration-contract.md`) / `NOT_VALIDATED` (cannot be measured honestly yet) / `BLOCKED`.

Nothing in this matrix is marked PASS or POSITIVE by this stage. The `Benefit acceptance` column states
the *criterion*, and the historical column in `evaluation-summary.md` states what was measured before.

### 1.5 Shared defaults

To avoid repeating identical cells 59 times, these defaults apply unless a row overrides them:

- **D-ENV**: functional checks run in E1 (`D:\Work\AtlasRuntime\.venv`, offline); live ablations run in
  E3 (`environment-protocol.md`).
- **D-NULL**: any unmeasured metric is `null` / `UNAVAILABLE`, never `0` (`metrics-contract.md` §2.6).
- **D-OUT**: every run writes `result.json`, `inference.jsonl`, and `trajectory/` (or an explicit
  `NOT_AVAILABLE` for the native arm).
- **D-REP**: T1 = every listed semantic, one deterministic run; T2 = 5 repetitions post warm-up;
  T3 = ≥3 repetitions per cell unless stated otherwise.
- **D-FAIR**: the fair baseline is "the same runtime with the mechanism switched off", not "no
  runtime".

---

## 2. Coverage summary

| Pillar | Modules | T1 | T2 | T3 | Ready | Needs test | Needs integration / env | Not measurable |
|---|---|---|---|---|---|---|---|---|
| A1 Context Engineering | 10 | 7 | 6 | 3 | 5 | 2 | 2 (ContextPilot env) | 1 (cache effect) |
| A2 Tool Engineering | 10 | 7 | 3 | 2 | 6 | 3 (semantic/FAISS/MCP live) | 1 | 0 |
| A3 Multi-Agent Coordination | 10 | 8 | 1 | 3 | 7 | 0 | 2 (router C2, contracts) | 1 |
| A4 Reliable Execution | 18 | 18 | 0 | 1 | 16 | 0 | 2 (crash-resume OOS) | 0 |
| A5 Observability & Evaluation | 11 | 9 | 1 | 1 | 8 | 1 (Inspect adapter) | 2 | 0 |
| **Total** | **59** | — | — | — | **42** | **6** | **9** | **2** |

Counts are design intents, not results. "Needs test" means no valid check exists today (semantic
search, FAISS index, MCP live, Inspect adapter, ContextPilot transform in-suite, R1-0 raw-run test).

---

## 3. A1 — Context Engineering

| ID | Module (origin) | Source: symbol | Entry / status | Functional (T) | Functional accept | Baseline → metric | Benefit accept | Cov. | Ready |
|---|---|---|---|---|---|---|---|---|---|
| A1-01 | ContextBlock / ContextRef / ContextKind (ATLAS) | `context/models.py`, `context/fingerprints.py` | workflow path; library | T1: version/immutability, ref→block resolution, layout hash stability | identical logical content ⇒ identical hash; no volatile field | — | structural only | 12 T1 | READY |
| A1-02 | ContextStore (ATLAS) | `context/store.py::InMemoryContextStore` | workflow path | T1: put/get/contains/version bump/replace | version monotonic; no duplicate block on re-projection | — | structural only | 9 T1 | READY |
| A1-03 | TaskScopedSelector (ATLAS) | `context/selectors.py` | workflow path | T1: dependency scoping, non-dependency exclusion | task sees only declared deps; unrelated blocks never selected | full-context selection → selected block count / prompt tokens | selection ⊆ dependency closure, 0 unrelated blocks | 3 T1 + E2 | READY |
| A1-04 | StablePromptRenderer (ATLAS) | `context/renderer.py` | workflow path, default in app | T1: order invariance; T2: layout hash under shuffled arrival | two completion orders ⇒ same layout + full-prompt hash | `NaiveExecutionOrderRenderer` → layout-hash equality rate | 100 % equality over ≥20 orderings | 7 T1 + S6-A | READY |
| A1-05 | ContextPilot dedup transform (ATLAS-ON-ANYCODE + 3P) | `context/transform.py::ContextPilotDedupTransform` | optional, needs extra | T2 in E2: exact-duplicate ratios 0/0.16/0.31/0.5; real `contextpilot` | identical non-duplicate content preserved; refs resolvable | identity transform → prompt tokens, cache miss, transform ms | ≥1 positive rung with non-negative miss; else NEGATIVE | 4 T1 (skipped) + S6-B | NEEDS_ENV |
| A1-06 | Context-locality placement (ATLAS) | `placement/planner.py`, `placement/policies.py` | optional (`bypass` default) | T1: locality vs capability assignment; T2: handoff refs by chain length | same task set, different assignment; no task loss | `capability_only` → logical handoff refs, switches, locality | ≥50 % handoff reduction on chain ≥2 with 0 task loss | 8 T1 + S6-C | READY |
| A1-07 | Durable context rebuild (ATLAS) | `durability/context_rebuild.py` | workflow resume only | T1: rebuild from checkpoint then resume; missing-block invariant raises | resumed task sees byte-equivalent dependency context | re-projection from restored queue → rebuild fidelity | 0 duplicate blocks, 0 missing deps | 8 T1 + E5 | READY |
| A1-08 | History compaction (ATLAS) | `history_compaction.py` | evaluation-only opt-in | T1: placeholder↔`history.read` byte-exact round trip; tamper rejection | paged read is byte-exact; archive never mutates | no compaction → replay prompt tokens / stop quality | not enabled unless it beats no-compaction on both | 16 T1 + G2b-1 | READY |
| A1-09 | Tool-result ingress offload (ATLAS) | `tool_result_offload.py` | evaluation-only opt-in | T1: large output → placeholder + `history.read`; exit code preserved | error class + exit code survive offload | full ingress → prompt tokens, turns, oracle pass | non-inferior quality with lower prompt | 22 T1 + G3-2 | READY |
| A1-10 | Cache-aware metrics (ATLAS) | `metrics/collector.py`, `metrics/models.py` | all Atlas paths | T1: parse hit/miss; consistency `prompt == hit + miss` | mismatch is reported, never silently repaired | — | accounting only | 6 T1 + 9 usage T1 | READY |

**A1-01/A1-02 detail.** Problem: dependency outputs are injected in completion order, so equal logical
context renders differently. Hypothesis: an explicit block/ref model makes layout deterministic.
Workload: three-task DAG with two parallel roots. Secondary: block count, context chars. Overhead:
in-memory only (`<1 ms` per block). Negative: identical content under different owners must still
hash-equal only when the stable key matches — check that owner changes *do* change the hash. Deps:
none. Missing: no test for concurrent `put` (single-threaded by design). Env: D-ENV. n: D-REP.
Output: `layout_hash`, `block_count`.

**A1-03 detail.** Problem: a worker given "all context" pays for context it cannot use. Hypothesis:
task-scoped selection removes irrelevant blocks without losing needed ones. Workload: S/P/C families
with declared deps. Secondary: prompt tokens, correctness. Overhead: selection is O(blocks). Negative:
a task whose needed fact lives in a *non-dependency* block must be shown to fail under scoping — this
is the known boundary, and it is the reason full-context stays a baseline. Deps: A1-01/02. Missing:
no workload yet in which selection changes model input under the coding path (see
`runtime-integration-contract.md`). Evidence: selected refs vs dependency closure, token delta.

**A1-04 detail.** Problem: cache reuse is accidental when layout depends on completion order.
Hypothesis: a deterministic order raises prefix stability. Workload: same DAG run with inverted
completion order (T2). Primary metric: layout-hash equality; secondary: stable-prefix tokens.
Overhead: one sort per render (`µs`). Negative: a renderer that reorders frequently-changing blocks
would *break* prefix reuse — test that the stable key puts volatile kinds last. Evidence: hash pairs,
`pre/post_transform_layout_hash`.

**A1-05 detail.** Problem: repeated context inflates prompt. Hypothesis: exact-duplicate removal cuts
prompt tokens and *may* cut cache miss. Workload: 0/0.16/0.31/0.5 exact duplicate ratios × 3 sizes ×
2–3 topologies. Secondary: cache hit/miss, transform latency, content preservation. Overhead:
0.9–7.8 ms per request (measured). Negative: at small/medium scale the historical miss count *rose*;
the protocol must report that rung, not hide it. Deps: `contextpilot` extra in E2 only. Missing:
in-suite coverage is skipped (`.pth` conflict). Env: E2. n: T2 5 reps × 9 cells. Evidence: per-cell
prompt/miss/latency + a content-equality check on non-duplicate blocks.

**A1-06 detail.** Problem: assigning dependent tasks to different agents forces context transfer.
Hypothesis: locality-aware placement cuts handoff volume. Workload: chain length 2/3/5 (P2/P3/P5).
Secondary: switches, load balance, prompt tokens. Overhead: planner is pre-execution, `ms`.
Negative: a high load weight raises handoffs while improving balance (measured) — report the
trade-off, never a single "better". Evidence: handoff refs/chars, locality, distribution.

**A1-07 detail.** Problem: after a crash the derived store is gone. Hypothesis: deterministic rebuild
restores exactly the context the unfinished task saw. Workload: kill between waves, then resume.
Secondary: re-executed completed tasks (must be 0). Overhead: one rebuild pass. Negative: an in-flight
task that already executed is re-run from turn 0 (AnyCode granularity) — must be reported, not hidden.
Deps: A1-01/02, AnyCode checkpoint. Evidence: rebuild counts, layout equivalence, final-output
equality.

**A1-08/A1-09 detail.** Problem: long transcripts and huge tool outputs dominate prompt cost.
Hypothesis: archive + reference reduces prompt without losing recoverability. Workload: short/long/
multi-dependency/paged reads. Secondary: turns, oracle pass, `history.read` call count. Overhead: one
write per archived block. Negative: historical G2b-1 measured prompt **+9.3 %** and G3-2 measured a
neutral cost with more `max_turns` workers — both must be reproduced or contradicted, never omitted.
Missing: the model called `history.read` **0/12** times historically, so the benefit is unproven.
Evidence: prompt/miss deltas, read counts, oracle.

**A1-10 detail.** Problem: cost decisions need per-call cache accounting. Hypothesis: correct parsing
enables every other context claim. Workload: recorded provider payloads + live traces. Secondary:
estimated cost. Negative: when the provider omits the split, values stay `null` and cost is
`UNAVAILABLE` — never defaulted to zero. Evidence: JSONL records with `cache_metrics_complete`.

---

## 4. A2 — Tool Engineering

| ID | Module (origin) | Source: symbol | Entry / status | Functional (T) | Functional accept | Baseline → metric | Benefit accept | Cov. | Ready |
|---|---|---|---|---|---|---|---|---|---|
| A2-01 | Unified tool registry (ATLAS-ON-ANYCODE) | `tools/runtime.py::AtlasToolRuntime` | coding + evaluation | T1: one registry serves core+external+MCP; per-run isolation | two runs never share disclosed state | — | structural only | 12 T1 | READY |
| A2-02 | Tool catalog (ATLAS) | `tools/catalog.py::ToolCatalog` | coding + evaluation | T1: core vs searchable split; unknown tool rejected | external tools never native-visible | — | structural only | 12 T1 | READY |
| A2-03 | Keyword search (ATLAS) | `tools/search.py::KeywordToolSearchIndex` | default backend | T1: token match, Top-K, empty/ambiguous query | deterministic ranking; empty query ⇒ no crash | semantic backend → Recall@K | parity or better on the same catalog | 12 T1 | READY |
| A2-04 | Semantic tool search (ATLAS-ON-ANYCODE + 3P) | `tools/search.py::SemanticToolSearchIndex` | optional (`keyword` default) | T1/T2: real embeddings, 384-dim, Top-K, id mapping | returns real tool ids; no keyword fallback | keyword → Recall@1/@3, MRR | ≥ keyword on unseen queries | **0** | NEEDS_TEST |
| A2-05 | FAISS index (3P inside A2-04) | `tools/search.py` (`faiss.IndexFlatIP`) | inside semantic backend | T2: index build, cosine ranking, rebuild on registry change | index size == tool count; stable ids | exact numpy cosine → rank agreement | 100 % Top-K agreement vs brute force | **0** | NEEDS_TEST |
| A2-06 | Progressive disclosure (ATLAS) | `tools/runtime.py`, `tools/dispatch.py` | coding + evaluation default | T1: undisclosed ⇒ `tool_not_disclosed`; disclosed ⇒ dispatch; duplicate disclosure idempotent | no undisclosed execution possible | full exposure → native schema chars, prompt tokens | ≥40 % schema reduction with 0 disclosure violations | 16 T1 + M1 | READY |
| A2-07 | Stable native tool surface (ATLAS) | `tools/runtime.py` (surface hash) | coding + evaluation | T1: `tools[]` hash identical before/after search, across turns | hash never changes mid-run | — | structural only | M1 acceptance | READY |
| A2-08 | RunToolState (ATLAS) | `tools/state.py::RunToolState(Store)` | coding + evaluation | T1: durability, per-run isolation, survives task-level resume | disclosed set restored byte-exactly | — | structural only | 12 T1 + M1 recovery | READY |
| A2-09 | `dispatch_action` (ATLAS) | `tools/dispatch.py::AtlasDispatchExecutor` | coding + evaluation | T1: argument validation, unavailable tool ⇒ `tool_unavailable`, error mapping | never executes a non-disclosed id | direct executor call → same result class | identical result classification | 16 T1 | READY |
| A2-10 | MCP integration (ATLAS-ON-ANYCODE + 3P) | `tools/runtime.py::connect_mcp_servers` | optional config | T1: stdio server discovery, hidden by default, dispatch after disclosure | MCP tool never auto-enters native surface | — | functional only (no benefit claim) | 1 T1 + 4 T1 | READY |

**A2-01/02 detail.** Problem: an unbounded tool surface makes schema cost and selection quality worse.
Hypothesis: one registry + explicit catalog split makes disclosure the only path to a non-core tool.
Workload: 8 core + N external (24/64/128). Secondary: registry size, lookup latency. Overhead:
dict lookup, `µs`. Negative: a tool registered but not catalogued must be unreachable, not "just
hidden". Deps: AnyCode `ToolRegistry`. Missing: none known. Evidence: registry/catalog dumps.

**A2-03/04/05 detail.** Problem: with 64–128 tools, keyword matching misses paraphrases. Hypothesis:
embeddings raise Recall@1/@3 on unseen queries. Workload: catalog sizes 24/64/128 from a **new**
catalog (not the historical M1 24-tool set), plus ≥40 queries with 1–3 labelled relevant tools,
including ambiguous and no-answer queries. Secondary: cold index build ms, warm query ms, native
schema chars, initial prompt tokens, search calls, extra turns, cost, oracle outcome. Overhead:
model load 10.4 s cold (cached weights), query `ms`. Negative: semantically similar but mutually
exclusive tools must produce a *wrong* Top-1 — record it. Deps: `sentence-transformers`, `faiss-cpu`,
`all-MiniLM-L6-v2` (cached). Missing: **no test at all today**; this is the single largest coverage
gap (see `final-2-evaluation-design-report.md` §11). Env: E1 for T1/T2; E3 for the end-to-end arm.
Evidence: per-query ranks, MRR, latency table, per-cell prompt tokens.

**A2-06/07/08/09 detail.** Problem: the model must be able to use a tool it can name, without paying
for every schema. Hypothesis: disclosure converts a large surface into a small one at the cost of one
extra turn when a tool is needed. Workload: 15 task-shaped requests requiring 1–3 non-core tools.
Secondary: turns, tool calls, cost, task success. Overhead: one search call per discovery; schema
chars in the tool result instead of `tools[]`. Negative: a task whose tool is never searched must fail
with `tool_not_disclosed`, and that failure must be counted as a *task* failure, not a runtime bug.
Evidence: turn counts, schema sizes, disclosure log, oracle.

**A2-10 detail.** Problem: external capabilities should not bypass the disclosure gate. Hypothesis:
MCP tools behave exactly like any other searchable tool. Workload: one stdio server, 3 tools.
Secondary: discovery latency. Negative: a server that dies mid-run ⇒ `tool_unavailable`, no crash.
Deps: `mcp` extra (installed 1.30.0). Missing: no live-network MCP test in CI. Evidence: discovery
trace, dispatch result.

---

## 5. A3 — Multi-Agent Coordination

| ID | Module (origin) | Source: symbol | Entry / status | Functional (T) | Functional accept | Baseline → metric | Benefit accept | Cov. | Ready |
|---|---|---|---|---|---|---|---|---|---|
| A3-01 | Scoped worker (ATLAS) | `workers/factory.py::ScopedWorkerFactory.create` | coding + evaluation | T1: per-worker `AgentConfig`, isolated tools, no policy/HITL leak | worker cannot act outside its assignment | — | structural only | 12 T1 | READY |
| A3-02 | Trusted worker identity (ATLAS) | `workers/factory.py::_derive_context`, `ledger/identity.py` | coding + evaluation | T1: identity derived from parameters, not prompt; tamper test | prompt text cannot change `worker_id`/`task_id` | — | structural only | 12 T1 + R1-1 | READY |
| A3-03 | Worker profile / assignment (ATLAS) | `workers/models.py`, `workers/catalog.py` | coding + evaluation | T1: profile resolution, base tools, delegate tools rejected | unknown profile ⇒ error, not fallback | — | structural only | 12 T1 | READY |
| A3-04 | Workflow compiler (ATLAS) | `workflow.py::compile_workflow` | workflow path | T1: id↔title mapping, cycle/dup errors, one-sink rule | invalid graph rejected before any model call | — | structural only | 12 T1 | READY |
| A3-05 | DAG integration (ANYCODE) | `anycode.core.scheduler`, `orchestrator._build_waves` | workflow + evaluation multi | T1: wave order, dependency satisfaction | Atlas adds no scheduler | — | ANYCODE (no Atlas claim) | 10 T1 | READY |
| A3-06 | Dual handoff (ATLAS) | `handoff.py::HandoffRuntime` | evaluation opt-in (`legacy` default) | T1: projection, pagination, access denial, integrity | consumer sees only its declared deps | truncation prompt → prompt tokens, task quality | non-inferior quality with fewer prompt tokens | 16 T1 + G1 | READY |
| A3-07 | Shared run budget (ATLAS) | `budget.py::RunBudgetState(RunBudgetHook)` | evaluation opt-in (`guarded`) | T1: run-scoped accounting, denial ⇒ `budget_exceeded`, concurrency | 3 workers cannot exceed 1 run total | per-agent budget → total tokens, overrun count | 0 overruns, tokens ≤ limit | 15 T1 + G2a | READY |
| A3-08 | Selective delegation router (ATLAS) | `workers/routing.py::route_delegation` | evaluation-only, default `off` | T1: 6 gates, determinism, DIRECT/DELEGATE_P* semantics | same inputs ⇒ same decision digest | always-single/always-multi → oracle, cost, regret | see `router-evaluation-spec.md` §6 | 57 T1 + R0/R0.5 | READY |
| A3-09 | Worker ownership (ATLAS) | `ownership/policy.py`, `ownership/shell.py` | evaluation opt-in, default off | T1: declared-path enforcement, unclassifiable shell denial | denial is fail-closed, not silent | enforcement off → denials, oracle, false denials | net oracle gain with acceptable false-denial rate | 112 T1 + R1-1 | READY |
| A3-10 | Worker task contract (ATLAS, retained research) | `experiments/evaluation/stage_g3_worker_contracts.py` | evaluation-only | T1: contract delivery checks on worker output | missing field ⇒ finding, not crash | no contract → oracle, contract violations | measurable reduction in contract violations | 21 T1 + G3-3 | READY |

**A3-01/02/03 detail.** Problem: a delegated worker with prompt-only identity can be talked out of its
scope. Hypothesis: parameter-derived identity + tool allow-list bounds what a worker can do.
Workload: 3 profiles × 3 assignments, including a hostile prompt. Secondary: tool allow-list
violations. Negative: identity is enforced by the gate, not the kernel (documented limitation) — the
protocol must state that a shell command can still write outside its declaration unless ownership
enforcement is on. Deps: A4-01. Evidence: `workers.json`, gated denials.

**A3-04/05 detail.** Problem: AnyCode resolves dependencies by title, Atlas by stable id. Hypothesis:
the compiler prevents silent mis-binding. Workload: valid/invalid graphs. Secondary: none.
Negative: same agent in two tasks of one wave is rejected by design — record it as a constraint.
ANYCODE owns scheduling; Atlas claims only compilation.

**A3-06 detail.** Problem: passing a whole upstream output either truncates information or inflates
context. Hypothesis: a reference + paged read preserves access at lower prompt cost. Workload: short,
long, multi-dependency, paged-read-needed. Secondary: `handoff.read` calls, oracle. Negative: a
consumer that never reads the reference loses the information — count it. Evidence: handoff metrics,
prompt tokens, oracle.

**A3-07 detail.** Problem: per-agent budgets multiply the allowance by worker count. Hypothesis: one
run-scoped budget bounds total spend. Workload: 1 vs 3 workers on the same task, T3. Secondary:
stop reason, quality. Negative: a budget stop can fail a run whose artifacts the oracle would accept —
report both facts. Evidence: reservations, denials, tokens, cost.

**A3-08 detail.** Router is an admission policy, not a planner: it can only accept or reject the plan
it is handed. Full protocol in `router-evaluation-spec.md`.

**A3-09 detail.** Problem: workers can write each other's files. Hypothesis: declared-path enforcement
prevents cross-worker writes. Historical result: **negative** (1/16 vs 7/16, 19 denials including
`python3 -m pytest`). Workload: shared-write and read-only tasks. Secondary: false denials, cost.
Negative: must be reported as NEGATIVE unless the new workload reverses it; no threshold tuning to
rescue it.

**A3-10 detail.** Problem: a worker can return a plausible answer that omits required deliverable
fields. Hypothesis: contract checks make omission visible. Workload: tasks with 1–3 required fields.
Secondary: extra turns. Negative: reports only; never blocks. Evidence: violation list.

---

## 6. A4 — Reliable Execution

| ID | Module (origin) | Source: symbol | Entry / status | Functional (T) | Functional accept | Baseline → metric | Benefit accept | Cov. | Ready |
|---|---|---|---|---|---|---|---|---|---|
| A4-01 | Action safety gate (ATLAS) | `actions/gate.py::ActionSafetyGate` | coding + evaluation | T1: 4 outcomes, order of checks, no bypass | no path executes a denied action | gate off → unauthorised side effects, denials | 0 unauthorised effects with ≤X false denials | 14 T1 + CP | READY |
| A4-02 | Action policy (ATLAS) | `actions/policy.py::ActionPolicy/ActionRule` | coding + evaluation | T1: rule matching, load errors, default deny | unknown tool ⇒ deny | — | structural only | 14 T1 | READY |
| A4-03 | Argument / binding digest (ATLAS) | `actions/binding.py` | coding + evaluation | T1: canonical JSON, input digest, tool-implementation digest | whitespace/key-order changes cannot alias | — | structural only | CP 14 T1 | READY |
| A4-04 | Deny / reviewer / HITL precedence (ATLAS) | `actions/gate.py::_evaluate` | coding + evaluation | T1: hard precheck > external deny > policy > reviewer > approval | no lower layer can override a hard deny | — | structural only | CP 14 T1 | READY |
| A4-05 | Durable action ledger (ATLAS) | `ledger/store.py::SQLiteActionLedgerStore` | coding + evaluation | T1: state machine, run/task/worker keys, cross-process read | every authorized action has ≥1 terminal or `UNCERTAIN` row | no ledger → duplicate effects, unknown rows | 0 duplicates; 100 % of executed actions recorded | 10 T1 + 9 T1 | READY |
| A4-06 | Idempotency integration (ATLAS-ON-ANYCODE) | `tools/runtime.py` (`SQLiteToolIdempotencyStore`) | coding + evaluation | T1: claim/complete/replay/conflict, process restart | replay never re-executes a completed effect | no store → duplicate effects | 0 duplicate effects across restart | 8 T1 | READY |
| A4-07 | UNCERTAIN classification (ATLAS) | `ledger/recovery.py::RecoveryClassifier` | coding + evaluation | T1: `IN_PROGRESS` + crash ⇒ `UNCERTAIN`, never auto-retry | aging a row never makes it retryable | — | 0 blind replays | 9 T1 | READY |
| A4-08 | SQLite cross-process durability (3P/stdlib + ATLAS schema) | `ledger/store.py`, `idempotency` | coding + evaluation | T1: two OS processes, WAL behaviour, barrier | second process sees committed rows | in-memory → survived rows after kill | 100 % of committed rows survive | 8 T1 | READY |
| A4-09 | Sandbox binding (ATLAS) | `sandbox/binding.py::ProjectSandboxManager`, `sandbox/store.py` | coding + evaluation | T1: state machine, conflict, one binding per task | no two live sandboxes for one task | — | structural only | 17 T1 | READY |
| A4-10 | Workspace tools (ATLAS) | `sandbox/tools.py::SandboxWorkspaceTools` | coding + evaluation | T1: path fence, read/write counts, same-handle invariant | all project tools use one handle | host execution → sandbox-only effects | 0 host mutations | 37 T1 | READY |
| A4-11 | Local ownership (ATLAS) | `sandbox/ownership.py::WorkloadOwnershipLock` | coding + evaluation | T1: concurrent owner denied, release on close | second owner cannot proceed | — | structural only | 25 T1 | READY |
| A4-12 | Verified reattachment (ATLAS) | `sandbox/reattachment.py` | operator path | T1: continuity digest, config/created-at/metadata mismatch classes | any mismatch refuses reattach | — | 0 silent reattachments | 13 T1 + 17 T1 | READY |
| A4-13 | Whole-response preflight barrier (ATLAS-ON-ANYCODE) | `suspension/batch.py`, `tools/conversation.py::_execute_tool_blocks` | coding path | T1: batch inspected before first execution; partial batch impossible | no tool in a suspended response executes | per-call approval → pre-execution suspensions | barrier holds for every batch | 23 T1 + IV | READY |
| A4-14 | Durable suspension (ATLAS) | `suspension/store.py`, `suspension/coordinator.py` | coding path | T1: create/list/settle, version, active-for-run | restart keeps suspension | in-memory → survives process exit | 100 % survive | 23 T1 | READY |
| A4-15 | Persistent approval (ATLAS) | `approvals/store.py`, `approvals/coordinator.py`, `approvals/resume.py` | coding path | T1: resolve once, stale version refused, revalidation on resume | no stale approval reused | — | 0 stale reuses | 10 T1 + IV | READY |
| A4-16 | TrustedRecoverySession (ATLAS) | `trusted_recovery.py` | operator harness | T1: identity validation, cleanup result, refusal paths | refuses on identity mismatch | — | 0 unverified recoveries | 17 T1 + Stage D | READY |
| A4-17 | Recovery cleanup (ATLAS) | `trusted_recovery.py::RecoveryCleanupResult` | operator harness | T1: destroy on close, uncertain disposal refused | cleanup failure is surfaced | — | 0 silent leaks in normal close | 12 T1 | READY |
| A4-18 | Tool error semantics (ATLAS-ON-ANYCODE) | `tools/conversation.py`, `ledger/bridge.py::_classify_result` | coding + evaluation | T1: non-zero exit stays a known error; `retry_safe` tri-state | completed error ≠ crash | legacy mapping → premature stops | 0 premature stops attributable to error semantics | 15 T1 + G3-1 | READY |

**A4-01…04 detail.** Problem: nothing decides whether *this* call from *this* worker may run.
Hypothesis: one decision point bound to a digest prevents both unauthorised execution and ambiguous
audit. Workload: 24 scripted requests per run — allowed, denied, require-human, malformed, spoofed
identity, replay of an old approval. Secondary: decision latency (`µs`), false denials. Negative: a
denial the model interprets as a tool bug can cause a retry loop — count retries after denial. Deps:
A4-03. Missing: no live-model false-denial measurement yet (historical: 19 denials in the ownership
arm). Evidence: decision table, digest, ledger rows.

**A4-05…08 detail.** Problem: an authorised action can die mid-flight, leaving an unknown effect.
Hypothesis: write-before-execute + a terminal classification makes the unknown state enumerable and
non-retryable. Workload: crash windows before claim / after claim / after execute / before complete /
before settlement; plus a second OS process reading the DB. Secondary: recovery latency, rows repaired.
Negative: `UNCERTAIN` is *not* resolution — the operator still decides; the protocol must not present
"safe stop" as task success. Deps: `anycode-py[persistence]`. Evidence: ledger dumps, reconciliation.

**A4-09…12 detail.** Problem: remote workspace mutation is invisible and binding state can drift.
Hypothesis: one binding + continuity digests make "same sandbox" checkable. Workload: bind, execute,
export, close; then reattach with matching and mismatching config/created-at/metadata. Secondary:
provision latency, reattach latency. Negative: reattachment is explicit only; there is no automatic
recovery — record it. Evidence: binding rows, digests, refusal classes.

**A4-13…15 detail.** Problem: a model response can mix benign and destructive calls. Hypothesis: a
batch barrier turns "approval" into a pre-execution property. Workload: response with 3 calls, one
requiring approval; approve/reject/expire; change an argument between approval and resume. Secondary:
suspension count, resume revalidations. Negative: the barrier is **not** transactional atomicity — a
crash after release leaves later calls unexecuted with no rollback. Deps: A4-05. Evidence:
suspension rows, approval versions, resume outcome.

**A4-16/17 detail.** Problem: a resumed session must not trust disk state blindly. Hypothesis:
identity + digest validation before any reattach. Workload: 11 OS-process scenarios + mismatches.
Secondary: cleanup timing. Negative: unresolved ownership must refuse to dispose (fail-closed) and can
leave a live sandbox for manual reaping. Evidence: recovery reports.

**A4-18 detail.** Problem: a completed non-zero exit looked like a crash and stopped the worker.
Hypothesis: classifying exit codes as known tool errors keeps the worker going. Workload: 2 tasks ×
legacy/corrected. Secondary: premature stops, oracle. Negative: a genuinely fatal command must still
stop the worker. Evidence: error classes, stop reasons.

---

## 7. A5 — Observability and Evaluation

| ID | Module (origin) | Source: symbol | Entry / status | Functional (T) | Functional accept | Baseline → metric | Benefit accept | Cov. | Ready |
|---|---|---|---|---|---|---|---|---|---|
| A5-01 | Trajectory recorder (ATLAS) | `trajectory/recorder.py` | coding + evaluation always; workflow opt-in | T1: append-only, redaction, id derivation | events are immutable and ordered | `result.json` only → observed action count | ≥ result.json count on every run | 11 T1 + 9 T1 | READY |
| A5-02 | Action proposal / observation (ATLAS) | `trajectory/models.py` | as above | T1: pairing proposed→outcome, worker terminal for non-returning workers | no orphan proposal without terminal | — | 100 % pairing | 12 T1 + R1-1 | READY |
| A5-03 | Input digest (ATLAS) | `actions/binding.py::input_digest` | coding + evaluation | T1: canonical argument digest, coverage metric | digest present on every executed action | — | coverage = 1.00 | 12 T1 + R1-1 | READY |
| A5-04 | Execution integrity (ATLAS) | `trajectory/recorder.py`, `metrics/fingerprint.py` | evaluation opt-in (`execution_integrity_mode`) | T1: private payload corpus, worker stop events | payload corpus is not the public trajectory | — | recovers `result.json`-missing actions | R1-1 T1 12 | READY |
| A5-05 | Trace reconciliation (ATLAS) | `trajectory/reconcile.py` | evaluation | T1: ledger↔trajectory consistency, gap/conflict classes | a conflict is reported, never resolved silently | — | 0 unexplained gaps | 12 T1 | READY |
| A5-06 | Metrics collector (ATLAS) | `metrics/collector.py` | all Atlas paths | T1: aggregation, cost model, missing-value handling | `null` propagates; never coerced to 0 | — | accounting only | 6 T1 | READY |
| A5-07 | Failure attribution (ATLAS) | `app/coding.py` (`MODEL_UNFINISHED`), `experiments/evaluation/stage_r1_0_analysis.py` | app coding + analysis | T1: unclassified failure impossible in app coding; analysis re-derives classes | `status=failed` ⇒ non-null class | null class → attributed rate | 100 % of failures classified | 3 T1 + R1-0 | READY |
| A5-08 | Deliverable verification (ATLAS) | `verification/engine.py` | evaluation observe-only | T1: PASS/FAIL/UNKNOWN per check family; never blocks | UNKNOWN ≠ PASS | no verification → findings | 0 false positives on the new workload | 54 T1 + R1-2A | READY |
| A5-09 | Independent oracle sandbox (ATLAS) | `experiments/evaluation/sandboxed_oracle.py` | evaluation default (`sandbox`) | T1: hidden tests in a disposable container; score parity vs host | identical verdicts to host mode | host grading → parity, timing | 100 % verdict parity | 40 T1 + R1-2H | READY |
| A5-10 | Inspect AI adapter (ATLAS-ON-3P) | `experiments/evaluation/inspect_adapter.py` | evaluation extra | T1: dataset→solver→scorer mapping, error mapping | a crashed sample scores 0, never PASS | native runner → same verdict on the same task | 1 task, identical verdict | **0** | NEEDS_TEST |
| A5-11 | Evaluation runner (ATLAS) | `experiments/evaluation/runner.py` | evaluation | T1: run identity, config digest, settings serialization, budget/oracle/verification wiring | every run is reproducible from its `identity` | — | structural only | 7 T1 | READY |

**A5-01…05 detail.** Problem: the upstream task result is an incomplete record (365 vs 591 actions
measured). Hypothesis: an append-only, id-addressed trajectory makes failure attribution possible.
Workload: 30 archived runs re-read (no model) + new runs. Secondary: bytes per event, read latency.
Negative: evidence writes are best-effort, not fsync'd; a hard kill can lose the last event. Deps:
AnyCode run store. Evidence: coverage table, recovered-action count.

**A5-06 detail.** Problem: cost and cache claims need one accounting path. Hypothesis: a single
collector prevents per-experiment token definitions. Secondary: cost model drift. Negative: provider
may omit the cache split ⇒ `UNAVAILABLE`; the protocol forbids substituting 0.

**A5-07 detail.** Problem: `status=failed` with no class makes model failure indistinguishable from a
runtime break. Hypothesis: every non-success gets a class. Workload: turn-limit, tool error, sandbox
loss, cleanup failure, budget stop. Negative: the current precedence can mask
`SANDBOX_PROVIDER_FAILURE` behind `MODEL_UNFINISHED` (documented in Final-1.6 §3 caveat A) — the
protocol must check the class *and* `sandbox_destroyed` separately until that is fixed.

**A5-08 detail.** Problem: "the agent said it finished" is not evidence. Hypothesis: deterministic
contract checks detect missing deliverables early at zero token cost. Workload: ≥20 new exports
including deliberate violations and a clean control. Secondary: verification latency, UNKNOWN rate.
Negative: recall is thin (23 TP / 23 FN historically) — a PASS must never be read as "fully checked".
Evidence: findings, TP/FP/FN counts on the new sample.

**A5-09 detail.** Problem: grading next to the agent lets benchmark code touch host paths. Hypothesis:
isolated grading reproduces the same verdict. Workload: the same workspace graded in host mode and in
sandbox mode. Secondary: per-stage timing. Negative: isolation is a process/network boundary, not a
security proof. Evidence: verdict parity, timing, digests.

**A5-10 detail.** Problem: the external harness path has never been tested, so an Inspect run could
silently mis-score. Hypothesis: adapter mapping is faithful. Workload: one deterministic task with a
fake adapter, one real-model task (E3). Secondary: wall time. Negative: exceptions must map to a
non-PASS score. Deps: `inspect-ai` 0.3.289. Missing: **no test**. Evidence: Inspect log + Atlas
`result.json` for the same sample.

**A5-11 detail.** Problem: an evaluation result is worthless if it cannot be tied to code+config.
Hypothesis: identity/digest capture makes runs reproducible. Workload: re-run the same config twice.
Secondary: `git_dirty`. Negative: a dirty tree invalidates comparability — recorded, not blocked.
Evidence: `identity` block, settings digest.

---

## 8. Deliberate non-coverage

| Item | Why it is not measured |
|---|---|
| Physical KV-cache placement / eviction | out of V1 scope (no serving engine) |
| Cross-host recovery and distributed leases | out of scope; SQLite is local by design |
| CPU / memory quota enforcement inside the sandbox | not pinned by the provider integration |
| OS-level per-worker isolation | identity is enforced by the gate, not the kernel |
| Ordinary coding crash recovery | no checkpoint exists in any coding composition; D4 declares it unsupported |
| Automatic Planner / task decomposition | router is an admission policy; D6 forbids a planner |
| Commerce / multi-industry business adapters | D3 closes the domain to software engineering |

## 9. What this matrix still needs before execution

1. Semantic/FAISS/MCP-live/Inspect coverage (A2-04, A2-05, A5-10).
2. An isolated environment for A1-05 (ContextPilot) — `environment-protocol.md` E2.
3. The integration contract for A1-03/A1-04 to appear in the coding path at all
   (`runtime-integration-contract.md`).
4. A frozen task set and candidate DAGs (`holdout-design.md`).
5. Owner approval of every source change listed in `final-2-evaluation-design-report.md` §11.

---

## 10. Final-3 acceptance results (2026-11-13)

Runtime `93cbd98` (frozen); regression 925 passed / 6 skipped / 1 deselected.
Full rows: `experiments/results/final3/module_acceptance.json` (+ `.csv`); narrative:
`docs/final-3-module-acceptance-report.md`.

**Functional: 59 / 59 PASS.**

**Benefit POSITIVE (20):** A1-03 (irrelevant blocks 2 → 0), A1-04 (layout equality 0.0 → 1.0),
A1-06 (handoff refs 3 → 0), A2-04 (Recall@1 0.80 → 0.90), A2-06 (schema chars 8 796 → 4 207),
A4-01, A4-04, A4-05, A4-06, A4-07, A4-08, A4-10, A4-13, A4-14, A4-15, A4-17, A5-01, A5-02, A5-03,
A5-07.

**CONDITIONAL (3):** A3-07, A4-18, A5-08.
**NEGATIVE (1):** A3-09 (1/16 vs 7/16, historical; not re-run live).
**NEUTRAL (1):** A1-05 (real ContextPilot removed 0 blocks at 0/25/50 % duplicates in a new workload).
**INSUFFICIENT_EVIDENCE (8):** A1-08, A1-09, A2-03, A3-06, A3-10, A4-12, A4-16, A5-09.
**NOT_APPLICABLE (26):** A1-01, A1-02, A1-07, A1-10, A2-01, A2-02, A2-05, A2-07, A2-08, A2-09,
A2-10, A3-01, A3-02, A3-03, A3-04, A3-05, A3-08, A4-02, A4-03, A4-09, A4-11, A5-04, A5-05, A5-06,
A5-10, A5-11.

Live-model benefit studies (T3, X0/X1, native/Atlas acceptance) were **not executed**: no provider
credential exists in the workspace. Native environment parity is **BLOCKED** (tool capability maps
1:1; execution location and shell differ), so no cost or latency comparison is drawn.
---

## 11. Final-4 supplement (2026-11-13)

Frozen runtime `93cbd98`; evaluation commit `28428e8`. Full rows:
`experiments/results/final4/module_supplement.json`; narrative:
`docs/final-4-module-evidence-supplement.md`.

Evidence levels were added to every row (`functional_evidence_level`, `benefit_evidence_level`,
`evidence_epoch`, `baseline_type`, plus separate current/historical/live result columns).

Changes to this matrix:

- **A1-05 ContextPilot dedup: NEUTRAL → POSITIVE (conditional).** D0–D3 show the real transform
  reduces characters by 31.84 % on a multi-line 12-block payload at 50 % exact duplicates; the Final-3
  zero was caused by single-line blocks the chunker cannot split. Condition: multi-line, chunkable
  blocks. Single-line blocks still never dedup.
- **A1-03 Selector / A1-04 Renderer:** re-measured on five topologies — 0 irrelevant blocks injected,
  stable layout 5/5 vs naive 3/5, direct-dependency-only coverage 7/9 (transitive deps are not
  included by design).
- **A2-04 / A2-05:** scale study at 64 and 128 tools with 32 frozen positive queries + 6 negatives:
  semantic Recall@1 0.81 → 0.91, MRR 0.86 → 0.93; unknown-tool rejection does **not** exist.
- **A1-08, A1-09, A3-06, A3-10, A4-12, A4-16, A5-09:** remain `INSUFFICIENT_EVIDENCE`; no new workload
  was executed this stage.
- **All live-dependent claims:** `BLOCKED` (no provider credential). No row is `E4_LIVE_INTEGRATED`.

Functional status is unchanged at 59/59 PASS; no mechanism was removed or re-scoped.