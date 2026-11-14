# Placement → Coding Integration — Design for Independent Review

**Every conclusion in this document is `DESIGN_PROPOSED`.** Nothing here is `APPROVED`, nothing is
implemented, and `src/` is unmodified (`git diff 93cbd98..HEAD -- src` empty).

Supersedes nothing; it refines `docs/final-4-placement-core-integration-approval.md` where that
document was imprecise. Read this one for the A/B decision.

Design contract reference: §2.E (context-locality placement is part of Context Engineering), §4
(placement ≠ router). The contract itself is not touched.

---

## 1. Current architecture (what actually runs today)

```text
evaluation entry                        Atlas composition                     AnyCode
─────────────────                       ─────────────────                     ────────
runner_entry.build_task()  ── WorkerStep[] ──► for step in task.workers:
                                               worker = ScopedWorkerFactory.create(
                                                   worker_id=f"{run}-{step}",   ──► AgentConfig(
                                                   task_id  =f"{run}-{step}",       name="coding_worker:<run>-<step>",
                                                   assignment=step.prompt)          execution_context=ctx)
                                               worker_by_task[step] = worker
                                            ── ensure_binding(task_id=workspace_task_id,
                                                              worker_id=first_worker.worker_id)
                                            ── TaskSpec(title={run}-{step},
                                                        assignee="coding_worker:<run>-{step}")
                                            ── AtlasContextEngine.run_tasks()  ──► _run_wave_task per task
                                                                                   agent = _pool.get(assignee)
                                                                                   agent.run(prompt)
```

Facts, each read from source rather than inferred from names:

| Fact | Source |
|---|---|
| `worker_id` and `task_id` are **the same string**, one worker per step | `experiments/evaluation/runner.py:1419-1429` |
| The factory already accepts them independently | `src/atlasos/workers/factory.py:70-82` |
| `worker_name = f"{profile.profile_id}:{worker_id}"` | `factory.py:96` |
| `ExecutionContext.attributes["atlas_worker"]` holds `worker_id`, `task_id`, `worker_name` | `factory.py:203-223` |
| The sandbox is bound to a *workspace* task id, not per worker | `runner.py:1433-1438` |
| `placement.policy` is consumed only by `AtlasRuntime._place` | `src/atlasos/runtime.py:618`; every Final-4 unit records `bypass` |

---

## 2. The two options, stated precisely

The brief's "Option A" is ambiguous in a way that decides the whole question, so it is split.

### A1 — reported-only placement (`DESIGN_PROPOSED`)

The planner runs, decisions and the resulting assignment matrix are recorded and reported, but the
executed `worker_id` stays `{run}-{task}`.

- **Does it change AnyCode's task→agent assignment? No.** `assignee` strings are untouched; the DAG,
  agent cardinality and every `ExecutionContext` are byte-identical to today.
- What changes: the experiment report, and nothing else.
- Honest label: this cannot test placement. It measures the planner on paper, which the planner's unit
  tests already do.

### A2 — slot as agent (`DESIGN_PROPOSED`, higher risk)

`k` slots become `k` `AgentConfig`s; tasks are assigned to slots, so **several tasks share one Agent**.

- **Does it change AnyCode's task→agent assignment? Yes**, for real: fewer agents than tasks, and
  `assignee` becomes `coding_worker:<run>-slot{n}`.
- Consequence 1 — attribution: `ExecutionContext` is baked into `AgentConfig` at construction and read
  once into `RunnerOptions` (`anycode/core/agent.py:85`). One slot therefore has **one** `task_id`, so
  the ledger's `task_id`/`worker_id` fields (`src/atlasos/ledger/bridge.py:96-98`) degrade from
  per-task to per-slot.
- Consequence 2 — concurrency: within a wave AnyCode runs tasks with `asyncio.create_task`
  (`orchestrator.py:533`) and stores results in `agent_results[assignee]` (`:541`). Two tasks of one
  slot in the same wave **collide on that map**. Placement would have to serialise a slot's tasks or
  the executed width silently changes.
- Consequence 3 — no session reuse: `Agent.run()` is *"Execute prompt as a standalone conversation
  (history is ignored)"* (`agent.py:101-104`), so sharing an Agent does not share a transcript.

### B — shared principal, per-task context (`DESIGN_PROPOSED`, AnyCode-transparent)

`worker_id` is chosen by the planner and shared by several tasks; `task_id` stays unique; **one Agent
per task**, so AnyCode is untouched.

- **Does it change AnyCode's task→agent assignment? No.** `assignee` stays `{run}-{task}`; the only
  change is which `worker_id` is stamped into that task's `ExecutionContext`.
