# AtlasRuntime Architecture

**Context-aware and Reliable Agent Runtime.**

This document describes the system that **exists in the repository**, verified against the source. It
is the *implementation-status* layer; the design baseline is
[`architecture-design-contract.md`](architecture-design-contract.md) and it outranks this file. Where
the two differ, this file is describing a gap, not amending the contract.

Two cores — Context Engineering (§9) and Reliable Execution (§5–§7) — plus the Adaptive Delegation
extension (§4, §10) and the evaluation layer (§8). The V1 research track is not a "retained
subsystem whose claim does not survive": logical context, task-scoped selection, deterministic
rendering, the transform seam and context-locality placement are part of the design. Its history is
indexed in `docs/research-history.md`.

Upstream attribution and per-module provenance live in `docs/anycode-delta.md`. Trade-offs and
rejected designs live in `docs/design-decisions.md`.

---

## 1. Boundary with AnyCode

AtlasRuntime is **not** a new agent framework. `anycode-py==0.9.0` owns the agent loop, the DAG
scheduler, the turn checkpoint, provider adapters, the tool executor and the run-store transcript.

| Responsibility | Owner |
|---|---|
| Agent loop, turn lifecycle, stop reasons | AnyCode `core/agent.py`, `core/runner.py` |
| DAG task queue, wave scheduling, assignment | AnyCode `tasks/queue.py`, `core/scheduler.py`, `core/orchestrator.py` |
| Turn checkpoint + resume | AnyCode `checkpoint/*`, `runstore/store.py` |
| Provider adapters, retry/resilience, cache-token parsing | AnyCode `providers/*` |
| Tool registry, execution normalization, `retry_safe` semantics | AnyCode `tools/registry.py`, `tools/executor.py` |
| Idempotency claim/complete/replay | AnyCode `tools/idempotency.py` |
| Sandbox provider protocol + backends | AnyCode `sandbox/*` |
| **Authorization decision, durable action ledger** | **AtlasRuntime `actions/`, `ledger/`** |
| **Scoped worker construction + trusted identity derivation** | **AtlasRuntime `workers/`** |
| **Sandbox binding, ownership, reattachment, workspace tools** | **AtlasRuntime `sandbox/`** |
| **Durable suspension + persistent approval** | **AtlasRuntime `suspension/`, `approvals/`** |
| **Execution trace integrity (proposed ⟷ observed)** | **AtlasRuntime `trajectory/`** |
| **Shared run budget with pre-request admission** | **AtlasRuntime `budget.py`** |
| **Deliverable verification, benchmark oracle isolation** | **AtlasRuntime evaluation layer (not in the runtime path)** |

---

## 2. Layers

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ Layer 1 — Application                                                        │
│   app/cli.py (Typer: atlas run/runs/status/logs/workers/approvals/...)        │
│   app/service.py (run registry, projections, approval/recovery endpoints)     │
│   app/web.py (read-only HTTP projection of the same service)                  │
│   Responsibility: task input, run bookkeeping, presentation. No state machine.│
└──────────────────────────────────────────────────────────────────────────────┘
                                   │
┌──────────────────────────────────────────────────────────────────────────────┐
│ Layer 2 — Orchestration                                                      │
│   runtime.py        AtlasRuntime facade (config, providers, context, metrics) │
│   workflow.py       AtlasWorkflow → AnyCode TaskSpec/AgentConfig compiler     │
│   workers/          ScopedWorkerFactory, WorkerStore, WorkerCatalog           │
│   workers/routing.py  delegation router (DIRECT | DELEGATE_PARALLEL)  [opt-in]│
│   handoff.py        dual-channel handoff runtime                      [opt-in]│
│   placement/        placement planner (bypass by default)             [opt-in]│
│   Scheduling itself is AnyCode's.                                            │
└──────────────────────────────────────────────────────────────────────────────┘
                                   │
┌──────────────────────────────────────────────────────────────────────────────┐
│ Layer 3 — Execution runtime                                                  │
│   tools/runtime.py  AtlasToolRuntime: one registry, disclosure gate, dispatch │
│   tools/catalog.py  ToolCatalog: which tools are native-visible               │
│   tools/search.py   ToolSearchIndex (keyword | semantic)                      │
│   tools/state.py    RunToolState: disclosed tools per run                     │
│   actions/gate.py   ActionSafetyGate: ALLOW | DENY | REQUIRE_HUMAN            │
│   ledger/           SQLiteActionLedgerStore, DurableActionBridge              │
│   sandbox/          binding store, ownership lock, reattachment, workspace    │
│   budget.py         RunBudgetState + RunBudgetHook (shared across workers)     │
└──────────────────────────────────────────────────────────────────────────────┘
                                   │
