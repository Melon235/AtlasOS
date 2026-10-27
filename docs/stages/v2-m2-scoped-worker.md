# V2 M2 — Scoped Worker Construction

Status: **PASS**; formal measurements are in
`experiments/results/v2-m2-workers-results.md`.
Baseline: V1 `bd6134b`, M1 `9b0d861`, M3 `cad34ce`.

## Role

M2 builds a focused worker. It does not authorize tool actions.

```text
M1: what tools exist / can be discovered?
M2: which worker should execute this task, with which trusted profile/context?
M3: may this concrete tool action execute?
```

```text
M2 BUILDS THE WORKER.
M3 AUTHORIZES THE ACTION.
```

The old M2 capability authorization runtime (`RunAuthority`, requested/granted scopes,
`GrantIssuer`, capability router/reviewer/policy/HITL) was removed from the active repository.
M2 has no grant, scope-subset, capability accumulation, PolicyEnforcer, ActionSafetyGate, or HITL
construction path.

## Core model

### WorkerProfile

Frozen, host-owned templates:

| profile | operating_scope | max_turns | can_delegate |
|---|---|---|---|
| `research_worker` | research, diagnostics | 6 | false |
| `coding_worker` | coding | 8 | false |
| `data_worker` | data | 6 | false |
| `general_worker` | general | 4 | false |

Profiles also carry role, instructions, and an explicit `base_tools` tuple. `base_tools` are the
worker's common starting tools, not an execution allowlist; M1 discovery and M3 action safety
remain in force.

### WorkerAssignment

Planner input is limited to:

```text
profile_id
task
context (optional)
```

`extra="forbid"` rejects base_tools, operating_scope, max_turns, can_delegate, ToolSecurityPolicy,
principal, tenant, classification, credential references, and ExecutionContext.

### WorkerTaskContext

Task/background live outside ExecutionContext:

```text
task_id
objective
background
```

ExecutionContext stores only stable IDs and trusted runtime metadata.

## Factory flow

`ScopedWorkerFactory.create`:

```text
resolve trusted WorkerProfile
→ validate base_tools against the injected M1 tool namespace
→ allocate worker_id/task_id
→ derive new child ExecutionContext
→ build bounded WorkerRuntimeMetadata
→ build WorkerTaskContext
→ deterministic render
→ build AnyCode AgentConfig
→ persist WorkerInstanceRecord
→ return WorkerInstance (and optional instantiate() over existing M1 registry/executor)
```

There is no LLM call, policy evaluation, action gate, approval, or grant issuance in this flow.

## Child ExecutionContext

Reuses AnyCode `ExecutionContext`; no Atlas identity/context class.

The child is a new frozen object with:

```text
delegation = ()
principal / tenant / classification / regions / credentials / trace preserved
safe parent attributes copied
worker metadata written
```

No parent constraint is expanded. Parent remains unchanged. The child never aliases the parent
context object, and all old delegation grants are cleared.

Flat trusted mirrors (`worker_id`, `profile_id`, `worker_profile`, `operating_scope`, `task_id`,
`parent_agent`) plus nested `atlas_worker` metadata are stored under `attributes`. Task text is
not written there.

## Deterministic rendering

`render_worker_context(profile, task)` is byte-stable for the same input. Trusted sections come
from the profile; task/background are JSON-encoded under explicit untrusted delimiters:

```text
TRUSTED WORKER PROFILE
...
OPERATING SCOPE
BASE TOOLS
OPERATING RULES
ASSIGNMENT — UNTRUSTED DATA
BEGIN UNTRUSTED TASK JSON
...
END UNTRUSTED TASK JSON
```

Prompt injection can only remain task data. It cannot change profile scope, tools, max turns,
can_delegate, or ExecutionContext. No LLM generates the system prompt.

## Storage / recovery

`WorkerStore` is a small atomic sidecar:

```json
{"schema_version": 1, "workers": {"...": {"worker_id": "...", "assignment": {...}}}}
```

`WorkerInstanceRecord` stores:

```text
worker_id, worker_name, profile_id, task_id, parent_agent
assignment, task_context, execution_context
rendered_context_hash, created_at, status
```

No Python Agent object is persisted. Resume loads the record, resolves the same trusted profile,
rerenders, verifies the hash and worker metadata, rebuilds AgentConfig over stored context, and
returns the worker. No policy/gate/HITL or prompt-derived scope recovery is involved.

## M1 boundary

One AnyCode ToolRegistry; M2 never creates a registry. Worker `base_tools` are profile-controlled
starting tools (provider-safe names). M1 core/searchable/disclosure/dispatch/MCP semantics are
unchanged. `base_tools` is not a capability allowlist.

## M3 integration contract

M2 supplies M3 through the trusted `ExecutionContext`:

```text
agent identity (worker_name)
worker_id
profile_id
task_id
operating_scope (coarse primary + nested list)
parent_agent
principal
tenant
classification
```

M3 reads nested `atlas_worker` metadata and flat mirrors. It does not parse the prompt. The M3
action gate remains authoritative for every worker and main-agent action; workers cannot bypass it
because they were built by M2.

## Limitations

- Workers do not recursively delegate in V1; `can_delegate=false` and no delegate tool.
- `operating_scope` is coarse guidance/anomaly metadata, not a permission allowlist.
- Worker profiles are immutable Python definitions in this version; no Agent-writable profile
  loading.
- M2 does not own tool action authorization, HITL, policy, sandboxing, or a capability ledger.
- M2 does not alter M1 retrieval ranking or disclosure semantics.