- Attribution stays per task (each task has its own context), ownership stays per task, no wave
  collision, no shared mutable state.
- What is genuinely new: a worker principal that owns several tasks, so `workers.json` and
  `ScopedWorkerFactory.resume` must stop assuming exactly one task per worker record.
- Because AnyCode is untouched, **B cannot change model cost or cache behaviour.** Its measurable
  effects are the assignment itself, logical handoff references, per-worker load, and correct
  attribution. That is the honest ceiling.

### 2.1 Object mapping (the ten requested objects)

| # | Object | A1 | A2 | B |
|---|---|---|---|---|
| 1 | Task id | unchanged `{run}-{task}` | unchanged | unchanged |
| 2 | Worker id | unchanged (= task id) | `{run}-slot{n}` | `{run}-slot{n}` |
| 3 | Logical placement slot | recorded only | = worker id = agent name | = worker id (not an agent) |
| 4 | AnyCode `AgentConfig` identity | unchanged, 1 per task | 1 per slot, N:1 to tasks | unchanged, 1 per task |
| 5 | Executing principal identity | task-scoped | **slot-scoped** | slot-scoped principal, task-scoped context |
| 6 | Task-bound `ExecutionContext` | unchanged | **lost** (one per slot) | preserved (one per task) |
| 7 | Tool consumer key | unchanged (task id) | unchanged, but shared across the slot's tasks | unchanged |
| 8 | Action identity | unchanged | `task_id` becomes the slot | unchanged |
| 9 | Ownership scope | 1 worker : 1 task | 1 worker : N tasks, **no per-task split** | 1 worker : N tasks, enforced per task |
| 10 | `WorkerStore` key | `worker_id` (unique per task) | `worker_id` (≥1 task) | `worker_id` (≥1 task) — needs schema |

Rows 5, 6 and 9 are why A2 is the risky option, and why B is the only one of the three that keeps the
task as the axis for correctness while still making the assignment real.

---

## 3. AnyCode integration seam (verifiable)

| Item | Value |
|---|---|
| Distribution | `anycode-py==0.9.0` (PyPI, `pyproject.toml:13`), import name `anycode` |
| Installed location | `.venv/Lib/site-packages/anycode` (193 `.py` files) |
| Package source digest (sha256 over sorted `anycode/**/*.py`, path+bytes) | `edef11b297517baf8cc11fc53a8808c41d8b94f59effd7b31a453093a5e8e51e` |
| `RECORD` sha256 | `59277760541f0318834cf267df5eca419f5404b82213035bf18e33fa3ed91505` |
| Public source | not vendored here; the digest above identifies the installed tree, and `anycode-py` publishes 0.9.0 on PyPI. **A public repository URL is `UNVERIFIED`** — no link is asserted because none was confirmed. |

Verified behaviours, all by reading the installed source:

1. **Task → agent.** `TaskSpec.assignee` (`orchestrator.py:84-90`) is carried into `Task.assignee`;
   `_run_wave_task` uses `assignee = task.assignee` verbatim and fails the task when it is empty
   (`:677-680`).
2. **Scheduler respects pre-assignment.** `Scheduler.schedule` only considers
   `status == "pending" and not t.assignee` (`core/scheduler.py`), so an explicit assignee is never
   re-assigned. `auto_assign` then writes nothing for those tasks.
3. **Per-task agent construction is pooled by name.** `_resolve_task_agent` →
   `self._pool.get(assignee) or self._build_agent_for_team(assignee, team)` (`:829-840`), so one
   `Agent` instance per assignee name for the engine's lifetime.
4. **`AgentConfig` is reusable** but frozen into `RunnerOptions` once per `Agent`
   (`agent.py:67-99`); reusing a config across tasks is safe, mutating a live one is not.
5. **A fresh conversation per task.** `Agent.run` ignores history (`agent.py:101-104`).
6. **Same-agent concurrency within a wave is possible** (`orchestrator.py:533`) and its results are
   keyed by assignee (`:541`) — the collision described in §2.
7. **Safe seam without touching AnyCode.** Three existing extension points are enough for B:
   overriding `_build_task_prompt` (already used by `AtlasContextEngine`), supplying `Execution_
   Context` through `AgentConfig`, and choosing `assignee`/`TaskSpec`. No AnyCode file needs editing
   for B. A2 needs nothing from AnyCode either, but breaks Atlas's own attribution.

---

## 4. Security impact matrix

`1:1?` = does the current code assume one worker ↔ one task?

