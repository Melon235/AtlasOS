# Placement → Coding Integration — Design for Independent Review (G0-R1)

Status: `AWAITING_INDEPENDENT_REVIEW`. Revision G0-R1, responding to review verdicts on
`76a427541617c1ff4993ef3ad3947639f802b6d8`.

Every conclusion here is `DESIGN_PROPOSED`. Nothing is implemented; `src/` is unmodified.

| Review item | Verdict | Consequence for this document |
|---|---|---|
| Option A1 (reported-only) | **REJECTED** | kept for the record only (§12) |
| Option A2 (slot as agent) | **REJECTED** | kept for the record only (§12) |
| Option B (shared `worker_id` + `WorkerStore.tasks`) | **NEEDS_REDESIGN** | replaced by **B′** (§3), and its three false source claims are corrected in §2 |
| B′ (run-scoped slot + task-scoped execution instance) | adopted as the design to review | §3–§9 |

Design contract reference: §2.E (placement is part of Context Engineering), §4 (placement ≠ router).
The contract is not modified.

---

## 1. What changed in this revision

1. Three source errors in the previous revision are corrected with exact call sites (§2).
2. B is withdrawn; B′ is specified: **one worker execution instance per task, unchanged identity
   semantics, plus a run-scoped placement slot that participates in construction, accounting and the
   frozen plan** (§3).
3. The slot-capacity invariant is analysed against the real seams and **declared not enforceable
   today**, with three options for the owner rather than a silent workaround (§3.5).
4. Two placement policies are shown to report incomparable counters; a policy-independent outcome
   evaluator plus an acceptance test is specified (§5).
5. Upstream provenance is corrected to the published artifacts (§6), keeping the wheel hash and the
   local tree digest distinct.

---

## 2. Three source corrections

### 2.1 Worker identity, agent name and assignee are one value, not three

The previous revision said B could "share `worker_id` while each task keeps its own
`AgentConfig.name`, with `factory.create` unchanged". **Those three statements cannot hold together.**
The chain is:

```text
src/atlasos/workers/factory.py:96     worker_name = f"{profile.profile_id}:{actual_worker_id}"
src/atlasos/workers/factory.py:98     AgentConfig(name=worker_name, ...)
experiments/evaluation/runner.py:1498 assignee=worker_by_task[step.task_id].worker_name
```

`assignee` **is** `worker_name` **is** derived from `worker_id`. Sharing `worker_id` between two tasks
therefore produces the same `worker_name`, hence the same `AgentConfig.name`, hence one AnyCode agent —
which is exactly the rejected A2. The only way to keep separate agents is separate `worker_id`s.

### 2.2 `agent_results` is overwritten by key, across waves as well as within a wave

```text
anycode/core/orchestrator.py:541      agent_results[assignee] = result
```

This single assignment sits inside the **run-level** wave loop, not inside a per-wave accumulator. Two
tasks with the same `assignee` therefore overwrite each other **whether or not they share a wave** —
serial execution in different waves overwrites just as surely as concurrent execution in one wave.

`TaskQueue` keeps per-task output, so the run can still appear "complete"; but the Atlas evaluation
runner collects tool calls and worker results from `result.agent_results`
(`experiments/evaluation/runner.py:1593` → `_collect_tool_calls`). The consequence of reuse is
therefore **loss of evidence**, not a crash: `worker_results` would silently contain fewer workers
than the DAG, and tool calls of the overwritten tasks would vanish from the report. Any design that
shares an agent name must state this, and B′ avoids it by keeping names unique.

### 2.3 `WorkerInstanceRecord` is task-bound in five fields, and the principal is not the worker

```text
src/atlasos/workers/models.py:99-112
    worker_id, worker_name, profile_id, task_id, parent_agent,
    assignment, task_context, execution_context, rendered_context_hash, created_at, status
```

`assignment`, `task_context`, `execution_context` and `rendered_context_hash` are all bound to one
task. A `tasks: list[str]` field would not represent several trusted execution records; it would only
hide that one record is being reused.

Second correction: `ExecutionContext.principal` is **inherited unchanged** from the parent —

```text
src/atlasos/workers/factory.py:214-228
    attributes = safe_parent_attributes(dict(parent_context.attributes))
    attributes["atlas_worker"] = metadata.nested_attributes()
    attributes["worker_id"] = worker_id          # metadata, not principal
    return parent_context.model_copy(update={"delegation": (), "attributes": attributes})
```