┌──────────────────────────────────────────────────────────────────────────────┐
│ Layer 4 — Reliability & observability                                        │
│   suspension/       durable suspension store + coordinator                    │
│   approvals/        persistent approvals + resume with revalidation           │
│   durability/       session sidecar manifest, deterministic context rebuild   │
│   trajectory/       recorder, payload corpus, reconciler                      │
│   provenance:       AtlasRuntime never overrides AnyCode checkpoints          │
└──────────────────────────────────────────────────────────────────────────────┘
                                   │
┌──────────────────────────────────────────────────────────────────────────────┐
│ Layer 5 — AnyCode integration                                                │
│   integrations/anycode_engine.py  AtlasContextEngine(AnyCode)                 │
│   tools/conversation.py           AtlasToolAgent + AtlasAgentRunner            │
│   integrations/deepseek.py        DeepSeekAdapter registered as a provider     │
│   config.py                       AtlasConfig (every mechanism is switchable) │
└──────────────────────────────────────────────────────────────────────────────┘
```

The evaluation layer (`experiments/evaluation/`) sits beside this stack. It imports the runtime;
the runtime never imports it.

---

## 3. Normal execution path

Entry points, in the order the code actually runs:

```text
atlas run "..." --mode workflow                app/cli.py::run_task
  └─ AtlasApplicationService.submit             app/service.py
      └─ thread _execute → _execute_workflow
          └─ AtlasRuntime(config).run(workflow) runtime.py
              ├─ _setup_tool_runtime            → AtlasToolRuntime
              ├─ _place                          → placement policy ("bypass" in app)
              ├─ _register_providers             → DeepSeekAdapter via anycode provider factory
              ├─ compile_workflow                → AnyCode TaskSpec / AgentConfig
              ├─ write_manifest                  → atlas_manifest.json
              ├─ _create_engine                  → AtlasContextEngine(OrchestratorConfig)
              └─ _execute → engine.run_tasks(team, tasks)
                              │
                              ▼  (AnyCode orchestrator — upstream)
                        Scheduler assigns task → build_agent (overridden)
                              │
                              ▼
                        AtlasToolAgent.run
                          └─ AtlasAgentRunner.stream          (AnyCode loop)
                              ├─ before_turn hooks: budget admission, compaction,
                              │                     conversation snapshot
                              ├─ adapter.chat/stream → provider
                              ├─ verification gate (upstream, optional)
                              └─ _execute_tool_blocks             (Atlas seam, M6 barrier)
                                  └─ ToolExecutor seam = ActionSafetyGate
                                      ├─ _evaluate: policy → reviewer → external policy
                                      │             → approval → binding digest
                                      ├─ DurableActionBridge: ledger STARTED
                                      ├─ real tool: SandboxWorkspaceTools / upstream builtin
                                      └─ ledger terminal record + trajectory observation
```

Result handling:

```text
engine.run_tasks → TeamRunResult
  └─ AtlasRuntime._finalize
      ├─ normalize tasks/workers → AtlasRunResult
      ├─ write result.json, append runs.jsonl, write inference.jsonl
      └─ update atlas_manifest.json status
```

Key classes: `AtlasRuntime` (`runtime.py`), `AtlasContextEngine`
(`integrations/anycode_engine.py`), `AtlasToolAgent` / `AtlasAgentRunner`
(`tools/conversation.py`), `AtlasToolRuntime` (`tools/runtime.py`), `ActionSafetyGate`
(`actions/gate.py`).

### Persisted state of a run

| Artifact | Writer | Meaning |
|---|---|---|
| `atlas_manifest.json` | `durability/manifest.py` | sidecar: git commit, workflow hash, config hashes, checkpoint location |
| `checkpoints/` | AnyCode `CheckpointManager` | execution truth (turn/wave snapshots) |
| `run_tool_state.json` | `tools/state.py` | tools disclosed in this run |
| `tool_conversations.json` | `tools/conversation.py` | per-task conversation at turn boundaries |
| `inference.jsonl` | `metrics/collector.py` | per-call tokens, cache hit/miss, latency, cost |
| `result.json`, `runs.jsonl` | `experiments.py` | normalized outcome + experiment index |
| `trajectory/` | `trajectory/recorder.py` (+ AnyCode run store) | action proposed/observed, worker lifecycle |
| `workers.json` | `workers/store.py` | worker instances and their trusted identity |
| `ledger.db` | `ledger/store.py` | authorized actions and outcomes |
| `approvals.db`, `suspension.db` | `approvals/store.py`, `suspension/store.py` | durable approval and suspension records |
| `idempotency.db` | AnyCode `SQLiteToolIdempotencyStore` | claim/complete records |
| `sandbox_bindings.db` | `sandbox/store.py` | sandbox binding state machine |

---

## 4. Multi-worker path

```text
AtlasWorkflow(agents=[A, B, C], tasks=[t1, t2, t3(depends_on=[t1])])
  └─ compile_workflow → TaskSpec(title=t1..t3, assignee=worker_name, depends_on=[...])
      └─ AnyCode TaskQueue + Scheduler (dependency-first / capability / round-robin)
          ├─ wave 1: t1, t2 in parallel   (bounded by AnyCode concurrency)
          └─ wave 2: t3 after t1 completes
