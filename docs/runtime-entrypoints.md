# Runtime Entry Points and Capability Matrix

Read-only audit of the code on `chore/final-1-5-repository-baseline` (`BASE_SHA`
`16251d05c34cff41d280396a1647214914e51a31`). Every statement below was traced to a call site; a file
existing is never treated as proof that a mechanism runs.

Re-verified at `b555043` for the Context Plane rows of §6: both coding compositions compose
`AtlasTaskPromptBuilder` behind `context.policy="task_scoped"` (Final-3 wiring). That a later pilot did
not enable the switch is a *configuration* fact, not evidence that the wiring is absent. The design
baseline for what these mechanisms are is `docs/architecture-design-contract.md`.

The three paths are **not** the same runtime composition. That is the single most important fact in
this document.

## 1. Workflow path

Entry: `atlas run "<task>" --mode workflow` (`src/atlasos/app/cli.py::run_task`).

```text
atlas run … --mode workflow
  └─ AtlasApplicationService.submit                     app/service.py
      └─ thread _execute → _execute_workflow
          ├─ config: context.policy="task_scoped", renderer="stable_deterministic",
          │          transform="identity", placement.policy="bypass", pricing set
          └─ AtlasRuntime(config).run(workflow)         runtime.py
              ├─ _setup_tool_runtime   → returns immediately (tools.mode="disabled")
              ├─ _place                → bypass (no planner)
              ├─ _register_providers   → DeepSeekAdapter via anycode provider factory
              ├─ compile_workflow      → TaskSpec/AgentConfig, agents get tools=[]
              ├─ write_manifest        → run_dir/atlas_manifest.json
              ├─ _create_engine        → AtlasContextEngine(
              │                             OrchestratorConfig(checkpoint=CheckpointConfig(
              │                                 enabled=True, path=run_dir/checkpoints, keep_last=10)),
              │                             prompt_builder=_build_atlas_prompt, tool_runtime=None)
              └─ _execute              → engine.create_team + engine.run_tasks
                    └─ AnyCode orchestrator: task queue, wavefront scheduling, agent loop
                          └─ AtlasContextEngine._build_task_prompt  (only overridden seam)
                                └─ ContextStore → TaskScopedSelector → StablePromptRenderer
                                   (+ optional IdentityContextTransform / ContextPilotDedupTransform)
```

| Question | Answer |
|---|---|
| ContextStore | `InMemoryContextStore`, rebuilt per run; dependency outputs projected as `task-output:<id>` blocks |
| Selector | `TaskScopedSelector` (`context/selectors.py`) |
| Renderer | `StablePromptRenderer(preserve_input_order=True)`, layout hashed per task |
| Workflow compiler | `workflow.py::compile_workflow`; dependencies are title-based at the AnyCode boundary |
| Tool runtime | **none** — no registry, no gate, no ledger, no sandbox; agents are compiled with `tools=[]` |
| Worker construction | upstream only; `ScopedWorkerFactory` is not used |
| Scheduler | AnyCode task queue + wavefront scheduler; Atlas adds no scheduling policy |
| Checkpoint | **on** (upstream `CheckpointManager` under `run_dir/checkpoints`) |
| Resume | `AtlasRuntime.resume(run_id, workflow)` — library API; `atlas resume` does **not** call it |
| Result | `_finalize` → `result.json`, `runs.jsonl`, `inference.jsonl`, manifest status |

The workflow path is the only composition with a durable checkpoint, and the only one that renders
Atlas prompts. It has **no** authorization gate and **no** ledger.

## 2. Application coding path

Entry: `atlas run "<task>" --mode coding [--workspace <dir>]` → `app/service.py::_execute_coding`.