`worker_id` is Atlas worker metadata carried in `attributes`. It is **not** the principal, and the
previous revision's phrasing ("the worker's principal") conflated the two. B′ therefore leaves the
principal untouched, as required.

---

## 3. B′ — Run-scoped Slot + Task-scoped Execution Instance

### 3.1 Identity separation (the operative table)

| Identity | Owner | Form | Semantics | Under B′ |
|---|---|---|---|---|
| `task_id` | Atlas evaluation | `{run}-{task}` | the unit of work | unchanged |
| `placement_slot_id` | Host | `{run}-slot{n}` | run-scoped **logical execution resource** | new |
| `worker_id` | `ScopedWorkerFactory` | `{run}-{task}` | task-scoped worker execution instance | **unchanged** |
| `worker_name` / `AgentConfig.name` / `assignee` | factory → AnyCode | `coding_worker:{run}-{task}` | unique execution identity | **unchanged** |
| `ExecutionContext.principal` | parent context | inherited | trusted principal | **unchanged** |

Several tasks may share a `placement_slot_id`; no two tasks ever share a `worker_id`,
`AgentConfig.name`, or `ExecutionContext`. This is the review's requested separation, and it removes
the three problems of §2 by construction rather than by compensation.

### 3.2 Trusted slot pool

A slot is created by the host before any task runs:

| Field | Meaning | Source of truth |
|---|---|---|
| `slot_id` | run-scoped id, `{run}-slot{n}` | host |
| `profile_id` | which trusted `WorkerProfile` executes tasks on this slot | `WorkerCatalog` (host-owned, frozen) |
| `capabilities` | the profile's *declared* capability surface used for planner eligibility | derived from the profile, never from task text |
| `max_active_tasks` | v1 fixed at `1`; see §3.5 for enforceability | host |

What a slot is **not**: a process, a GPU worker, a remote node, a conversation, a tool grant. It owns
no history and adds no permission.

**Do not read `WorkerProfile.base_tools` as an authorization boundary.** Two facts from source:

- `WorkerProfile` has `base_tools`, `operating_scope`, `max_turns`, `can_delegate` — there is **no**
  generic `capabilities` field (`workers/models.py:26-57`).
- `AtlasContextEngine.build_agent` replaces the agent's tool list with the runtime's shared tool table,
  so `AgentConfig.tools` set at factory time is not what the agent ultimately holds.

Therefore the trusted mapping is:

| Layer | Decides | Enforced by |
|---|---|---|
| `profile_id` | role, system prompt, `operating_scope`, `max_turns`, declared tool preference | `ScopedWorkerFactory.create` → `AgentConfig` + `attributes` |
| shared tool table | which tools exist at runtime | `AtlasToolRuntime` / `AtlasContextEngine.build_agent` |
| action policy + gate | which calls are permitted per task | `ActionSafetyGate` (`_coding_policy()`), ledger |
| sandbox binding | where calls execute | `ProjectSandboxManager.ensure_binding` |

A slot constrains the **first** row only. Planner capability and `base_tools` are **eligibility
hints**, not authorization; authorization stays where it is today.

### 3.3 Frozen assignment plan

```text
task_id → placement_slot_id        (exactly one legal slot per task)
```

The plan is produced by the existing planner (`placement/planner.py::plan_workflow`) with a policy
chosen by configuration, then **frozen by the host** before any worker exists, and recorded with a
digest. It is not recomputed during execution. The DAG is not modified; AnyCode's scheduler still
decides the order.

### 3.4 Task execution binding

```text
Task A → Slot 1 → Worker A (worker_id {run}-a, name coding_worker:{run}-a, own ExecutionContext)
Task B → Slot 1 → Worker B (worker_id {run}-b, name coding_worker:{run}-b, own ExecutionContext)
Task C → Slot 2 → Worker C (worker_id {run}-c, …)
```

Construction order, and this order matters:

1. resolve slot from the frozen plan for this task;
2. build the worker with `WorkerAssignment(profile_id=slot.profile_id, task=…)`, and **reject** the
   task if the planner-supplied `profile_id` disagrees with the slot's profile;
3. assert `worker.profile.profile_id == slot.profile_id` on the built instance (not on the request);
4. record `(task_id, slot_id, worker_id, worker_name)` in the run's evidence.

Prohibited by construction: creating a worker first and stamping `placement_slot_id` afterwards.

### 3.5 Slot admission — what is enforceable today, and what is not