```

Per worker: `ScopedWorkerFactory.create` derives a child `ExecutionContext` with
`run_id/task_id/worker_id/profile_id/parent_agent` attributes, renders the scoped prompt, records
the instance in `workers.json`, and returns the frozen `AgentConfig`. All workers share one
`AtlasToolRuntime`, one `RunToolState`, one ledger, and — when configured — one
`RunBudgetState`, so budget and tool disclosure are run-scoped, not worker-scoped.

Cross-worker data transfer is either upstream task-result injection or the opt-in dual-channel
handoff (`handoff.py`, `handoff_mode="dual"`). Multi-worker is **not** the default: the
benchmarks did not show a quality benefit (see `docs/evaluation-summary.md`).

---

## 5. Budget termination path

```text
RunBudgetHook.before_turn  (tools/conversation.py, appended last so the estimate sees the
                            prompt that will actually be sent)
  ├─ estimate = system + tool defs + conversation + completion reserve
  ├─ RunBudgetState.reserve(...) under asyncio.Lock
  ├─ admission denied → raise → runner turns it into an `error` event
  └─ AtlasToolAgent._apply_budget_outcome
        → success=False, stop_reason=budget_exceeded, reason preserved
```

Without that last step an AnyCode run that never receives a `done` event falls back to an empty
`success=True` result. The rejection is therefore recorded as a failure, and the ledger/trace
show the same terminal state.

Normal end: `RunBudgetState` releases the reservation and the ratio estimate is updated.

---

## 6. Authorization-denial path

```text
ActionSafetyGate.execute / execute_batch
  ├─ _hard_precheck        tool known? disclosed? dispatchable?
  ├─ _evaluate
  │    ├─ ActionPolicy rules (allow / deny / require_human)
  │    ├─ ActionReviewer (optional model-free reviewer)
  │    ├─ external policy (e.g. ownership)   → DENY
  │    ├─ approval manager / durable approval → REQUIRE_HUMAN
  │    └─ binding digest (tool + implementation + input)
  ├─ DENY → ToolResult(is_error=True, retry_safe=False) + ledger DENIED + audit
  └─ ALLOW → _execute → ledger STARTED → tool → COMPLETED / EXECUTED_ERROR
```

A denial is visible to the model as a tool error and is never silently retried. An action with an
unknown outcome is recorded as `UNCERTAIN`; it is not replayed automatically.

---

## 7. Recovery path

Two distinct mechanisms, deliberately separate:

**A. Checkpoint resume (normal interruption) — workflow path, library API only**

```text
AtlasRuntime.resume(run_id, workflow)          # no CLI command reaches this
  ├─ active_suspensions(run_id) → raise SuspensionResumeRequired if any
  └─ _resume
      ├─ read atlas_manifest.json, verify_compatible(workflow, config hashes)
      ├─ completed_task_ids → rebuild_context_store (deterministic, no model call)
      ├─ _create_engine → engine.run_tasks(..., resume_from=checkpoint)
      └─ _finalize(recovery=...)
```

Scope note: `app/coding.py`, `evaluation/runner.py` and `evaluation/baseline.py` build a bare
`OrchestratorConfig()`, so `checkpoint=None` and **no coding run writes a checkpoint**. Checkpoint
resume is reachable only through the workflow composition, which in turn has no action gate, no
ledger and no sandbox.

**B. Suspension settlement (approval / unknown side effect)**

```text
atlas approve <approval_id>  → ApprovalCoordinator.resolve + SQLiteSuspensionStore
atlas resume <run_id>        → service.recovery() reports RECOVERY_REQUIRED
operator harness             → TrustedRecoverySession (trusted_recovery.py)
                                 → resume_execution_suspension (approvals/resume.py)
```

`atlas resume` never performs a checkpoint resume: `AtlasApplicationService.resume` raises
`RecoveryAuthorityUnavailable` unless an active trusted suspension exists.

The application service deliberately refuses to reattach or destroy a sandbox on its own: without
M5/M6 authority that would be an unreviewed remote side effect. Recovery requires the operator
harness, which rebuilds the same worker and tool runtime from durable state and revalidates the
binding digest before continuing.

---

## 8. Verification path (evaluation only)

```text
experiments/evaluation/runner.py::run_sample
  ├─ execute the task through the runtime
  ├─ _observe_verification → verification/engine.py::VerificationEngine
  │     structural / file / text / dynamic checks → PASS | FAIL | UNKNOWN
  ├─ oracle: sandboxed_oracle.py → hidden tests inside a disposable network-disabled sandbox
  └─ persist verification + oracle + metrics into the run directory