```text
_resolve_workspace        requires the directory to be under experiments/fixtures
                          AND to contain csv_parser.py → otherwise HTTP 422
  └─ run_coding_session(CodingRunOptions)      app/coding.py  (in-process, no subprocess)
      ├─ provider: deepseek (real); real sandbox provider required (no fake fallback)
      ├─ ProjectSandboxManager + ensure_binding  → sandbox_bindings.db
      ├─ probe_network_none / probe_host_separation   (empirical, fail-closed)
      ├─ ScopedWorkerFactory.create            → workers.json, trusted ExecutionContext
      ├─ AtlasToolRuntime(                    tools/runtime.py
      │     action_policy=_coding_policy(),
      │     action_catalog=ActionCatalog(),
      │     action_ledger=SQLiteActionLedgerStore,
      │     idempotency_store=SQLiteToolIdempotencyStore,
      │     suspension_store=SQLiteSuspensionStore,
      │     approval_store=SQLiteApprovalStore,
      │     sandbox_tools=SandboxWorkspaceTools,
      │     trajectory_recorder=TrajectoryRecorder)
      └─ AtlasContextEngine(OrchestratorConfig(), tool_runtime=runtime)   ← checkpoint=None
            └─ one team, one agent, one TaskSpec → run_tasks
```

| Question | Answer |
|---|---|
| Tools exposed | 8 core tools natively (`bash`, `file_read`, `file_write`, `file_edit`, `grep`, `list_files`, `tool_search`, `dispatch_action`); project tools are disclosed then dispatched |
| Scoped worker | yes, profile `coding_worker`, one worker per run |
| Action gate / ledger | yes / yes |
| Sandbox | yes — one binding, workspace at `/workspace/project`, `network="none"` verified |
| Budget | **none** (no `RunBudgetState` in this composition) |
| Trajectory | always on (`run_dir/trajectory`) |
| Suspension / approval | yes — stores are configured, so `REQUIRE_HUMAN` becomes a durable suspension |
| Checkpoint / resume | **none** — `OrchestratorConfig()` leaves `checkpoint=None`; no resume entry exists |
| Verification / oracle | none |

The UAT-shaped workspace requirement is real: `csv_parser.py` must exist. This is a demonstration
path, not a general repository driver, and this stage did not change it.

## 3. Evaluation path

Entry: `python -m experiments.evaluation.cli run [--sample X] [--repetitions N]`.

```text
experiments/evaluation/cli.py::_run
  └─ runner.run_sample(task, settings: RunnerSettings, …)
      ├─ sandbox + fixture materialisation + host-isolation probes
      ├─ ProjectSandboxManager (real provider) / ownership manifest + enforcer (opt-in)
      ├─ router (only when delegation_route_mode != "off")   → may collapse to the A0 plan
      ├─ HandoffRuntime (legacy | dual), HistoryCompactionRuntime, ToolResultIngressRuntime
      ├─ AtlasToolRuntime | AllToolsAtlasRuntime            (progressive_tools=True by default)
      ├─ RunBudgetState                                      (only budget_mode="guarded")
      ├─ AtlasContextEngine(OrchestratorConfig(on_progress=…), …)   ← checkpoint=None
      ├─ oracle: sandboxed_oracle.evaluate_oracle_isolated  (oracle_mode="sandbox" default)
      └─ verification: VerificationEngine.verify_async      (verification_mode="observe"; never gates)
```

| Question | Answer |
|---|---|
| Task loading | `experiments/evaluation/tasks.py::TASKS`, 30 tasks, dataset `atlasos-agentbench-v1` 1.0.0 |
| Native AnyCode baseline | `experiments/evaluation/baseline.py`: `AnyCode(OrchestratorConfig())` with native built-in tools on a host copy — no Atlas gate, ledger, sandbox or context plane |
| Atlas single vs multi | selected by the task's own `workers` tuple (`single` mode vs `multi_worker` mode) |
| Router | evaluation-only; see §5 |
| Sandbox | yes, real provider, same image and `network="none"` |
| Verification | observation only; writes `verification.json` / `verification_summary.json` |
| Oracle | hidden tests in a disposable sandbox; `host` mode exists only to reproduce old numbers |
| Metrics | `result.json` (tokens, cache hit/miss, latency, cost), `provider_calls.jsonl`, `trajectory/`, `ledger.db` |
| Evidence | `experiments/evidence/` holds sanitized aggregates; raw runs stay in `experiments/runs/` (ignored) |

**Shared core with the application path:** the tool runtime, action gate, ledger, sandbox manager,
scoped worker and trajectory recorder. **Different:** the evaluation path adds the oracle,
verification, ownership, handoff, compaction, offload, budget and router, and it does not configure
suspension/approval stores. Neither path has a checkpoint.