The reviewer's operational requirement is that B′ must not degrade into a report label. Three
candidate enforcement points were inspected:

| Seam | Verdict |
|---|---|
| `AtlasContextEngine._build_task_prompt` override | synchronous, runs before the agent exists; cannot hold a slot |
| `anycode.core.pool.AgentPool` | **global** semaphore only (`core/pool.py:29`); no per-agent or per-slot capacity |
| `OrchestratorConfig` concurrency | global; setting it to 1 serialises every arm and destroys the width being measured |
| override `_run_wave_task` | would copy the main execution loop — forbidden by the brief |
| inject dependencies to force slot order | changes the DAG — forbidden by the brief |

Also relevant: the agent pool refuses duplicate names (`AgentPool.add` raises), which is another
reason §3.1 keeps names unique.

**Conclusion — declared blocker, not a workaround:** `max_active_tasks = 1` is **not enforceable at
any existing AnyCode seam**. B′ v1 must therefore either (a) declare capacity unenforced and drop it
from every claim, (b) accept a global cap (distorts all arms, not recommended), or (c) request a
minimal AnyCode change adding a pre-dispatch hook — a change to a **vendored dependency**, not to
`src/atlasos`, and therefore a separate approval. This stage does none of them.

**Second, sharper consequence.** With capacity unenforced *and* a homogeneous slot pool (every slot
holding `coding_worker`), B′ produces **no runtime difference at all** — it becomes A1 plus correct
bookkeeping. B′ has real runtime teeth only when the pool is heterogeneous (different profiles,
scopes, `max_turns`, tool preferences) or when capacity is enforced. G2 must therefore begin by
defining a heterogeneous slot pool; otherwise the honest outcome of the experiment is "no difference,
as designed".

---

## 4. Reliable-execution compatibility

| Mechanism | Assumption today | Under B′ | Action |
|---|---|---|---|
| `WorkerStore` / `workers.json` | one record per worker, already per task | unchanged (one worker per task) | none |
| `ScopedWorkerFactory.resume` | exact `worker_id` + `task_id` match (`factory.py:132-145`) | unchanged | none |
| Ownership manifest | per task, declared paths per worker | unchanged | none |
| Durable ledger | `run_id`/`task_id`/`worker_id` from the task's context (`ledger/bridge.py:96-98`) | unchanged | none |
| Action identity | from the trusted idempotency key | unchanged | none |
| `AgentConfig.name` uniqueness | one agent per task | unchanged | none |
| Sandbox binding | run/workspace scoped | unchanged | none |
| Approval / suspension | per task, none configured today | unchanged | none |
| Slot plan persistence | — | new, sidecar only | §4.1 |

Net: **B′ requires no change to `src/atlasos`.** The only new persisted artifact is the slot plan.

### 4.1 Persistence and recovery boundary

- Location: `run_dir/placement_plan.json`, next to `context.json`; **not** inside `WorkerStore`,
  because a plan is not a worker record.
- Content: slot table, `task → slot` map, policy name + version, planner digest, plan digest.
- Integrity: digest recomputed on load; mismatch ⇒ `PLACEMENT_PLAN_CORRUPT`.
- Recovery: on resume, a task whose plan is unavailable or whose slot no longer exists ⇒ fail closed
  (`PLACEMENT_PLAN_UNAVAILABLE`). No implicit re-planning, because a re-plan would silently change the
  assignment under measurement.
- What this does **not** claim: that Atlas recovery understands slots. Ordinary coding recovery
  support is unchanged; placement mode simply refuses to resume without its frozen plan.

---

## 5. Placement outcome metrics (policy-independent)

Problem confirmed in source:

```text
src/atlasos/placement/policies.py:94-96   CapabilityOnlyPolicy: matched_context_count = 0
                                                             logical_handoff_refs = len(required_context)
src/atlasos/placement/policies.py:166-168 ContextLocalityPolicy: computes real matches
```

The two policies report `logical_handoff_refs` under different definitions, so comparing their own
counters is meaningless. Policy scores stay as they are (they *rank candidates*); what must be unified
is the evaluation of a **final assignment**.

`AssignmentOutcome` (G2, policy-agnostic — takes only the assignment and the workflow):

| Field | Definition |
|---|---|
| `cross_slot_dependency_edges` | dependency edges whose producer and consumer are on different slots |
| `logical_context_handoff_refs` | `(task, required_context item)` pairs whose providing task sits on a different slot |
| `slot_load` | tasks per slot, plus peak concurrent tasks observed |
| `legal_parallel_width` | maximum antichain width of the DAG restricted to the slot assignment |
| `plan_matches_execution` | executed `(task → slot)` equals the frozen plan |

