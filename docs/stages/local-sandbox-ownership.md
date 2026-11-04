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
→ close/cancel: provider operation, durable transition, then release
→ process exit: OS releases the lock
```

`ensure_binding()` and `reattach_verified()` acquire ownership before touching the durable record,
provider control plane, or provider sandbox. Workspace tools call `require_ready()`, which verifies
that this process still holds ownership before any provider operation. Releasing ownership while a
READY runtime cache remains causes later tool operations to fail closed without provider access.

The existing `asyncio.Lock` remains the in-process operation serializer. The OS lock and the asyncio
lock are complementary; neither replaces the other.

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

A lock acquisition failure must not mark the sandbox LOST. A durable LOST/CLOSED record cannot be
revived by acquiring the lock.

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
