# Placement → Coding Integration — Design for Independent Review (G0-R1 + G1)

Status: `AWAITING_INDEPENDENT_CODE_REVIEW`. B′ design approved at
`a1b45bdc58a771e321569591030a6b3d262e6454`; **G1 is implemented** (§7b), G2–G5 are not.

Everything in §1–§12 is `DESIGN_PROPOSED` except where §7b names code that exists. §3.5 was corrected
after review: slot capacity is enforceable via a `_run_wave_task` subclass wrapper, not blocked.

| Review item | Verdict | Consequence for this document |
|---|---|---|
| Option A1 (reported-only) | **REJECTED** | kept for the record only (§12) |
| Option A2 (slot as agent) | **REJECTED** | kept for the record only (§12) |
| Option B (shared `worker_id` + `WorkerStore.tasks`) | **NEEDS_REDESIGN** | replaced by **B′** (§3), and its three false source claims are corrected in §2 |
| B′ (run-scoped slot + task-scoped execution instance) | **APPROVED** for G1 | §3–§9, implemented in §7b |

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

**Reviewer decision (G0-R1 → G1): this is NOT a blocker.** The earlier conclusion in this section was
wrong: it claimed that overriding `_run_wave_task` would mean copying AnyCode's main execution loop.
AnyCode 0.9.0 (`orchestrator.py`, fixed at commit `ffdc7d28`) dispatches each task through
`asyncio.create_task(self._run_wave_task(task, queue, team, …))`, i.e. a *single-task method* called
from `_execute_tasks`. A subclass can therefore override that one method, add admission / completion /
abort logic, and delegate with `await super()._run_wave_task(...)`. No loop body is copied, the
installed package is not patched, and no scheduler is added.

Enforcement model adopted for G3:

1. **Static plan legality** (implemented in G1, see §7b): the frozen assignment must not place two
   tasks of the same wave on a slot whose capacity is 1.
2. **Runtime admission check** (G3): the wrapper verifies the task's frozen assignment, the worker ↔
   slot ↔ profile binding, and that the slot has no other active task; an illegal concurrent
   placement fails closed instead of waiting.
3. **No queue, no DAG edit, no global cap.** G3 must not introduce a dynamic wait queue, must not
   serialise by injecting dependencies, and must not set global concurrency to 1 as a stand-in for
   slot capacity.
4. **Lifecycle**: G3 has to handle exception, cancellation, failure, suspension, `UNCERTAIN` and
   unsettled side effects. A `finally` that drops a local lock is not evidence of execution safety,
   and a new task must not start on a slot whose previous task is unresolved.

G1 implements step 1 and nothing else: no wrapper, no admission, no engine change.

**Consequence for the experiment.** Capacity is real but is enforced by *plan legality plus a runtime
check*, not by a work queue. A homogeneous pool (`coding_worker` on every slot) then differs from a
single-slot plan only in how many slots the DAG may fan out to; a heterogeneous pool additionally
constrains which capability can execute a task. Both are legitimate, and they answer different
questions — see §7b.4.

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

## 7. Core code location (corrected in G1)

The domain model lives in the core package, not in the evaluation layer:

```text
src/atlasos/placement/slots.py     PlacementSlot, build_slot_pool, FrozenAssignmentPlan,
                                   freeze_assignment, verify_plan, plan_from_payload, bind_task
```

Reasons: it is a Context Engineering mechanism (contract §2.E), it must not be reachable only from
one experiment runner, and the evaluation layer already imports from `atlasos.*` in the other
direction. **Core never imports `experiments`.**

Planned evaluation wiring (G2/G3, not implemented): `runner.run_sample` builds the pool and plan,
calls `bind_task` in the worker loop (`runner.py:1418`), and re-checks plan-versus-execution next to
`_collect_tool_calls` (`runner.py:1593`). `AtlasRuntime._place` and `planner.policy` are not modified.

## 7b. G1 delivered (this stage)

### 7b.1 Interfaces actually implemented

| Symbol | Signature (abridged) |
|---|---|
| `PlacementSlot` | `frozen dataclass(slots=True)`: `slot_id`, `profile_id`, `capabilities: frozenset[str]`, `max_active_tasks: int = 1` |
| `build_slot_pool` | `(*, run_id: str, catalog: WorkerCatalog, profile_ids: Sequence[str]) -> tuple[PlacementSlot, ...]` |
| `slot_capabilities` | `(profile) -> frozenset[str]`; the rule is `frozenset(profile.operating_scope)` |
| `SlotProfile` | per-slot fingerprint of the trusted definition that will build the workers |
| `AssignmentEntry` | `(task_id, slot_id)`, one per task |
| `FrozenAssignmentPlan` | `schema_version`, `run_id`, `workflow_digest`, `slots`, `slot_profiles`, `assignments`, `wave_of_task`, `policy_name`, `policy_version`, `policy_config_digest`, `plan_digest` |
| `freeze_assignment` | `(*, run_id, workflow, slots, assignments, catalog, policy_name, policy_version, policy_config=None) -> FrozenAssignmentPlan` |
| `verify_plan` | `(plan) -> None`; recomputes the digest from the plan's own fields |
| `plan_from_payload` / `plan_from_json` | envelope in, verified plan out |
| `TaskExecutionBinding` | `run_id`, `logical_task_id`, `runtime_task_id`, `placement_slot_id`, `worker_id`, `agent_name`, `profile_id`, `plan_digest` |
| `bind_task` | `(*, plan, logical_task_id, runtime_task_id, factory, parent_context, parent_agent, assignment, prior=None) -> tuple[TaskExecutionBinding, WorkerInstance]` |