| Mechanism | Location | 1:1? | A1 | A2 | B | Risk if changed | Verification |
|---|---|---|---|---|---|---|---|
| `ScopedWorkerFactory.create` | `workers/factory.py:70-124` | accepts independent ids, no assumption | none | none | none | — | pass `worker_id≠task_id`; assert `attributes["atlas_worker"]` |
| `ScopedWorkerFactory.resume` | `factory.py:127-164` | **yes** (`metadata["task_id"] != record.task_id` → reject) | none | **breaks** | **breaks** | recovering a worker serves the wrong task | resume with 2 tasks; assert per-task equality |
| `WorkerInstance` | `factory.py:104-118` | no (holds both ids) | none | none | none | — | serialise/deserialise round trip |
| `WorkerStore` / `workers.json` | `workers/store.py` (keyed by `worker_id`) | **yes** (one record per id) | none | **overwrite** | **overwrite** | silent loss of a worker's task history | reload after 2 tasks; assert both listed |
| `AtlasContextEngine` | `integrations/anycode_engine.py` | no (keys by task) | none | none | none | — | existing 13 tests |
| `AtlasTaskPromptBuilder` | `context/task_prompt.py` | no (`agents_by_name` keyed by name) | none | none | none | — | per-task render evidence |
| `consumer_keys` (handoff) | `runner.py` (runtime task ids) | no | none | none | none | — | dependency injected once |
| `RunToolState` | `runner.py` (run-scoped) | no | none | none | none | — | tool list per run |
| Action identity | `ledger/identity.py:29-40` | no (from idempotency key) | none | **loses task axis** | none | two tasks map to one action | two tasks, distinct `action_id` |
| Idempotency key | `tools/runtime.py:291,584`; `anycode/tools/executor.py:158` | no | none | **may replay across tasks** | none | Task B reuses Task A's cached result wrongly | distinct keys per task; assert no cross-claim |
| Durable ledger | `ledger/bridge.py:92-99`, `store.py` (keyed `action_id`+`attempt_id`) | no, but reads `task_id`/`worker_id` from context | none | **wrong task recorded** | none (context per task) | mis-attributed audit trail | assert `record.task_id` per task |
| Ownership manifest | `runner.py` worker→path, `sandbox/ownership.py` | **yes** | none | **breaks** | **breaks** | a worker could write a sibling task's declared paths | 2-task worker; deny sibling write |
| Approval / suspension | `_coding_policy()` has no `require_human` | n/a today | none | none | none | approval of Task A reused for B — only if approvals are ever enabled | assert approval keyed by `task_id`, not `worker_id` |
| Trusted recovery | `trusted_recovery.py`, `factory.resume` | **yes** | none | **breaks** | **breaks** | restoring the wrong binding onto a task | recover 2 tasks under one worker |
| Sandbox binding | `sandbox/binding.py:303`; bound to `workspace_task_id` | no (run-scoped) | none | none | none | per-task isolation would be lost if binding were per worker | `binding.task_id` stays the workspace id |

Reading of the matrix: **A1 changes nothing, A2 breaks four mechanisms (resume, store, ledger
attribution, ownership) plus the wave-result map, and B breaks two (resume, store) with a contained,
testable fix.** No option leaks permissions by itself; the risk is mis-attribution and lost
per-task enforcement.

---

## 5. Persistence compatibility

| Artifact | A1 | A2 | B |
|---|---|---|---|
| `workers.json` schema | unchanged | 1 record per slot; task history collapses | **needs** `tasks: list[str]` (additive) |
| Existing published runs | readable | readable but not comparable (different cardinality) | readable |
| `result.json` `worker_results` | unchanged | keyed by slot → fewer entries | unchanged |
| Ledger `action_ledger` rows | unchanged | `task_id` becomes slot | unchanged |
| Trajectory records | unchanged | task field becomes slot | unchanged |

An additive `tasks` list keeps old directories parseable; a silent 1:1→1:N reinterpretation of the
existing key would not. Any schema change must bump a version field and be read back by the same
commit.

---

## 6. Minimal implementation scope (`DESIGN_PROPOSED`)

### B (the only option that keeps the task axis)

