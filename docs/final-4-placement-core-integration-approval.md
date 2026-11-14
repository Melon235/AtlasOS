# Placement Core Integration — Approval Request

`PLACEMENT_CORE_INTEGRATION_APPROVAL_REQUIRED`

Status: **review complete, nothing implemented, `src/` untouched.**
Supersedes the reading of §3 of `docs/final-4-placement-integration-proposal.md`; that document's
analysis of the planner stands, this one answers what a real integration would have to change.

Design reference: contract §2.E (placement is part of Context Engineering) and §4 (placement ≠
router). Code: `src/atlasos/placement/planner.py`, `src/atlasos/workers/factory.py`,
`src/atlasos/workers/store.py`, `experiments/evaluation/runner.py`.

---

## 1. Semantic model that must survive integration

The eight objects are currently *conflated* in the evaluation path: one `WorkerStep` produces exactly
one `ScopedWorker`, whose `worker_id` **and** `task_id` are both `f"{run_id}-{step.task_id}"`
(`runner.py`, the worker-creation loop). Placement needs them separated:

| Object | Owns | Current binding | Required binding |
|---|---|---|---|
| Task id | a unit of work: assignment text, dependencies, done-definition | `{run_id}-{step}` | unchanged |
| Worker id | a principal: who acts, what it may do | same string as task id | `{run_id}-slot{n}` |
| Worker profile (`coding_worker`) | capability set and trusted system prompt | per worker, identical text today | unchanged, shared by all slots |
| `ExecutionContext` | principal, tenant, classification, `run_id`/`task_id`/`worker_id` | one per worker | **one per (worker, task)**, derived from the slot principal |
| Consumer key | the identity used by handoff/context projection to address a task's output | runtime task id | unchanged (task-scoped, not worker-scoped) |
| Action identity | `(run, task, tool, args, attempt)` in the durable ledger | task id | unchanged |
| Sandbox binding | the workspace container | one per run, shared by all workers already | unchanged |
| Ownership manifest | worker → declared paths | `{worker: task}` 1:1 | `{worker: [tasks]}` 1:N |

The rule that follows: **task identity must remain the axis for context, ledger, ownership and
handoff; only the acting principal moves to the worker.** Any design that moves task identity onto the
worker breaks attribution.

## 2. The ten questions

1. **How is the one-to-one `WorkerStep` ↔ worker relation removed?** Only in two places: the worker
   creation loop (create `k` slots once, before task specs) and the `assignee` field of the
   `AtlasTaskSpec` (from the planner's decision instead of the step's own worker). Both are evaluation
   layer. The *storage* half — `ScopedWorkerFactory.create(worker_id=…, task_id=…)` writing
   `ExecutionContext.task_id` — is `src/` and cannot be worked around.

2. **Can the existing worker pool / construction interface be reused?** `ScopedWorkerFactory` is
   reusable as-is for creating slot principals, but each call binds one `task_id`. Reusing it for a
   multi-task worker needs either one call per (slot, task) — which re-creates today's cardinality,
   i.e. no pooling — or a small extension documented in §4.

3. **How are worker profile and worker instance distinguished?** Already distinct in the type system:
   the profile is `WorkerAssignment.profile_id` + the trusted system prompt; the instance is the
   worker record (`workers.json`) with its `ExecutionContext.attributes["atlas_worker"]`. Placement
   chooses instances, never profiles. No new type is needed; what is missing is a slot identity that
   is not derived from a task.

4. **Do multiple tasks on one worker actually reuse the model session?** **No.** AnyCode runs each DAG
   task as its own agent invocation with a fresh message list; conversation state is per invocation,
   and `tool_conversations.json` is keyed by runtime instance, not by worker. Two tasks on one worker
   therefore do **not** share transcript, prompt prefix or provider KV state.

5. **If only identity is reused, what is gained?** Three things, none of them performance:
   (a) ownership and ledger attribution become per-principal, which is what the `[worker → task]`
   mapping is for; (b) the *logical* handoff count that the planner optimises becomes the number of
   references that actually cross a principal boundary, so the planner's objective can be measured
   against a real assignment instead of a hypothetical one; (c) it becomes possible to test whether
   locality-aware assignment changes anything at all. Claiming a cache or cost benefit from identity
   sharing would be false.

6. **How do context `consumer_keys` stay stable?** They are keyed by the runtime task id
   (`f"{run_id}-{step}"`), which does not change under placement. `title_by_id` and
   `worker_by_task` must be rebuilt from the same task→(slot, task) mapping so the three views
   (handoff keys, context builder, ownership manifest) cannot drift.

7. **How are `workers.json` and the assignment represented?** Add `assignments: {worker_id: [task_ids]}`
   and keep `workers` keyed by worker id with a `tasks` list. Existing readers that assume 1:1 must be
   updated in the same change; a silent schema change would invalidate every published run.