```

Verification never changes task semantics: it observes the produced files and records findings.
`UNKNOWN` is a first-class result — a check that cannot be evaluated is not reported as a pass.

---

## 9. Core: logical context + deterministic rendering

The Context Plane is what a task logically receives, kept separate from how a prompt is laid out.

```text
workflow.py / coding composition task text
  └─ context/store.py       InMemoryContextStore (ContextBlock / ContextRef)
      └─ context/selectors.py  TaskScopedSelector  (WHAT the task logically needs)
          └─ context/renderer.py StablePromptRenderer
                order: stable system prefix → stable task prefix → context blocks → dynamic tail
                └─ context/transform.py  IdentityContextTransform | ContextPilotDedupTransform
```

One implementation builds every Atlas prompt: `context/task_prompt.py::AtlasTaskPromptBuilder`
(project `task-output:<id>` blocks from completed tasks → select → transform → render). It is reached
through the single AnyCode seam `AtlasContextEngine._build_task_prompt`:

| Entry point | Context Plane switch | State |
|---|---|---|
| Workflow (`atlas run --mode workflow`) | `app/service.py` sets `policy="task_scoped"`, `renderer="stable_deterministic"`, `transform="identity"` | on by default |
| App coding (`--mode coding`) | `app/coding.py::_build_context_plane` | opt-in (`context.policy="task_scoped"`); `upstream_default` returns `None` |
| Evaluation (`experiments.evaluation`) | `runner.py::_context_prompt_builder` | opt-in, same switch; refuses `task_scoped` together with `handoff_mode="dual"` |

Separation of concerns is preserved: selectors decide *what*, renderers decide *how*. ContextPilot
dedup stays behind `transform="contextpilot_dedup"` plus the optional extra, because it produced no
measurable cache-miss win (see `docs/evaluation-summary.md` §7).

Wired is not the same as used: the Final-4 pilot (2026-11-14) declared `task_scoped` in its manifest
but ran `eval-benchmark.yaml`, which sets no context section and therefore resolved to
`upstream_default`. Those three runs did not exercise this plane. See
`docs/final-4-legacy-pilot-reclassification.md`.

---

## 10. Mechanism switchboard

Every non-default mechanism is a config field, and the default configuration never enables a
mechanism the experiments did not support.

Defaults below are `config.py`'s, i.e. what library callers get from `AtlasConfig()`. The
application service overrides the context pair; the coding session and the evaluation runner
construct their tool runtime directly, so `tools.mode` does not apply to them.

| Config field | Default | Values | Notes |
|---|---|---|---|
| `provider.model` | `deepseek-chat` | any provider model | DeepSeek adapter is the reference provider |
| `context.policy` | `upstream_default` | `upstream_default` / `task_scoped` | the app service sets `task_scoped`; coding and evaluation must set it themselves |
| `context.renderer` | `upstream_default` | `upstream_default` / `naive_execution_order` / `stable_deterministic` | only two policy/renderer pairs are legal |
| `context.transform.name` | `identity` | `identity` / `contextpilot_dedup` | requires `task_scoped` + `stable_deterministic` and the `contextpilot` extra |
| `scheduler.policy` | `upstream_default` | any, but only `upstream_default` is accepted today | recorded, not re-implemented |
| `placement.policy` | `bypass` | `bypass` / `static_baseline` / `capability_only` / `context_locality` | `bypass` used by the app |
| `tools.mode` | `disabled` | `disabled` / `progressive` | `progressive` = search + dispatch; the coding and evaluation paths build the runtime themselves |
| `tools.search_backend` | `keyword` | `keyword` / `semantic` | semantic requires `tools` extra |
| `trajectory.enabled` | `false` | bool | opt-in on the workflow path; always on in coding/evaluation |
| `sandbox.enabled` | `false` | bool | the coding/evaluation paths manage the sandbox explicitly |
| `sandbox.provider` | `fake` | `fake` / `opensandbox` / `daytona` | the app and evaluation paths require a real provider |

Opt-in mechanisms (not in the default config): durable suspension/approvals (M6), dual handoff,
selective delegation, ownership enforcement, history compaction, ingress offload, worker task
contracts. Each is documented with its measured result in `docs/design-decisions.md`.

---

## 11. Explicit non-goals

No physical KV-cache management, no serving engine, no container runtime of our own, no
distributed scheduler, no cross-host recovery, no business-domain adapters. See
`docs/limitations.md`.
