# AnyCode 0.9.0 vs AtlasRuntime — Contribution Delta Audit

This document answers one question without marketing language:

> What does the upstream runtime already do, and what did this project actually add?

It is the provenance basis for every claim in `README.md`, `docs/architecture.md` and
`docs/evaluation-summary.md`. Anything that AnyCode 0.9.0 already provides is attributed to
AnyCode, even when AtlasRuntime wraps or re-composes it.

---

## 0. Audit basis

| Item | Value |
|---|---|
| Upstream package | `anycode-py==0.9.0` (`pip list` / `anycode_py-0.9.0.dist-info`) |
| Upstream source used for the audit | `D:\Work\AtlasOS\.venv\Lib\site-packages\anycode\` (installed wheel, 230 modules) |
| Upstream license | MIT (`anycode_py-0.9.0.dist-info/METADATA`) |
| AtlasRuntime base branch | `fix/r1-2h-oracle-sandbox-isolation` (`facca98`) |
| Declared dependency | `pyproject.toml` → `anycode-py[openai]==0.9.0` |

No copy of the upstream source tree is vendored in this repository. `src/atlasos/` contains only
call sites, adapters, subclasses and independent components.

---

## 1. What AnyCode 0.9.0 already provides

Evidence column points at the installed module that implements the behaviour. "Depth" records how
much of the mechanism actually exists, not what the README advertises.

| Capability | Upstream implementation | Depth |
|---|---|---|
| `Agent` | `core/agent.py::Agent` (`run`/`prompt`/`stream`/`reset`) | In-memory conversation, no persistence |
| `AgentRunner` | `core/runner.py::AgentRunner` (turn loop, `LifecycleEmitter`, `LoopDetector`, stop reasons) | Full, incl. `resume_from: TurnCheckpoint` |
| `AgentPool` | `core/pool.py::AgentPool` | Concurrency cap + shutdown; no health/restart |
| LLM adapter | `providers/adapter.py::create_adapter`, `openai.py`, `fake.py`, `resilience.py` | Provider-agnostic; **parses `prompt_tokens_details.cached_tokens` into `cache_read_input_tokens`** |
| `ToolRegistry` | `tools/registry.py::ToolRegistry` | Flat dict, `side_effecting`, `idempotency_key_field`; no tags/permissions/search |
| `ToolExecutor` | `tools/executor.py::ToolExecutor` | `retry_safe` tri-state, `check_tool_access`, HITL hook, batch via `gather`; **no tool timeout** |
| Task queue | `tasks/queue.py::TaskQueue`, `tasks/task.py` | In-memory DAG + Kahn topological order + cycle detection + snapshot serialize |
| DAG scheduling | `core/scheduler.py::Scheduler` (4 assignment policies), `core/orchestrator.py::_build_waves` (wavefront) | Assignment + wave execution are real |
| `ExecutionContext` | `identity/context.py::ExecutionContext` (frozen, credential-shape rejection) | Identity model only; **not consulted by `check_tool_access`** |
| Checkpoint | `checkpoint/manager.py`, `checkpoint/store.py` (filesystem + SQLite), `backends/sqlite.py` | Full-rewrite snapshots, spec-hash gate |
| Resume | `runner.resume_from`, `orchestrator._resume_from_checkpoint`, `core/session_chain.py` | Three separate mechanisms |
| Recovery sweeping | `schedule/scheduler.py::sweep_once` (`mark_interrupted_runs`, `due_wakes`, sweep lock) | Heartbeat + lock; resume callback is a type alias |
| Idempotency | `tools/idempotency.py` (`execute`/`replay`/`in_progress`/`conflict`, in-memory + SQLite) | Strong; **no TTL for in-flight rows** |
| Tool security | `security/policy.py::check_tool_access`, `resolve_tool_path`, `validate_shell_command` | Allowlist + path fence; three unconnected policy layers |
| Sandbox | `sandbox/protocol.py::SandboxProvider`, `factory.py` (daytona/e2b/modal/runloop/vercel/langsmith) | create→destroy only; **no reattach, lease or ownership** |
| Evaluation | `eval/suite.py`, `eval/scorer.py` (substring criteria only), `harness/*` | Scaffolding, no benchmark dataset |
| Verification | `verification/gate.py::QualityGate`, sensors for ruff/pyright/pytest | Runs in the runner loop; Python-ecosystem only |
| Hooks | `guardrails/hooks.py::HookRunner` | Only `before_turn` / `after_turn`, no tool-level hooks |
| Telemetry | `telemetry/*` (`EventEmitter`/`MetricsCollector` **default disabled**), `cost/tracker.py` (cache-aware pricing) | No exporter for events/metrics |
| Handoff | `handoff/executor.py::HandoffExecutor` | Same-process, text prompt, **drops `ExecutionContext`** |
| HITL | `hitl/approval.py::ApprovalManager`, 3 gates | **In-memory only** |
| Budget | `guardrails/budget.py::BudgetTracker` | **Per-`AgentRunner`** (`max_tokens_per_agent`); no shared pool |
| Run store | `runstore/store.py::FilesystemRunStore` (15 transcript kinds, redaction, path-safety) | Real; `_next_seq` is per-process |
| Plugins / MCP | `plugins/*` (entry points), `mcp/*` (stdio/http, schema bridge) | Register-only; **no reconnect, no unload** |
| Routing | `routing/*` | Model routing only, not task→agent |

### Upstream gaps that mattered for this project

1. No shared budget across workers (one `BudgetTracker` per runner).
2. No tool-call timeout.
3. No sandbox reattachment, lease, or orphan reclamation.
4. No durable approval state.
5. No expiry for in-flight idempotency rows.
6. No pre/post tool-call hook, therefore no seam where a whole model response can be judged
   before any of its tools execute.
7. No single authorization decision point that sees tool + identity + argument binding.
8. No durable record pairing a proposed action with its observed outcome.
9. No reattach/recovery composition for a suspended session.
10. No benchmark oracle, and no isolation of the oracle from the worker sandbox.

---

## 2. Contribution table

Decision vocabulary: `KEEP_CORE`, `KEEP_OPTIONAL`, `KEEP_EVALUATION`, `ARCHIVE_EXPERIMENT`,
`REMOVE_REDUNDANT`, `REMOVE_OBSOLETE`, `NEEDS_MORE_EVIDENCE`.

| # | Engineering problem | AnyCode 0.9.0 native | AtlasRuntime delta | Implementation path | Experiment evidence | Decision |
|---|---|---|---|---|---|---|
| 1 | Model sees every tool schema; tool surface grows with MCP | `ToolRegistry.to_llm_tools()` returns everything; no search or disclosure | Progressive surface: model sees `tool.search` + `dispatch_action`; a tool must be disclosed before it is dispatchable | `tools/catalog.py`, `tools/search.py`, `tools/state.py`, `tools/runtime.py::AtlasToolRuntime.dispatch`, `integrations/anycode_engine.py::build_agent` | eval-final: tool-discovery family 15/15, Recall@1 = 1.0; **ablation inconclusive** (18/24 vs 7/8) | `KEEP_CORE` (mechanism) / benefit `NEEDS_MORE_EVIDENCE` |
| 2 | No single authorization decision point | `check_tool_access` (allowlist) + HITL hook + 3 disconnected policy layers | `ALLOW / DENY / REQUIRE_HUMAN` ladder over policy rules, reviewer, external policy, executable binding digest | `actions/gate.py::ActionSafetyGate`, `actions/policy.py`, `actions/binding.py`, `actions/catalog.py` | control-plane: 24 requests → 15 ALLOW / 1 DENY / 8 REQUIRE_HUMAN, 0 bypass, 0 hard-deny override | `KEEP_CORE` |
| 3 | Idempotency exists, but no durable record of what was authorized and what happened | `ToolIdempotencyStore.claim/complete`, `retry_safe` tri-state | SQLite action ledger keyed by (run, task, worker, tool, input digest); `DurableActionBridge` couples gate decision → execution → outcome; `UNCERTAIN` classification for unknown side effects | `ledger/store.py::SQLiteActionLedgerStore`, `ledger/bridge.py::DurableActionBridge`, `ledger/recovery.py::RecoveryClassifier`, `ledger/identity.py` | G3-1: 3 `UNCERTAIN/EXECUTED_ERROR` records, `retry_safe=true` records 0, replays 0; v2-integrated: executed 7 / replayed 1 / uncertain 0 | `KEEP_CORE` |
| 4 | Workers are ad-hoc `AgentConfig` dicts | `AgentConfig`, `ExecutionContext` | Scoped worker: profile + assignment → frozen worker instance with derived trusted `ExecutionContext`, durable worker registry, rendered scoped context | `workers/factory.py::ScopedWorkerFactory`, `workers/models.py`, `workers/store.py`, `workers/renderer.py` | R1-0: fixed per-worker overhead 1,988–2,085 tokens = 4.5–5.2 % of spend (so the scoped prefix is *not* the cost driver) | `KEEP_CORE` |
| 5 | Sandbox has no lifecycle beyond create/destroy | `SandboxProvider` protocol | Binding state machine (`PENDING→READY→LOST→DESTROYED`), OS-level ownership lock, capability/health gating, verified reattachment by digest, workspace tools bound to one handle | `sandbox/binding.py`, `sandbox/store.py`, `sandbox/ownership.py`, `sandbox/reattachment.py`, `sandbox/tools.py`, `sandbox/validation.py` | v2-real-sandbox: 2/3 real UAT; eval-final: host-isolation violations 0/90, forbidden workspace changes 0/90 | `KEEP_CORE` |
| 6 | Approval state dies with the process; a model response executes partially | In-memory `ApprovalManager`; batch execution via `gather` with no barrier | Durable suspension: whole-response preflight barrier, SQLite suspension + approval stores, revalidation on resume with a fresh binding digest | `tools/conversation.py::AtlasAgentRunner._execute_tool_blocks`, `suspension/*`, `approvals/*`, `approvals/resume.py` | v2-integrated S1–S8 (scripted): 6 suspensions, 7 approvals, 13 resume revalidations, 0 stale approvals accepted; OS-process HITL resume tests | `KEEP_OPTIONAL` (opt-in, requires M3+M4) |
| 7 | Execution evidence cannot be reconstructed after the fact | `FilesystemRunStore` transcript (15 kinds), no action/outcome pairing | Trajectory recorder: `action_proposed` / `tool_observed` pairs with input digests and sizes, worker lifecycle terminals, reconciler, integrity `record` mode | `trajectory/recorder.py`, `trajectory/models.py`, `trajectory/reconcile.py`, `trajectory/payloads.py`, `trajectory/context.py` | R1-0: `result.json` held 365 of 591 recorded actions (**38 % missing, concentrated in failed workers**); R1-1: `input_digest_coverage = 1.00` over 32 runs, 111 recovered actions | `KEEP_CORE` |
| 8 | Budget is per agent, so N workers each get the full allowance | `BudgetTracker` instance per `AgentRunner` | Shared run budget with pre-request admission, learned token-ratio estimate, rejection recorded as a real failure instead of an empty success | `budget.py::RunBudgetState`/`RunBudgetHook`, `tools/conversation.py::AtlasToolAgent._apply_budget_outcome` | G2a: 0/4 overruns (vs 2/4), 95/95/0 request accounting, overrun 0; **no success-rate benefit** (both arms 0/4) | `KEEP_CORE` (capability) / benefit `NEEDS_MORE_EVIDENCE` |
| 9 | Handoff is a trimmed text prompt that loses identity | `HandoffExecutor` (same process, truncation) | Dual-channel handoff: typed result channel with offload to a store, `handoff.read` paging, trusted consumer task identity | `handoff.py::HandoffRuntime` | G1: mechanism correct; success 2/6 vs 1/6; only 1 offload sample; 2,366 chars were silently dropped in legacy | `KEEP_OPTIONAL` (default `legacy`) |
| 10 | Task→agent assignment is fixed or round-robin | `core/scheduler.py` (4 policies) | Delegation router: DIRECT vs DELEGATE_PARALLEL with explicit rejection reasons and offline audit | `workers/routing.py` | R0: 12/12 direct, 0 live parallel admissions; R0.5: cost **+40 % / +269 %**, latency +27 % / +59 %, correctness below single worker | `KEEP_OPTIONAL` (default off) — `NEEDS_MORE_EVIDENCE` for any default |
| 11 | Quality gate checks Python tooling, not the deliverable | `QualityGate` + ruff/pyright/pytest sensors | Deterministic deliverable verification: structural, dynamic contract, file/text checks, `UNKNOWN` instead of guessing | `verification/engine.py`, `verification/code_checks.py`, `verification/dynamic.py`, `verification/structured_checks.py` | R1-2A: 66 verified deliverables, **0 false positives**; 8.1 ms static / 4.99 s dynamic; 0 model tokens | `KEEP_EVALUATION` |
| 12 | Benchmark oracle runs on the host next to the agent | none | Hidden oracle executed in a disposable, network-disabled sandbox; no host code execution during grading | `experiments/evaluation/sandboxed_oracle.py` | R1-2H: 28/28 score agreement, 0 dependency/environment differences, 5.9–6.4 s per grading, 0 model tokens | `KEEP_EVALUATION` |
| 13 | Provider prompt cache is invisible to the runtime | cache token fields parsed upstream | Selector/renderer split, stable prefix layout, cache-hit/miss accounting per turn, cost estimate | `context/selectors.py`, `context/renderer.py`, `context/store.py`, `metrics/collector.py` | E2: no token/cost/cache win; Stage 6: prompt −44 % max but warm cache-miss reduction **mixed (+11 % at large scale)** | `KEEP_OPTIONAL` — cache-aware rendering is `NEEDS_MORE_EVIDENCE` |
| 14 | Placement is not modelled at all | — | Task placement policies + planner (static / capability / context locality) | `placement/*`, `runtime.py::_place` | E2 P0/P1: prompt identical, agent switches 48→24, locality 0.667 — **no token claim** | `ARCHIVE_EXPERIMENT` (kept only as `policy="bypass"` fallback; no default path) |
| 15 | Recovery composition does not exist | `sweep_once` + resume callbacks | Trusted recovery composition that rebuilds the exact worker/tool runtime from durable state and delegates continuation | `trusted_recovery.py`, `approvals/resume.py` | Stage D: 11 real OS-process scenarios + 8 deterministic; CLI refuses to reattach without M5/M6 authority | `KEEP_OPTIONAL` |
| 16 | Nothing stops a worker from writing another worker's files | `resolve_tool_path` (workspace-relative only) | Path ownership manifest + enforcement gate (declared paths, foreign-path denial) | `ownership/*` | R1-1: enforce arm **1/16 vs 7/16** oracle; `python3 -m pytest` classified unclassifiable and denied | `KEEP_OPTIONAL`, **default off** — evidence is negative |
| 17 | Long histories are re-sent verbatim | Upstream `ContextManager` compaction | Rolling history compaction + tool-result ingress offload with `history.read` recovery | `history_compaction.py`, `tool_result_offload.py` | G2b-1: prompt +9.3 %, cache miss +88 %, cost +43 %; G3-2: prompt +3.7 %, cost −0.7 % (noise), `max_turns` workers 0→4 | `KEEP_OPTIONAL`, **default off** |
| 18 | Worker task text is free-form | — | Structured worker task contract (objective/interface/acceptance) | `experiments/evaluation/stage_g3_worker_contracts.py` | G3-3: oracle 2/6 vs 2/6, prompt +15 %, turns +27 %, discipline mixed | `KEEP_EVALUATION` (analysis mode only) |
| 19 | Evaluation harness | `eval/*` substring scorer | Inspect AI adapter, benchmark task model, matched AnyCode baseline, oracle, round orchestration, analysis | `experiments/evaluation/*` | eval-final: 90 runs, success 84/90, baseline 20/25 vs 21/25 | `KEEP_EVALUATION` |
| 20 | CI | — | Offline suite on Windows + Linux, ruff, pyright | `.github/workflows/ci.yml` | see `docs/final-consolidation-report.md` | `KEEP_CORE` |

---

## 3. Per-module audit (the eight questions)

Each entry answers: (1) what upstream already solved, (2) what upstream does not satisfy,
(3) the actual delta, (4) the layer it lives in, (5) the implementation style, (6) the evidence,
(7) whether it belongs on the default path, (8) its limits.

### 3.1 Tool runtime and progressive discovery — `src/atlasos/tools/`

1. Upstream: registry, executor, `side_effecting`, `idempotency_key_field`, batch execution.
2. Missing: disclosure gating, tool search, per-run tool state, dispatch accounting.
3. Delta: one registry holds every executable tool; `ToolCatalog` decides which are
   native-visible; `RunToolState` gates `dispatch_action`; `ToolSearchIndex` answers `tool.search`.
4. Layer: execution runtime (Layer 3).
5. Style: composition — `AtlasToolRuntime` wraps the AnyCode `ToolRegistry` and `ToolExecutor`.
6. Evidence: eval-final tool family 15/15 with Recall@1 = 1.0; ablation inconclusive.
7. Default path: yes (`tools.mode="progressive"`).
8. Limits: disclosure state is per run, not per worker; a hidden tool can still be guessed by
   name (rejected, and counted as `tool_not_disclosed`).

### 3.2 Action safety gate — `src/atlasos/actions/`

1. Upstream: `check_tool_access`, HITL approval hook, `ToolSecurityError`.
2. Missing: one decision point that combines policy, reviewer, approval, and executable binding;
   no audit record; no revalidation semantics.
3. Delta: `ActionSafetyGate` implements the decision ladder and records every evaluation;
   argument digests bind the decision to the exact executable and input.
4. Layer: execution runtime (Layer 3).
5. Style: independent implementation used as the executor seam
   (`AtlasToolRuntime._inner_executor`).
6. Evidence: control-plane 24 requests, 0 bypass, 0 hard-deny override, 0 stale approval reuse.
7. Default path: yes whenever a policy is configured (coding run).
8. Limits: the gate sees the tool call, not the shell command's semantics; `DENY` is text-level
   classification, not OS isolation.

### 3.3 Execution ledger — `src/atlasos/ledger/`

1. Upstream: idempotency claim/complete with `in_progress`/`conflict`/`replay`.
2. Missing: durable record of authorization + outcome; classification of *unknown* side effects;
   no reconciliation between idempotency rows and checkpoints.
3. Delta: SQLite ledger keyed by run/task/worker/tool/input digest, `DurableActionBridge`
   sequences gate → execute → record, `RecoveryClassifier` yields `COMPLETED/ACTIVE/UNCERTAIN`.
4. Layer: reliability (Layer 4), entered from the gate.
5. Style: composition on top of upstream idempotency; the ledger is the authority for Atlas
   actions, AnyCode checkpoints remain the authority for execution state.
6. Evidence: G3-1 3 `UNCERTAIN` records / 0 replays; v2-integrated executed 7 / replayed 1.
7. Default path: yes in the trusted coding run.
8. Limits: `UNCERTAIN` is resolved by an operator decision, not automatically; the ledger is a
   local SQLite file (single host).

### 3.4 Scoped workers — `src/atlasos/workers/`

1. Upstream: `AgentConfig`, `ExecutionContext`, `AgentPool`.
2. Missing: construction from (profile, assignment), frozen identity derivation, durable registry.
3. Delta: `ScopedWorkerFactory.create` derives a child `ExecutionContext` from the parent,
   renders the scoped prompt, records the instance, and returns an `AgentConfig` ready for the
   engine.
4. Layer: orchestration (Layer 2).
5. Style: adapter over `AgentConfig` + `ExecutionContext`.
6. Evidence: R1-0 measured fixed per-worker overhead (1,988–2,085 tokens); R0/R0.5 used the same
   factory for 1–3 workers.
7. Default path: yes.
8. Limits: no OS-level per-worker isolation; identity is carried in attributes, not enforced by
   the OS.

### 3.5 Sandbox binding — `src/atlasos/sandbox/`

1. Upstream: provider protocol + 6 backends, `PolicySandboxProvider`, `SandboxHandle`.
2. Missing: durable binding, reattachment, ownership lock, capability preconditions, workspace
   export, network/host-separation probes.
3. Delta: SQLite binding store with an explicit state machine; OS file lock for local ownership;
   digest-verified reattachment; `SandboxWorkspaceTools` bound to exactly one handle; empirical
   `probe_network_none` / `probe_host_separation`.
4. Layer: execution runtime (Layer 3).
5. Style: adapter (`OpenSandboxProvider`) + independent binding/validation components.
6. Evidence: v2-real-sandbox 2/3; eval-final 0/90 isolation violations; R1-2H 28/28.
7. Default path: yes in coding mode.
8. Limits: one container per grading in the oracle; CPU/memory quotas are not pinned.

### 3.6 Suspension and approvals — `src/atlasos/suspension/`, `src/atlasos/approvals/`

1. Upstream: in-memory `ApprovalManager` with timeout and stdin/webhook gates.
2. Missing: durability, whole-response atomicity, revalidation on resume.
3. Delta: durable suspension record, batch preflight before any tool executes, SQLite approval
   store with versioned resolutions, resume path that re-checks the binding digest.
4. Layer: reliability (Layer 4).
5. Style: subclass seam (`AtlasAgentRunner._execute_tool_blocks`) + independent stores.
6. Evidence: v2-integrated S1–S8; OS-process HITL recovery tests.
7. Default path: opt-in (requires M3 + M4 + a suspension store).
8. Limits: only reachable through the operator recovery harness; the app service refuses to
   reattach without M5/M6 authority.

### 3.7 Trajectory and execution integrity — `src/atlasos/trajectory/`

1. Upstream: `FilesystemRunStore` transcript, 15 event kinds, redaction, atomic writes.
2. Missing: action/outcome pairing, input digests, worker lifecycle terminals, reconciliation.
3. Delta: recorder writes `action_proposed` before execution and `tool_observed` after, with
   digests, sizes and a payload corpus in `record` mode; `TrajectoryReconciler` compares trace,
   ledger and result.
4. Layer: observability (Layer 4).
5. Style: extension of the upstream run store (same store, additional event kinds).
6. Evidence: R1-0 38 % action loss in `result.json`; R1-1 digest coverage 1.00.
7. Default path: yes (`trajectory.enabled`).
8. Limits: evidence is best-effort (`.env`-free, `try/except` around every write); no fsync
   guarantee.

### 3.8 Shared run budget — `src/atlasos/budget.py`

1. Upstream: per-runner `BudgetTracker` with token/cost/turn/tool caps.
2. Missing: one pool shared by every worker in a run; admission before a request is sent.
3. Delta: `RunBudgetState` + `RunBudgetHook` reserve an estimate before each request, learn the
   observed ratio, and turn a rejection into a real `budget_exceeded` failure.
4. Layer: execution runtime / reliability.
5. Style: hook (`before_turn`) + subclass outcome fix.
6. Evidence: G2a 0/4 overruns, 95/95/0 accounting; no success benefit.
7. Default path: yes in the coding run (`budget=guarded`).
8. Limits: estimate is not an upper bound; `asyncio.Lock` is single-process only.

### 3.9 Dual handoff — `src/atlasos/handoff.py`

1. Upstream: same-process handoff with context truncation.
2. Missing: durable result channel, paging read-back, trusted consumer identity.
3. Delta: typed handoff records with offload to a store, `handoff.read` paging tool, per-consumer
   task identity.
4. Layer: orchestration (Layer 2).
5. Style: independent implementation registered as external tools.
6. Evidence: G1 mechanism PASS, success unchanged.
7. Default path: no (`handoff_mode="legacy"`).
8. Limits: only 1 real offload sample; cross-process recovery untested in a real run.

### 3.10 Delegation router — `src/atlasos/workers/routing.py`

1. Upstream: 4 assignment policies.
2. Missing: a decision *not* to delegate, with reasons.
3. Delta: `RouteKind.DIRECT` vs `DELEGATE_PARALLEL` gated by capability, interface stability,
   parallel width and shared-write risk, with per-evidence status.
4. Layer: orchestration.
5. Style: independent policy module (deterministic, no LLM).
6. Evidence: R0 12/12 direct; R0.5 positive admission 6/6 but cost +40 %/+269 %.
7. Default path: no.
8. Limits: never produced a live parallel admission in a benchmark run; interface stability is
   inferred from text.

### 3.11 Deliverable verification — `src/atlasos/verification/`

1. Upstream: `QualityGate` sensors (ruff/pyright/pytest) inside the runner loop.
2. Missing: checks on the *deliverable* (structure, public contract, dynamic behaviour).
3. Delta: deterministic contract engine emitting `PASS/FAIL/UNKNOWN` findings, `UNKNOWN` when a
   check cannot be evaluated.
4. Layer: evaluation layer (kept out of the runtime path).
5. Style: independent engine + protocol for a dynamic runner.
6. Evidence: R1-2A 0 false positives across 66 deliverables; static 8.1 ms mean.
7. Default path: no (observation only).
8. Limits: public-contract coverage is thin (23 TP / 23 FN); a mid-month date family is not
   detected by the structural checks.

### 3.12 Oracle sandbox — `experiments/evaluation/sandboxed_oracle.py`

1. Upstream: no benchmark oracle.
2. Missing: grading isolated from the agent sandbox.
3. Delta: hidden tests mounted only inside a disposable, network-disabled sandbox; the host never
   executes benchmark code.
4. Layer: evaluation.
5. Style: independent runner over the Atlas sandbox binding.
6. Evidence: R1-2H 28/28 agreement, 0 model tokens, 5.9–6.4 s per grading.
7. Default path: n/a.
8. Limits: one container per grading; CPU/memory not pinned; `oracle_mode="host"` still exists
   for comparison.

### 3.13 Context plane — `src/atlasos/context/`

1. Upstream: `ContextManager` (window budget, compaction, masking) and cache-token parsing.
2. Missing: explicit logical context blocks, a selector/renderer split, layout hashing.
3. Delta: `ContextBlock`/`ContextRef` model, `TaskScopedSelector`, `StablePromptRenderer`
   (stable prefix before dynamic tail), `ContextPilotDedupTransform` behind a config flag.
4. Layer: orchestration (prompt construction).
5. Style: independent components; the renderer is used by `runtime.py` for task prompts.
6. Evidence: E2 no win; Stage 6 dedup shrinks prompt but not warm cache-miss.
7. Default path: `task_scoped` + `stable_deterministic` + `identity` transform only.
8. Limits: cache behaviour is provider-controlled; dedup never fired on short dependency outputs
   (0/72 calls).

### 3.14 Placement — `src/atlasos/placement/`

1. Upstream: none.
2. Missing: nothing required by the final product.
3. Delta: static / capability / context-locality placement planner.
4. Layer: orchestration (research).
5. Style: independent policy set.
6. Evidence: E2 P0/P1 halved agent switches, no token effect.
7. Default path: no (`policy="bypass"` hardcoded in the app).
8. Limits: only meaningful for multi-worker DAGs, which the benchmarks do not reward.

### 3.15 Trusted recovery — `src/atlasos/trusted_recovery.py`

1. Upstream: sweep + resume callbacks, no composition.
2. Missing: rebuild the trusted worker and tool runtime from durable state.
3. Delta: reconstructs `ScopedWorkerFactory` + `AtlasToolRuntime` + ledger/approval/suspension
   stores, then delegates to `resume_execution_suspension`.
4. Layer: reliability.
5. Style: composition helper (explicitly not a second runtime).
6. Evidence: Stage D 11 real OS-process scenarios; `test_stage_d_*` integration tests.
7. Default path: no (operator harness only).
8. Limits: single host, single READY sandbox, never creates a replacement sandbox.

### 3.16 Ownership — `src/atlasos/ownership/`

1. Upstream: workspace-relative path fence.
2. Missing: cross-worker write protection.
3. Delta: declaration manifest + enforcement policy hooked into the gate as an external policy.
4. Layer: reliability.
5. Style: independent manifest/policy + a gate adapter.
6. Evidence: R1-1 enforce arm 1/16 vs 7/16 baseline; shell coverage PARTIAL.
7. Default path: no.
8. Limits: text-level shell classification; interpreter invocations are rejected
   (`python3 -m pytest` was the main compatibility loss); no OS isolation.

### 3.17 History compaction and ingress offload — `src/atlasos/history_compaction.py`, `src/atlasos/tool_result_offload.py`

1. Upstream: compaction primitives inside `ContextManager`.
2. Missing: recoverable archival with a read-back tool.
3. Delta: rolling archive with digests + `history.read` paging.
4. Layer: execution runtime.
5. Style: hook + external tool.
6. Evidence: G2b-1 negative; G3-2 neutral; `history.read` never called in a real run.
7. Default path: no (`off`).
8. Limits: breaks the cache prefix; model never used the recovery tool.

### 3.18 AnyCode integration adapters — `src/atlasos/integrations/`

1. Upstream: `AnyCode`, `Agent`, `AgentRunner`, provider factory registry.
2. Missing: a seam to swap task-prompt construction, the tool runtime, budget hook and compaction
   hook per run.
3. Delta: `AtlasContextEngine(AnyCode)` overriding `build_agent`; `DeepSeekAdapter` registered as
   an AnyCode provider factory.
4. Layer: integration (Layer 5).
5. Style: subclass + provider registration.
6. Evidence: every real run used this seam.
7. Default path: yes.
8. Limits: overrides are narrow by design; new upstream versions may change these signatures.

---

## 4. Contribution statement (defensible wording)

Use these sentences, not stronger ones:

- "Built a persistent action ledger on top of AnyCode's idempotency primitives, and added
  classification and reconciliation for actions whose outcome is unknown."
- "Added whole-response preflight suspension on top of AnyCode's tool executor, so a model
  response cannot execute partially while approval is pending."
- "Added a durable sandbox binding and verified reattachment layer; AnyCode 0.9.0 only offers
  create/destroy."
- "Added a shared run budget that admits requests before they are sent; upstream tracks budget
  per agent."
- "Added execution trace integrity (proposed action ⟷ observed outcome with input digests); the
  upstream transcript records both sides independently but does not pair them."

Do **not** write: "implemented an idempotent execution framework", "implemented DAG scheduling",
"implemented checkpoint/resume", "implemented sandbox isolation", "implemented agent runtime".

---

## 5. What stayed upstream

DAG scheduling, wave execution, agent loop, turn checkpoints, `TaskQueue`, provider adapters,
MCP transport, plugin discovery, cost pricing, `QualityGate` sensors, HITL channels, and the
run-store transcript are all used as-is. AtlasRuntime contributes seams and durable records
around them; it does not re-implement them.