8. **How are ownership and ledger scopes kept correct?** Action identity already contains the task id,
   so ledger rows stay correct *if* the `ExecutionContext` used at tool-dispatch time carries the task
   id. Under a pooled worker that context must be per task; the manifest must declare a worker owning
   several tasks and the enforcer must resolve `worker → tasks` rather than a single task.

9. **Does AnyCode's scheduler permit this assignment?** Yes. It dispatches by `assignee` name within
   the dependency ordering and does not require distinct agents per task. Two tasks assigned to one
   agent simply run sequentially in dependency order on that agent's turn budget.

10. **How does the planner's assignment reach the real coding entry point?** `AtlasTaskSpec.assignee`
    is already the field AnyCode compiles from, so the planner's output plugs in there: build the
    `AtlasWorkflow`, call `plan_workflow`, map `decision.task_id → assignee` onto the specs. The
    blocker is step 1's storage half, not the plumbing.

## 3. Current vs target call chain

```text
today
  WorkerStep[] ──► create worker per step (task_id = worker_id)
                ──► AtlasTaskSpec(assignee = that worker)
                ──► AtlasContextEngine.run_tasks ──► AnyCode DAG

target (option B)
  WorkerStep[] ──► AtlasWorkflow ──► plan_workflow(ContextLocalityPolicy)
                ──► decisions {task → slot}
                ──► create k slot principals (worker_id = slot, profile = coding_worker)
                ──► per task: ExecutionContext(slot principal, task_id)
                ──► AtlasTaskSpec(assignee = slot)
                ──► AtlasContextEngine.run_tasks ──► AnyCode DAG (unchanged)
```

## 4. Option A — identity-only slots (evaluation layer, no `src/` change)

| Item | Change |
|---|---|
| Files | `experiments/evaluation/runner.py` (`placement_mode` setting), new `experiments/evaluation/final4/placement_arm.py` |
| State model | `workers.json` gains `assignments`; `worker_by_task` becomes `task → slot` |
| Compatibility | workflow composition untouched; `ScopedWorkerFactory` still called once per (slot, task), so `ExecutionContext.task_id` remains per task and attribution stays correct |
| Honest limit | because the factory is still called per task, the *cardinality* does not change; only the name/identity is shared. Logical handoff counts improve; runtime cost cannot, and this arm must never be reported as a performance experiment |
| Tests | plan determinism; every task lands on a capable slot; executed assignment equals `plan.decisions`; dependency still injected once; identity attributes unchanged |
| Rollback | delete the setting; `placement_mode="off"` is the default and is recorded in the run digest |

## 5. Option B — true pooled worker (`src/` change, needs approval)

| Item | Change |
|---|---|
| `src/atlasos/workers/factory.py` | `ScopedWorkerFactory.create` accepts an optional `task_ids` scope, or gains `create_task_context(worker, task_id)` returning a derived `ExecutionContext` |
| `src/atlasos/workers/store.py` | `WorkerRecord` gains `tasks: list[str]`; schema version bump; readers updated in the same commit |
| `experiments/evaluation/runner.py` | create `k` slots before specs; per task use the derived context; build `title_by_id`/`consumer_keys`/`worker_by_task` from one mapping |
| ownership | `manifest_for_task` becomes `manifest_for_assignment`; enforcer resolves `worker → tasks` |
| Risks | ledger/action attribution if any dispatch path still uses the worker-level context; ownership enforcement silently weakening (a worker with two tasks could write paths declared for the other); `workers.json` schema break for every published run |
| Tests | per-task ledger attribution with two tasks on one slot; ownership denies a write declared only for the sibling task; worker identity attributes byte-identical; dependency injection still exactly once; deterministic plan |
| Rollback | the `placement_mode` setting stays `off` by default; the schema bump is additive (`tasks` list) so old directories still parse |

Estimated size: option A ≈ 120 lines of evaluation code; option B ≈ 60 lines in `src/` + 40 in the
evaluation layer, and it needs the offline suite re-run plus a live acceptance on one multi-task DAG.

## 6. Fair P0/P1 protocol (only after approval)

Same task, same legal worker set, same public information, same tool directory and permissions, same
context configuration, same budget, same model, same sandbox.

| Arm | Placement |
|---|---|
| `P0` | `static_baseline` (or `capability_only`) over the same workflow |
| `P1` | `context_locality` over the same workflow |

Record per arm: task→worker assignment, logical handoff refs, actual worker count, parallel width,
per-worker load, context transfers, prompt/cache/cost, latency, oracle.

Interpretation rule stated in advance: a fall in logical handoff references is **not** evidence of a
cache or cost benefit. Only the measured cache hit/miss, tokens and latency may support that, and with
n ≥ 3 interleaved repetitions.

## 7. What this stage did

Nothing. `placement.policy` remains `bypass` in every Final-4 unit and is recorded as `BYPASS` in
`pilot_manifest_v3.json` and in each run's `environment.context_resolved`. No planner call, no
assignment change, no new scheduler.

Decision requested: approve Option A, Option B, both, or neither.