| File | Function | Current | Proposed | Callers |
|---|---|---|---|---|
| `src/atlasos/workers/store.py` | `WorkerRecord` | `worker_id`, `task_id` | add `tasks: list[str]`, keep `task_id` as the creating task | `factory.create/resume`, `runner` |
| `src/atlasos/workers/factory.py` | `ScopedWorkerFactory.resume` | validates one task | validate the *requested* task against `record.tasks` | `trusted_recovery`, tests |
| `src/atlasos/workers/factory.py` | `create` | already independent ids | unchanged signature; document that `worker_id` may repeat | `runner.py:1419` |
| `experiments/evaluation/runner.py` | worker creation loop | one per step | one per (slot, task) with the slot's `worker_id` | `run_sample` |
| `experiments/evaluation/final4/placement_arm.py` | new | — | build `AtlasWorkflow`, `plan_workflow`, map decisions | `runner.py` |
| `experiments/evaluation/runner.py` | `RunnerSettings` | no placement field | `placement_mode: off\|context_locality` in the digest | `runner_entry` |

Is the earlier "60 + 40 lines" estimate complete? **No.** It omitted: the `WorkerStore` schema plus
read-back (≈40 lines with tests), the `resume` validation change, the ownership manifest's
`worker → tasks` resolution, and the tests for each. A realistic B estimate is **≈150 lines of `src/`
including tests, plus ≈120 lines of evaluation code**.

### A2 (only if the owner wants AnyCode's assignment itself to change)

Needs, in addition to the evaluation-side work: a rule that a slot's tasks never share a wave (or an
explicit acceptance that executed width changes), and an accepted loss of per-task ledger/ownership
attribution. It is listed for completeness; this document does not recommend it.

---

## 7. Actual-assignment evidence plan

Every arm records, per task: `task_id`, chosen `worker_id`, `assignee` string, `ExecutionContext`
digest, the planner decision that produced it, the wave index, and whether the executed assignment
equals the planned one. A run is only admissible as a placement experiment if that equality holds and
the executed worker count matches the plan. Assertion, not report-only.

---

## 8. Alternatives considered

1. **Change AnyCode to key `agent_results` by task id.** Rejected: modifies the runtime under test and
   is unnecessary for B.
2. **Reuse `ProjectSandboxManager` per worker.** Rejected: the sandbox is run-scoped today
   (`runner.py:1433`), and per-worker sandboxes would multiply cost with no measured benefit.
3. **Let the router and placement share one decision point.** Rejected by the contract (§4): the router
   decides *whether* to delegate, placement decides *to whom*.
4. **Skip placement entirely because no option shows a cost benefit.** Rejected: placement is a
   contract mechanism; the correct move is to state its ceiling, not to delete it.

---

## 9. Unverified assumptions

1. That a pooled `worker_id` is operationally useful at all — no experiment has tested it.
2. That `WorkerStore` is the only persistence keyed by `worker_id` (audit covered `workers/`, `ledger/`,
   `sandbox/`; a full grep for `workers.json` readers is still owed in G3).
3. That `approvals` never serialize `worker_id` into a key today; `_coding_policy()` has no
   `require_human` rule, so the path is unreachable and the assumption is untested.
4. That the ownership manifest can express 1:N without weakening enforcement; not prototyped.
5. That AnyCode's `_pool` keys solely on `assignee` name in 0.9.0 (read once; no test of two agents
   with the same name).
6. The PyPI source link for 0.9.0 — the digest is verifiable, the URL is not asserted.

---

## 10. Proposed gate boundaries (review decides)

| Gate | Scope | Exit criterion |
|---|---|---|
| **G1** | Task / Worker / task-bound `ExecutionContext` model: `WorkerRecord.tasks`, `resume` validation, `workers.json` read-back | offline tests green; no behaviour change with `placement_mode=off`; existing runs still parse |
| **G2** | Worker pool identity, adapter over the existing planner, assignment legality and parallel constraints | plan determinism; every task on a capable slot; executed assignment == planned |
| **G3** | Wire into the coding entry point, trusted identity and Reliable-Execution compatibility | ledger/ownership per task proven with a 2-task worker; no cross-task approval or replay |
| **G4** | Context Plane integration, real dependency transfer, recording provider + local OpenSandbox smoke | dependency delivered once per consumer; no duplicate injection |
| **G5** | Full regression, support-scope update, runtime freeze, final independent review | reviewer sign-off |

Each gate: implement → test → commit → push → independent review → **stop**. No gate starts before the
owner forwards `APPROVED — NEXT GATE`.

---

## 11. What this document does not claim

- It does not claim placement improves cost, latency or cache. No option can: placement never changes
  what the model sees, and B does not change AnyCode at all.
- It does not claim A2 is viable; it is described so the owner can reject it with reasons.
- It does not claim the B line estimate is complete; §6 lists what the earlier estimate omitted.
- It does not choose. **`AWAITING_INDEPENDENT_ARCHITECTURE_REVIEW`.**
