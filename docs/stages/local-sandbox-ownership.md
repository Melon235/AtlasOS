# Stage C — Local Sandbox Workload Ownership

Status: **implemented for one Host / one trusted state root**. This is not distributed fencing.

## Problem

Stage A and B made the M5 task → sandbox relationship durable and made reattachment explicitly
verifiable. They did not prevent two Atlas processes on the same Host from both operating the same
workload. Stage C adds a Host-local OS file lock:

```text
one (run_id, task_id) → at most one active Atlas process owner
```

The durable lifecycle remains:

```text
READY / LOST / CLOSED
```

Ownership is not a new lifecycle state. Lock contention returns a typed failure and does not imply
that the sandbox is LOST.

## Lock identity and path

The lock file is derived from a canonical digest of:

```text
run_id
task_id
```

The filename is:

```text
<state_root>/workload-<sha256-identity>.lock
```

Raw user-controlled IDs are never concatenated into the path. The file is opened and locked through
the OS; its existence is never used as proof of ownership.

`state_root` is the trusted state directory. The current SQLite-backed composition derives it from
the durable binding store's parent directory unless an explicit `ownership_root` is supplied.

## OS implementations

Windows Atlas Host:

```text
msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
```

Linux runtime:

```text
fcntl.flock(fd, LOCK_EX | LOCK_NB)
```

The lock is held by an open file descriptor, not by a process-local boolean or a lock-file marker.
The descriptor stays open for the whole ownership period. Normal release unlocks and closes it;
process death lets the OS release it.

## Lifecycle

```text
acquire Host-local workload lock
→ read durable binding
→ if absent: provider.create + provision + durable commit under ownership
→ if present READY and explicit recovery requested: Stage B verified reattach
→ keep lock while this process uses the sandbox
→ close/cancel: provider operation, confirmed durable terminal transition, then release
→ process exit: OS releases the lock
```

`ensure_binding()` and `reattach_verified()` acquire ownership before touching the durable record,
provider control plane, or provider sandbox. Workspace tools call `require_ready()`, which verifies
that this process still holds ownership before any provider operation.

Explicit `release_ownership()` is a handoff, not just an unlock. Under the workload gate it
invalidates the manager's runtime handle cache and asks the provider for a **local detach only**;
the remote sandbox is never destroyed. The durable READY record and its continuity identity are
preserved. After release, the old runtime handle and old WorkspaceTools instance have no execution
authority. Recovery must reacquire OS ownership and perform a successful Stage B verified
reattachment; `ensure_binding()` does not reuse the old handle.

If local detach is unavailable or fails, release fails closed: the runtime authority is removed,
the uncertainty marker is set, and OS ownership is retained rather than handed off.

The manager-wide per-workload async gate is entered **before** OS ownership is acquired or confirmed.
`ensure_binding`, `reattach_verified`, `mark_lost`, `close`, `cancel`, explicit ownership release,
and every provider operation from `SandboxWorkspaceTools` pass through that gate.

`release_ownership()` waits for the gate, so a release cannot complete while a provider call is still
in flight. The OS ownership lock remains held across individual tool calls; it is not released at the
end of each call.

If a coroutine is canceled while waiting for the gate, it has not acquired or released OS ownership
and cannot affect the active owner. If a provider call is canceled after it starts, Atlas does not
assume the remote side effect stopped. The local ownership remains held and the workload is marked
in-process **provider-operation-uncertain**; further provider operations and explicit release fail
closed until `mark_lost` or `close` resolves the workload. This uncertainty marker is not a durable
M5 lifecycle state.

The uncertainty marker is process-local. If the owning process dies while a remote side effect is
still uncertain, the OS releases the lock and a later process can attempt verified reattachment; no
provider API can prove that the earlier in-flight operation has finished. That is an explicit
fail-closed boundary of this Stage, not a claim of distributed or remote-fencing correctness.

## Failure classification

```text
lock held by another process
→ SandboxOwnershipBusy
→ provider.create / inspect / connect / execute = 0
→ durable lifecycle unchanged
```

```text
lock backend/path unavailable
→ SandboxOwnershipUnavailable
→ no unlocked fallback
```

```text
close/cancel/mark_lost exception or durable-transition failure
→ durable READY may still be recoverable
→ retain OS ownership + provider-operation-uncertain
→ no handoff to another process
→ explicit retry/mark_lost/close once terminal state is confirmed
```

A store-availability failure is not evidence that the sandbox is LOST, and it must not be converted
into LOST. A lock acquisition failure must not mark the sandbox LOST. A durable LOST/CLOSED record
cannot be revived by acquiring the lock.

Only a confirmed durable terminal state (or a successfully completed lifecycle operation) allows
ownership release after a lifecycle error. Provider destroy/cancel exceptions, post-destroy durable
transition failures, and unreadable store failures all fail closed.

## Windows / WSL2 boundary

The supported Stage C scope is:

```text
one Host OS
one trusted local state root
```

Windows `msvcrt` locks and Linux `fcntl.flock` locks are not asserted to interoperate across the
Windows filesystem boundary used by WSL2. The current Atlas Host runs the Windows client and uses a
Windows-local state root; the OpenSandbox server running in WSL2 is remote from Atlas ownership.
A Linux Atlas runtime requires a native Linux state root and native `flock` semantics. Do not place
the state root on a cross-OS share and assume mutual exclusion.

Windows may release a forcibly terminated process's byte-range lock asynchronously. A new process may
observe transient `SandboxOwnershipBusy` immediately after a kill; callers may use a bounded
acquisition timeout instead of assuming immediate release.

## Non-goals

Stage C does not implement:

```text
distributed ownership
Redis or external lease authority
heartbeats
fencing tokens
owner generation
automatic takeover
automatic M6 OS-process resume
replacement sandbox creation
```

It also does not make network policy permanent. The Stage B post-connect network check is a
point-in-time TOCTOU-bounded observation.

Stage D remains true OS-process HITL resume.
