# Stage D — True OS-Process HITL Resume

Status: implemented for a durable M6 pre-execution suspension whose M5 sandbox S1 is still
`READY`. This is not arbitrary agent crash recovery and not distributed fencing.

## D1 — Trusted Runtime Reconstruction

`atlasos.trusted_recovery.TrustedRecoverySession.prepare()` reconstructs the runtime from a
deployment-owned state root:

```text
state_root/
  workers.json
  policy.json
  suspension.db
  approvals.db
  ledger.db
  idempotency.db
  sandbox_bindings.db
  runs/<run_id>/
```

It reuses the existing `ScopedWorkerFactory.resume(worker_id)`, `WorkerStore`, `AtlasToolRuntime`,
`SQLiteSuspensionStore`, `SQLiteApprovalStore`, `SQLiteActionLedgerStore`,
`SQLiteToolIdempotencyStore`, `ProjectSandboxManager`, and `SandboxWorkspaceTools`.

Reconstruction is fail-closed on:

- missing trusted state or policy;
- missing SQLite idempotency store (never falls back to the in-memory store);
- suspension/run/task/worker/profile mismatch;
- tampered Worker record, principal, tenant, rendered context hash, or trusted identity digest;
- changed tool binding/schema/source;
- a Worker profile that does not expose the pending tool.

## D2 — M5 × M6 Recovery Ordering

The explicit entry is `trusted_recovery.resume_durable_sandbox_suspension()` or the session API
`TrustedRecoverySession.prepare().resume()`. It composes the existing M6
`resume_execution_suspension()` path; it is not a second settlement pipeline.

Ordering:

```text
load suspension and validate run/task/worker/profile
→ inspect durable lifecycle
→ reconstruct trusted Worker + M3/M4/M5/M6 runtime
→ ACTIVE + pending approval: return WAITING (no M5 connect, no tool)
→ CLEARED: return CLEARED
→ SETTLED / SETTLING+settlement: continuation backfill only (no tool, no M5 connect)
→ ACTIVE resolved or SETTLING without settlement:
   acquire M5 ownership
   reattach_verified(S1)  (never create S2)
   verify network-none + config/metadata/control-plane continuity
   compare M5 continuity digest with the suspension
   invoke existing M6 resume
   M3 resume revalidation
   M4 + AnyCode SQLite execution claim
   durable settlement
   conversation continuation
   suspension clear
```

`SETTLING` keeps its existing lease/revision CAS semantics; recovery does not force it to
`ACTIVE`. `CLEARED` and `SETTLED` never re-execute a tool.

## M4 / AnyCode duplicate protection

The execution claim remains AnyCode's `SQLiteToolIdempotencyStore`. M4 remains evidence/state
recording around it. Recovery uses the same suspension execution key and the same SQLite database
as Process A. The path never constructs `InMemoryToolIdempotencyStore` when an SQLite store is
required.

If a process dies after the idempotency claim but before the result is recorded, recovery sees an
in-progress claim and classifies the action as `UNCERTAIN`. It does not remove the claim and does
not blindly retry a potential side effect. A durable completed settlement is replayed/backfilled
through the existing continuation path.

## Recovery session cleanup

The public wrapper defaults to `dispose_sandbox_on_settlement=False`. `TrustedRecoverySession.close()`
is idempotent and performs a Stage C safe handoff when ownership can be released:

```text
wait for in-flight operations through the existing workload gate
→ local detach
→ invalidate runtime handle cache
→ release OS workload lock
```

It never destroys S1 and never converts the durable READY binding to CLOSED. A later process must
use verified reattachment to use S1 again.

If the M6 settlement contains an `UNCERTAIN` result, the manager still reports provider
uncertainty, or local detach fails, cleanup retains ownership and sets
`RecoveryCleanupResult.ownership_unresolved`. It does not force unlock. The public wrapper raises
`TrustedRecoveryOwnershipUnresolved` with the recovery outcome and cleanup result so callers can
perform explicit reconciliation.

If recovery already failed, cleanup does not replace the original exception; unresolved ownership
is attached as a Python exception note. `close()` can be called repeatedly and returns the same
cached cleanup result.

## Stage C interaction

The pending execution interval holds M5 OS workload ownership. Normal cleanup uses the Stage C
gate and `close()`/safe local detach rules. If a provider result is uncertain, ownership is retained
rather than handed off as if retry were known-safe.

## D3 acceptance

`experiments/eval_os_process_hitl_resume.py` uses real OS subprocesses and real OpenSandbox:

- Process A creates S1, writes a marker, forms a deterministic `file_write` tool-use, obtains
  `REQUIRE_HUMAN`, persists the M6 suspension, and is killed with the OS process API.
- A separate reviewer process resolves the real `SQLiteApprovalStore` challenge.
- Process B reconstructs the Worker/runtime, reattaches S1, verifies network-none and continuity,
  executes the original `file_write` once, persists settlement/continuation, clears the
  suspension, and destroys S1.
- Process C covers crash-after-settlement backfill and repeated recovery.
- Additional scenarios cover pending approval, rejection, ownership busy, lost S1, changed policy,
  changed Worker identity, crash after durable settlement, and crash during an in-flight
  side-effecting write with an unconfirmed result.

## Boundaries

```text
DISTRIBUTED FENCING = NOT IMPLEMENTED
MULTI-HOST OWNERSHIP = NOT IMPLEMENTED
ARBITRARY AGENT CRASH RECOVERY = NOT IMPLEMENTED
REMOTE EXACTLY-ONCE = NOT CLAIMED
```

A local process death releases OS ownership. If a remote side effect is still uncertain, Stage C's
in-memory uncertainty marker does not survive; recovery relies on durable M4/AnyCode state and
returns `UNCERTAIN` where no safe retry can be proven.