### 7b.2 Task identity mapping

| Name | Example | Owner |
|---|---|---|
| `logical_task_id` | `w1` | the workflow (`AtlasTaskSpec.task_id`) |
| `runtime_task_id` | `eval-abc123-w1` | the executor; here it is also the `worker_id` |
| `worker_id` / `task_id` (factory) | `eval-abc123-w1` | `ScopedWorkerFactory.create`, unchanged semantics |
| `agent_name` | `coding_worker:eval-abc123-w1` | `factory` → `AgentConfig.name` → AnyCode `assignee` |

The plan only knows `logical_task_id`. `bind_task` requires the caller to supply `runtime_task_id`,
because the mapping from a workflow task to a runtime task is a composition decision, not a plan fact.

### 7b.3 Immutability and canonical form

- Every collection in the plan is a `tuple`/`frozenset`; there is no `dict` field, so a caller cannot
  mutate the plan through `.assignments`.
- `freeze_assignment` copies everything it keeps: assignments are normalised into sorted
  `AssignmentEntry` tuples, slots are re-read from the input sequence, and the policy configuration is
  digested immediately rather than retained.
- The digest covers: schema version, run id, workflow digest, slot definitions, **full trusted profile
  definitions** (via per-slot `definition_digest` over role, instructions, `base_tools`,
  `operating_scope`, `max_turns`, `can_delegate`), the assignment set, the wave map, and the policy
  name/version/config digest. Map insertion order cannot affect it (sorted canonical JSON).
- Serialised form is an **envelope**: `{"plan": <content>, "plan_digest": "sha256:…"}`. The stored
  digest is compared with a recomputation over the content, so editing either side is rejected. A
  digest never covers itself.
- The digest is an integrity check, not a signature or an authorization token.

### 7b.4 Homogeneous vs heterogeneous pools (experiment roles)

| Pool | Question it answers | Must not be reported as |
|---|---|---|
| homogeneous (`coding_worker` × n) | does the assignment change how the DAG fans out, with capability held constant? | a context-locality benefit |
| heterogeneous (`coding_worker` + `research_worker` + …) | does capability-constrained placement change what runs where? | a context-locality benefit either — the difference is capability, not locality |

The existing catalog already provides `research_worker`, `coding_worker`, `data_worker`,
`general_worker`. G2 adds **no** profile and changes **no** profile definition or gate rule.

### 7b.5 What G1 verified against the real factory

`tests/unit/test_placement_slots.py` builds two tasks on **one** slot through the real
`ScopedWorkerFactory`, then asserts on the constructed objects and on `workers.json`:

- distinct `worker_id`, distinct `AgentConfig.name`, correct per-task `task_id` in both the record and
  `ExecutionContext.attributes`;
- `principal` inherited unchanged from the parent context;
- the profile actually used is the slot's profile;
- both records persisted and independently readable, and the existing `resume(worker_id)` restores
  each task record;
- profile mismatch rejected **before** the factory persists anything (store still empty);
- cross-run plan rejected; conflicting rebinding rejected without overwriting the first record.

### 7b.6 Not done in G1

Planner adapter (G2), `AssignmentOutcome` evaluator (G2), slot admission / `_run_wave_task` wrapper
(G3), AnyCode execution consumption of a plan (G3), Context Plane integration (G4), real sandbox or
model runs (G4/G5), placement recovery, plan persistence into `run_dir`, conversation reuse, and any
performance claim. `WorkerStore` schema, `workers.json` format, `factory.py`, `models.py`, the gate,
the ledger, ownership, approval, suspension and the sandbox binding are untouched.

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
| **G1** | Minimal slot / frozen plan / task-execution binding model. `WorkerStore`, `worker_id` semantics and old `ExecutionContext` untouched | **implemented** (§7b); offline tests green; awaiting code review |
| **G2** | Reuse the existing planner; legal-candidate constraints; homogeneous or heterogeneous slot pool as the experiment requires; policy-independent `AssignmentOutcome` + the equal-assignment equality test | acceptance test of §5 passes; no planner algorithm change |
| **G3** | Actual AnyCode execution consumes the frozen plan; slot admission through the `_run_wave_task` wrapper (§3.5); trusted profile binding; reliable-execution compatibility | plan == execution; ledger/ownership per task unchanged; failure path releases the slot |
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