Acceptance test (G2): **two policies that produce the same assignment must produce identical
`AssignmentOutcome`**, byte for byte. This is the guard against re-introducing policy-specific
accounting.

Boundary on history: the Stage-6 planner results were computed with the per-policy counters and are
**not** recomputable into this definition; they stay as published, labelled with their original
definition, and are not used for the P0/P1 comparison.

---

## 6. Upstream provenance (corrected)

| Item | Value |
|---|---|
| Package | `anycode-py==0.9.0` (PyPI) |
| Release page | https://pypi.org/project/anycode-py/0.9.0/ |
| Published wheel SHA-256 | `c93c27d398634b4327225f9acd2ac67ae8651606a06a85d292d2d84696a9f8db` |
| Published source commit | `ffdc7d28e8d5ceefedf3e490ccbe37499eb6eec8` |
| Source tree | https://github.com/Quantlix/anycode/tree/ffdc7d28e8d5ceefedf3e490ccbe37499eb6eec8 |
| Local unpacked tree digest (sha256 over sorted `anycode/**/*.py`, path+bytes) | `edef11b297517baf8cc11fc53a8808c41d8b94f59effd7b31a453093a5e8e51e` |
| Local `RECORD` sha256 | `59277760541f0318834cf267df5eca419f5404b82213035bf18e33fa3ed91505` |

The wheel hash and the local tree digest are **different artifacts** and must never be used
interchangeably: one covers the distributed archive, the other covers an unpacked installation on this
machine. Reading of behaviour in §2 and §3 is against the installed tree.

Behaviours confirmed from that source (all read, not inferred from names): `TaskSpec.assignee` carried
verbatim (`orchestrator.py:677-680`); scheduler skips already-assigned tasks
(`core/scheduler.py`, `pending = [... and not t.assignee]`); agents pooled by name
(`:829-840`); `Agent.run` ignores history (`agent.py:101-104`); results keyed by assignee (`:541`);
pool concurrency is global (`core/pool.py:29`).

---

## 7. Minimal implementation interfaces (G1–G3, not implemented)

```python
# experiments/evaluation/final4/placement_slots.py   (new, evaluation layer only)

@dataclass(frozen=True)
class PlacementSlot:
    slot_id: str                      # f"{run_id}-slot{n}"
    profile_id: str                   # must exist in WorkerCatalog
    max_active_tasks: int = 1         # declared; enforcement per §3.5
    capabilities: tuple[str, ...] = ()  # profile-derived eligibility hints only

@dataclass(frozen=True)
class FrozenAssignmentPlan:
    slots: tuple[PlacementSlot, ...]
    assignments: Mapping[str, str]    # task_id -> slot_id
    policy: str
    policy_version: str
    plan_digest: str

def build_slot_pool(*, run_id: str, catalog: WorkerCatalog,
                    profile_ids: Sequence[str]) -> tuple[PlacementSlot, ...]: ...
    # raises PlacementSlotError on unknown profile or duplicate slot profile config

def freeze_plan(*, workflow: AtlasWorkflow, slots: Sequence[PlacementSlot],
                policy: PlacementPolicy, weights: PlacementWeights) -> FrozenAssignmentPlan: ...
    # calls placement.planner.plan_workflow; raises if any task lacks exactly one slot
    # or if a task's required capability is absent from its slot's profile

def bind_task(*, plan: FrozenAssignmentPlan, task_id: str, factory: ScopedWorkerFactory,
              parent_context: ExecutionContext, assignment: WorkerAssignment) -> WorkerInstance: ...
    # resolves slot, asserts slot.profile_id == assignment.profile_id, constructs the worker with
    # worker_id=f"{run_id}-{task_id}", task_id=f"{run_id}-{task_id}", then re-asserts on the instance

def verify_executed_assignment(*, plan: FrozenAssignmentPlan,
                              executed: Mapping[str, str]) -> None: ...
    # raises PlacementPlanMismatch before results are published
```

Callers: `runner.run_sample` (worker-construction loop, `runner.py:1418`), and the post-run evidence
step next to `_collect_tool_calls` (`runner.py:1593`). `AtlasRuntime._place` and the planner are not
modified.