## 4. Recovery path

Four separate mechanisms, deliberately not unified (`src/atlasos/{durability,approvals,suspension,
sandbox,ledger}`):

| Mechanism | Entry | Reachable from |
|---|---|---|
| Checkpoint resume + context rebuild | `AtlasRuntime.resume(run_id, workflow)` → `durability.verify_compatible` → `rebuild_context_store` → `run_tasks(resume_from=…)` | workflow path, **library API only** |
| Durable suspension resume | `AtlasRuntime.resume_suspension` → `approvals/resume.py::resume_execution_suspension` | application coding (stores configured) |
| Approval resolution | `atlas approve/reject` → `ApprovalCoordinator.resolve` (refuses without an active suspension) | application service |
| Sandbox reattachment | `sandbox/reattachment.py`, `trusted_recovery.py::TrustedRecoverySession` | operator harness, manual |
| Uncertain side effect | `ledger/recovery.py`: `IN_PROGRESS` → `UNCERTAIN`, never an automatic replay | coding + evaluation |

`atlas resume <run_id>` maps to `AtlasApplicationService.resume`, which raises
`RecoveryAuthorityUnavailable` unless a trusted suspension exists. It never performs a checkpoint
resume and never reattaches a sandbox.

Final-2 pilot findings (branch `experiment/final-2-recovery-pilot`, `c5fce5c`) that are **not**
fixed on this branch: both coding compositions build a bare `OrchestratorConfig()`, so no checkpoint
file is ever written and no coding run can resume; an abrupt kill leaks a live sandbox pair.

## 5. Router path

`src/atlasos/workers/routing.py::route_delegation` (frozen policy `r0.1.0`, six ordered gates).

1. **Input** — `DelegationProposal` (candidate tasks with `depends_on`, plus provenance-tagged
   evidence: write/read paths, risk class, interface symbols, deliverables, context, merge protocol,
   goal coverage) + `CapabilityCatalog` (from the trusted `WorkerCatalog`) + `RoutingPolicy`
   (frozen thresholds). Pure function: no I/O, no clock, no randomness.
2. **DIRECT** — the runner replaces the task's worker list with `a0_steps(task)`, forces
   single-worker grading (`distinct_workers=1`) and records `executed_worker_count=1`.
3. **DELEGATE_PARALLEL** — the candidate DAG runs unchanged and `executed_worker_count =
   len(task.workers)`. (`DELEGATE_ONE` exists in the vocabulary but the runner only branches on
   `is_direct`.)
4. **Does it control the worker count?** Only by collapsing a plan that already exists. It cannot
   create a worker, an assignment or a task.
5. **Does it control DAG compilation?** No. It never rewrites the proposal; `compile_workflow` runs
   afterwards on whatever plan survived.
6. **Does it depend on a pre-built decomposition?** Yes. The proposal is built from the benchmark
   task's frozen `WorkerStep` list (`stage_r0_routing.py::build_proposal`); no planner produces it at
   runtime.
7. **Offline only?** In practice yes: no module under `src/` imports the router; only
   `experiments/evaluation/` and its analysis scripts do.
8. **Default off?** Yes — `RunnerSettings.delegation_route_mode = "off"`.
9. **Tests** — `tests/unit/test_delegation_routing.py` (31 cases), `tests/unit/test_stage_r05_routing.py`
   (15 cases).
10. **Most important limitation** — the router can only accept or reject a decomposition it was
    handed; it cannot improve one. In the live R0 sample it rejected 12/12 candidates, and the
    forced-positive R0.5 experiment admitted 6/6 still at +40 % cost, so no generalisation benefit
    has been demonstrated. It is the subject of Track C, not a solved capability.

## 6. Capability matrix

`ACTIVE_DEFAULT` — on in that entry point with the shipped defaults. `ACTIVE_OPTIONAL` — reachable
there by configuration only. `EVALUATION_ONLY` — wired only in `experiments/`. `NOT_WIRED` — the
module exists but this entry point never constructs it. `NOT_APPLICABLE` — the path has no such
concept. `NOT_VALIDATED` — present, no live evidence.

