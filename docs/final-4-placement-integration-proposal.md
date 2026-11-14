# Final-4 — Context-locality Placement: Support Boundary and Integration Proposal

Design reference: `docs/architecture-design-contract.md` §2.E (placement is part of Context
Engineering) and §4 (placement ≠ router). Code reference: `src/atlasos/placement/`,
`src/atlasos/runtime.py::_place`, `src/atlasos/workers/`.

Status: **analysis complete, integration NOT performed**. The change required to reach the coding and
evaluation paths is a core assignment change, which this stage is not authorised to make
(§13 of the stage instruction). This document is the minimal proposal the owner can approve or reject.

## 1. Answers to the eight questions

**1. What are the planner's inputs and outputs?**

`placement/planner.py::plan_workflow(workflow, policy, weights) -> PlacementPlan`.

- **Input** — an `AtlasWorkflow`: tasks with `depends_on`, `required_capabilities`, `required_context`,
  and agents with `name` + `capabilities`. Nothing else: no model call, no clock, no randomness.
- **Output** — a new `AtlasWorkflow` with `task.assignee` rewritten, one `PlacementDecision` per task
  (chosen agent, locality score, logical handoff refs), and a `PlacementSummary`
  (`dependency_agent_switches`, `mean_assignment_locality`, `total_logical_handoff_refs`,
  `assignment_distribution`).

**2. Does it change the actual task-to-worker assignment?**

Yes, and only there. The placed workflow is compiled by `compile_workflow`, so AnyCode's scheduler
executes the rewritten assignees. It is *pre-placement*: the DAG and the wave structure are decided
before execution, and AnyCode still owns dispatch (planner docstring, lines 1–6).

**3. How is context locality computed?**

`PlacementPolicy.choose(...)` scores each candidate agent from `AgentContextState`
(`consumed_task_ids`, `produced_task_ids`, context refs) against `TaskRequirements.required_context`,
plus the agent's current `load`. `PlacementWeights(locality=1.0, load=0.2)` combines them.
`summarize_plan` then reports two different things, deliberately not merged:
`dependency_agent_switches` (how often a dependency edge crosses an agent boundary) and
`total_logical_handoff_refs` (how many logical refs would cross). Neither is a cache measurement.

**4. Is it compatible with the trusted worker profile?**

Partly, and this is where it breaks.

- Compatible: placement only sets `assignee`; the worker's `ExecutionContext` (principal, tenant,
  classification, `run_id`/`task_id`/`worker_id`) comes from `ScopedWorkerFactory`, so placement
  cannot forge identity.
- Not compatible: `ScopedWorkerFactory` is keyed by **the task being assigned**
  (`worker_id=f"{run_id}-{step.task_id}"`), and the evaluation runner creates one worker per frozen
  `WorkerStep` *before* any placement decision. A placement that reassigns tasks to a **shared pool**
  of workers would change `workers.json` cardinality, `worker_id` semantics, the handoff
  `consumer_keys` map and the ownership manifest's `worker → task` declaration.

**5. Does it change parallel width?**

Only indirectly. The planner assigns at most one task per agent per wave and raises
`no available agent for task X in wave N` when it runs out, so with more agents it can *widen* the
executed wave relative to a single assigned worker; with fewer, it serialises. It never changes the
DAG itself. In the current Coding and Evaluation compositions the width is fixed by the number of
`WorkerStep`s, so placement would add nothing there.

**6. Does it affect resource permission or worker identity?**

No. Permissions come from the action policy/catalog and the sandbox binding; identity comes from the
factory. A capability filter inside the policy is the only check placement performs, and it is not a
security boundary.

**7. Is there a safe reuse seam in the current Coding path?**

No. Coding is a one-task composition (`app/coding.py` builds one `AtlasTaskSpec` and one worker), so
there is nothing to place. Evaluation is the only path with a real DAG, and it constructs workers
per step; a genuine placement arm needs the worker pool described in answer 4.

**8. Is `placement.policy` actually consumed?**