Changed files at G1–G3 (all outside `src/`): `experiments/evaluation/final4/placement_slots.py` (new),
`experiments/evaluation/runner.py` (worker loop + evidence), `experiments/evaluation/final4/runner_entry.py`
(unit config + digest), plus tests.

---

## 8. Performance boundary of B′

Explicitly **not** claimed:

| Claim | Why not |
|---|---|
| logical slot reuse = agent instance reuse | each task still gets its own `Agent` (`pool.get(assignee)` with unique names) |
| = conversation reuse | `Agent.run` ignores history |
| = fewer model input tokens | each task's prompt is unchanged by placement |
| = KV-cache reuse | no shared prefix is created by sharing a slot |
| = lower cost or latency | nothing in the model path changes |

What B′ buys: a correct, host-frozen **resource assignment** with genuine identity isolation, per-task
attribution intact, and an auditable plan-versus-execution check. Its measurable runtime effect can
only come from profile heterogeneity (§3.2) or enforced capacity (§3.5) — the latter is blocked.

---

## 9. Unverified assumptions and open blockers

1. **Slot capacity is not enforceable** at any existing seam (§3.5). Owner decision required before any
   claim about serialisation or parallel width.
2. **Heterogeneous slot pool not yet defined.** With one profile, B′ has no runtime effect. G2 must
   specify at least two profiles that differ in something the runtime acts on (`operating_scope`,
   `max_turns`, tool preference), otherwise the experiment is null by construction.
3. **`WorkerCatalog` contents** were not audited for a second usable profile; `coding_worker` is the only
   one exercised so far.
4. **`AtlasContextEngine.build_agent` tool override** is described from its behaviour in this repo; the
   exact precedence against `AgentConfig.tools` should be re-confirmed in G3 with a test, not a reading.
5. **Slot in the request evidence.** Today the request wrapper identifies a worker by
   `sha256(system prompt)`; under B′ that is insufficient (two slots can share a profile and prompt).
   G4 must bind request evidence to the trusted task execution identity, not to a prompt digest.
6. **Stage-6 historical placement numbers** are on the per-policy definition and cannot be merged with
   §5 outcomes.

---

## 10. Revised gate boundaries

| Gate | Scope | Exit criterion |
|---|---|---|
| **G1** | Minimal slot / frozen plan / task-execution binding model. `WorkerStore`, `worker_id` semantics and old `ExecutionContext` untouched | offline tests: plan determinism, one slot per task, `bind_task` rejects a profile mismatch, `WorkerStore` unchanged, existing runs still parse |
| **G2** | Reuse the existing planner; legal-candidate constraints; heterogeneous slot pool; policy-independent `AssignmentOutcome` + the equal-assignment equality test | acceptance test of §5 passes; no planner algorithm change |
| **G3** | Actual AnyCode execution consumes the frozen plan; slot admission (only if approved per §3.5); trusted profile binding; reliable-execution compatibility | plan == execution; ledger/ownership per task unchanged; failure path releases the slot |
| **G4** | Context Plane + real sandbox integration; different tasks on one slot keep correct context; real parallel evidence | dependency delivered once per consumer; request evidence bound to task execution identity |
| **G5** | Full regression, support-scope update, freeze the new runtime SHA | reviewer sign-off |

Each gate: implement → test → commit → push → independent review → **stop**.

---

## 11. What this document still does not claim

- It does not claim B′ has a performance benefit; §8 lists what it cannot deliver.
- It does not claim capacity can be enforced today; §3.5 declares the opposite.
- It does not claim the three-correction list is complete — it is what the review identified.
- It does not choose between the capacity options in §3.5.

---

## 12. Appendix — the rejected options, kept for the record

### A1 — reported-only placement (REJECTED)

Planner decisions recorded, execution untouched. Changes no assignment; measures nothing. Rejected
because it cannot validate placement.

### A2 — slot as agent (REJECTED)

`k` agents for `N > k` tasks. Changes AnyCode's assignment, loses the per-task `ExecutionContext`
(baked into `AgentConfig` at construction, `agent.py:85`), and collides in `agent_results[assignee]`
(§2.2). Rejected.

### B — shared `worker_id` + `WorkerStore.tasks` (NEEDS_REDESIGN, withdrawn)

Three defects, each proven above: `worker_id` determines `worker_name`/`AgentConfig.name`/`assignee`,
so "share the id, keep the names" is impossible (§2.1); agent-name sharing silently drops results
(§2.2); and `WorkerInstanceRecord` cannot represent several tasks by adding a list (§2.3). B′ replaces
it.