| Mechanism | Workflow | App Coding | Evaluation | Evidence |
|---|---|---|---|---|
| Context selector (`TaskScopedSelector`) | ACTIVE_DEFAULT | ACTIVE_OPTIONAL (`context.policy="task_scoped"`, `app/coding.py::_build_context_plane`) | ACTIVE_OPTIONAL (same switch, `runner.py::_context_prompt_builder`; not enabled in the Final-4 pilot) | E2, S6 |
| Stable renderer | ACTIVE_DEFAULT | ACTIVE_OPTIONAL (same switch) | ACTIVE_OPTIONAL (same switch) | S6 Table A |
| ContextPilot dedup | ACTIVE_OPTIONAL | ACTIVE_OPTIONAL (requires `task_scoped` + `stable_deterministic`) | ACTIVE_OPTIONAL (same) | S6 Table B (no cache-miss win) |
| Context-locality placement | ACTIVE_OPTIONAL (`placement.policy`) | NOT_WIRED (`bypass`; one task, nothing to place) | NOT_WIRED (`runner.py` constructs one worker per frozen step) | S6 Table C |
| Tool search (`tool_search`) | NOT_APPLICABLE (agents have no tools) | ACTIVE_DEFAULT | ACTIVE_DEFAULT | M1: 2 599 → 1 399 tokens |
| Progressive disclosure (`dispatch_action`) | NOT_APPLICABLE | ACTIVE_DEFAULT | ACTIVE_DEFAULT | M1 acceptance table |
| Scoped worker + trusted identity | NOT_WIRED | ACTIVE_DEFAULT | ACTIVE_DEFAULT | R1-1 ownership |
| Dual handoff | NOT_WIRED | NOT_WIRED | ACTIVE_OPTIONAL (`handoff_mode="dual"`) | G1 |
| Shared run budget | NOT_WIRED | NOT_WIRED | ACTIVE_OPTIONAL (`budget_mode="guarded"`) | G2a |
| Selective delegation router | NOT_WIRED | NOT_WIRED | ACTIVE_OPTIONAL (`delegation_route_mode`) | R0 (12/12 rejected), R0.5 |
| Worker ownership | NOT_WIRED | NOT_WIRED | ACTIVE_OPTIONAL (`ownership_mode`, default off) | R1-1 (negative) |
| Action safety gate | NOT_WIRED (`action_policy=None`) | ACTIVE_DEFAULT | ACTIVE_DEFAULT | control plane 24 → 15/1/8 |
| Durable action ledger | NOT_WIRED | ACTIVE_DEFAULT | ACTIVE_DEFAULT | G3-1, M4 |
| Sandbox binding | NOT_WIRED | ACTIVE_DEFAULT | ACTIVE_DEFAULT | M5, Stage D |
| Durable suspension | NOT_WIRED | ACTIVE_DEFAULT | NOT_WIRED (no stores passed) | M6 |
| Checkpoint resume | ACTIVE_OPTIONAL (library API) | NOT_WIRED | NOT_WIRED | Stage 5 / E5 |
| Recovery (crash → resume a coding run) | NOT_APPLICABLE | NOT_VALIDATED | NOT_VALIDATED | Final-2 pilot: no checkpoint exists |
| Trajectory | ACTIVE_OPTIONAL (`trajectory.enabled=False`) | ACTIVE_DEFAULT | ACTIVE_DEFAULT | R1-0, R1-1 |
| History compaction | NOT_WIRED | NOT_WIRED | ACTIVE_OPTIONAL | G2b-1 (negative) |
| Tool-result offload | NOT_WIRED | NOT_WIRED | ACTIVE_OPTIONAL | G3-2 (neutral) |
| Deliverable verification | NOT_WIRED | NOT_WIRED | EVALUATION_ONLY, observation only | R1-2A (0 FP, weak recall) |
| Isolated oracle | NOT_WIRED | NOT_WIRED | EVALUATION_ONLY, sandbox default | R1-2H (28/28) |

## 7. What the matrix implies

- No single path exercises every mechanism. Any claim must name its entry point.
- The reusable core across paths is: tool runtime, action gate, ledger, sandbox, scoped worker,
  trajectory. The workflow path deliberately uses none of them, and the coding paths use no
  checkpoint.
- Evaluation-only mechanisms (router, ownership, compaction, offload, verification, oracle) must not
  be described as product defaults.