Once: `runtime.py::_place` (lines 618–644) reads `placement.policy`, calls
`plan_workflow` for `static_baseline` / `capability_only` / `context_locality`, and returns the placed
workflow plus `asdict(decision)` records. `bypass` returns the workflow unchanged. **Neither the Coding
app nor the Evaluation runner calls `_place` or `plan_workflow`** — they build task specs and workers
directly, so `placement.policy` in a YAML file that the evaluation runner loads is inert.

## 2. Why this stage does not wire it

Wiring placement into the evaluation path requires, at minimum:

1. a worker **pool** constructed before assignment (`workers.json` semantics change);
2. a mapping from `WorkerStep` → pool member that survives `title_by_id`, `consumer_keys`,
   `worker_by_task` and the ownership manifest;
3. a re-definition of what `worker_id` means when two tasks share one worker;
4. a decision on whether a reassigned worker may inherit another worker's disclosed tool state
   (`RunToolState` is run-scoped today, which helps, but the *assignment* text is per worker).

Items 1–3 are core assignment semantics, not evaluation wiring. Doing them inside a pilot would
change the runtime under measurement, which is exactly what the design contract forbids.

## 3. Minimal proposal (for owner approval)

Scope: evaluation layer only, no `src/` change.

| Step | Change | Files |
|---|---|---|
| P-a | Add `placement_mode` to `RunnerSettings` (`off` \| `context_locality`) and record it in the digest | `experiments/evaluation/runner.py` |
| P-b | Build a pool of `k` workers with profile `coding_worker`, `worker_id=f"{run_id}-slot{n}"`, before task specs are created | `experiments/evaluation/runner.py` |
| P-c | Convert the frozen `WorkerStep` list into an `AtlasWorkflow` (tasks carry `required_context` = direct dependencies) and call `plan_workflow(..., ContextLocalityPolicy())`; use the returned assignees to bind `step → slot`; record `plan.summary` and every decision | new `experiments/evaluation/final4/placement_arm.py` |
| P-d | Keep `title_by_id`, `consumer_keys` and `worker_by_task` derived from the *same* mapping so handoff, context plane and ownership stay consistent | `experiments/evaluation/runner.py` |
| P-e | Emit `metrics.extra["placement"] = {policy, summary, decisions, plan_digest}` | `experiments/evaluation/runner.py` |

Risks:

- two tasks on one worker means one worker writes two modules; a task-level turn must then contain
  both assignments, which changes the prompt under measurement;
- `WorkerStore` and the ownership manifest assume one task per worker; the manifest builder would have
  to declare a worker owning two tasks;
- the handoff `consumer_keys` map is keyed by runtime task id `f"{run_id}-{step.task_id}"`; a pooled
  worker changes the mapping, and the batch read/deny metrics would no longer be comparable to the
  published ones.

Verification for such an arm: a deterministic test that (a) the plan is deterministic for a fixed
input, (b) every task still lands on a capable worker, (c) the executed assignment equals
`plan.decisions`, (d) the Context Plane still delivers each dependency exactly once, (e) worker
identity attributes are unchanged.

Compatibility impact: the frozen R0/R1/R2/X0/X1 matrix must keep `placement_mode="off"` and record
`BYPASS` explicitly, exactly as `pilot_manifest_v2.json` does (`placement.policy: bypass`).

Experiment boundary (only after approval): P0 = current fixed assignment, P1 = context-locality
assignment, same DAG, same capabilities, same public information, same tools, same context
configuration. Report logical handoff, actual assignment, parallel width and per-worker load. A fall in
logical handoff references is **not** evidence of a physical cache benefit.

## 4. What is claimed today

- The planner is implemented, deterministic and unit-tested.
- Its measured effect is on *logical* handoff references: −50 % / −66.7 % / −80 % at chain length
  2 / 3 / 5 (S6 Table C), with the absolute reduction constant at 2 because only the two cross-chain
  merge inputs still cross an agent boundary.
- It is reachable from the workflow composition through `placement.policy`.
- It is **not** reachable from the Coding app or the Evaluation runner. Every Final-4 unit records
  `BYPASS`.
