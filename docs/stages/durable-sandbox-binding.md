# Stage A — Durable Sandbox Binding

Status: **implemented and frozen**. Stage A itself did not implement verified cross-process sandbox
reattachment; Stage B adds an explicit verification/reconnect primitive while leaving automatic
recovery and ownership locking disabled.

## Problem

Before Stage A, `ProjectSandboxManager.bindings` was the only record of the task-to-sandbox
relationship. If the Atlas process exited, that relationship disappeared even when the physical
OpenSandbox container still existed. A restart could therefore either lose all knowledge of the
sandbox or be tempted to create a replacement.

Stage A makes the M5 relationship durable:

```text
Atlas run/task T1
        │
        ▼
DurableSandboxBindingRecord
        │  T1 → sandbox S1
        ▼
SQLiteSandboxBindingStore
```

The in-process `ProjectSandboxBinding` is now only a live-handle cache. It contains the
`SandboxHandle` required by the current process, but it is not the authority for whether T1 owns S1.

## Durable record vs runtime handle

| Concept | Contains | Authority |
| --- | --- | --- |
| `DurableSandboxBindingRecord` | logical run/task, provider, sandbox id, workspace, config/capability digests, source identity, state, revision | M5 relationship and lifecycle truth |
| `ProjectSandboxBinding` | the above identity plus a live `SandboxHandle` | runtime-local cache only |
| OpenSandbox | the actual container/process | infrastructure truth |
| M7 trajectory | lifecycle evidence | evidence only, never binding authority |
| M6 suspension | approval continuity | approval state only, never binding authority |

No SDK object, provider session, socket, coroutine, asyncio lock, credential, API key, or resolved
secret is stored in SQLite.

## SQLite schema

```sql
CREATE TABLE sandbox_bindings (
    binding_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    worker_id TEXT,
    provider_name TEXT NOT NULL,
    sandbox_id TEXT NOT NULL,
    workspace_root TEXT NOT NULL,
    config_digest TEXT NOT NULL,
    capabilities_digest TEXT NOT NULL,
    source_commit TEXT NOT NULL,
    source_digest TEXT NOT NULL,
    bundle_digest TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    revision INTEGER NOT NULL,
    payload TEXT NOT NULL,
    UNIQUE (run_id, task_id)
);
```

`UNIQUE(run_id, task_id)` enforces at most one committed durable binding for one Atlas task. The
payload is a serialized `DurableSandboxBindingRecord`; the indexed columns are the query and CAS
surface.

## Create ordering

```text
inspect source
→ provider.create()
→ provision workspace
→ binding_store.create(durable READY record)
→ install runtime ProjectSandboxBinding cache
→ M7 READY evidence (best effort)
```

If `binding_store.create()` fails after the physical sandbox was created, Atlas best-effort destroys
that sandbox, does not install a runtime binding, does not emit M7 READY, and fails the operation
closed.

If the process dies before the durable commit, no durable binding exists and Atlas does not claim the
sandbox. It is an orphan for TTL/manual cleanup; Atlas never scans provider inventory to guess
ownership.

## Durable lifecycle and revision

```text
READY → LOST
READY → CLOSED
```

`LOST` and `CLOSED` are terminal for Stage A. Every transition requires the expected revision and is
committed with a compare-and-swap update; stale revisions fail closed. This is M5 binding consistency,
not M4 execution idempotency.

`provider.destroy()` or `provider.cancel()` success must be observed before a durable transition to
`CLOSED`. Failure or an unknown provider result transitions a READY binding to `LOST`, never
`CLOSED`.

## Restart behavior in Stage A

If a durable record is `READY` and the new process has no runtime handle:

```text
durable READY binding exists
→ no live SandboxHandle attached
→ SandboxReattachmentRequired
→ provider.create count remains 0
```

Atlas does not call `Sandbox.connect()`, does not recreate S1, and does not fall back to the Host.
This remains the automatic-recovery behavior. Stage B provides an explicit
`reattach_verified()` primitive for callers that have verified the durable provider evidence; it is
not wired into `ensure_binding()` or M6.

`LOST` and `CLOSED` durable records also block recreation after a manager restart.

`require_ready()` reads the durable record and validates the runtime binding identity before any
provider operation. A stale in-memory READY binding cannot override a durable `LOST`/`CLOSED`
record.

## Continuity projection

M6 must not hash a runtime binding dump. M5 owns a stable projection:

```text
SandboxContinuityProjection
  protocol_version = atlas/m5-sandbox-continuity/v1
  binding_id
  run_id / task_id / worker_id
  provider_name / sandbox_id
  workspace_root
  config_digest / capabilities_digest
  source_commit / source_digest
```

State, revision, timestamps, runtime handles, and provider objects are excluded. M6 consumes the
single `compute_sandbox_continuity_digest()` result; it does not reconstruct or hash the projection
itself.

## Crash windows

| Window | Behavior |
| --- | --- |
| Provider sandbox created, durable commit not reached | Best-effort destroy if Atlas is alive; otherwise orphan with no durable ownership |
| Durable commit succeeded, runtime cache not installed | Restart sees durable READY without a handle and fails closed with reattachment required |
| Provider destroy succeeded, durable CLOSED not committed | Durable record may remain READY; restart fails closed and never creates a replacement |
| Durable store unavailable while reading | Operation fails closed; runtime cache is not promoted to authority |

## Non-goals

Stage A does **not** implement:

- verifying that S1 still exists;
- connecting to S1;
- cross-process ownership locks, leases, heartbeats, fencing tokens, or takeover;
- split-brain prevention;
- distributed sandbox coordination;
- true OS-process HITL resume;
- provider-side continuity verification.

See **Stage B — Verified OpenSandbox Reattachment** for the explicit provider-instance reconnect
primitive. Cross-process ownership remains Stage C.
