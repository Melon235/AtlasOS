# Stage B — Verified OpenSandbox Reattachment

Status: **implemented as an explicit primitive**. Automatic cross-process recovery and ownership
locking are not implemented.

## Problem

Stage A made the M5 task → sandbox relationship durable. A restarted process with a durable `READY`
record and no live `SandboxHandle` correctly failed closed with
`SandboxReattachmentRequired`. Stage B adds an explicit API that can prove the remote sandbox is the
same durable sandbox before calling `Sandbox.connect()`:

```text
durable READY binding
→ current trusted config compatibility
→ control-plane identity
→ exact sandbox ID inspection
→ attachable provider state
→ provider-created-at
→ Atlas metadata correlation
→ Sandbox.connect(S1)
→ runtime-local ProjectSandboxBinding
```

`ensure_binding()` still never calls this automatically.

## Installed OpenSandbox contract

Verified against the local `opensandbox==1.2.0` package:

```text
Sandbox.connect(
    sandbox_id: str,
    connection_config: ConnectionConfig | None = None,
    health_check: ... = None,
    connect_timeout: timedelta = timedelta(seconds=30),
    health_check_polling_interval: timedelta = timedelta(milliseconds=200),
    skip_health_check: bool = False,
) -> Sandbox

Sandbox.close() -> None
    closes local HTTP/client resources only; it does not terminate the remote sandbox.

Sandbox.get_info() -> SandboxInfo              (async)
SandboxManager.get_sandbox_info(id) -> SandboxInfo (async)
SandboxManager.close() -> None                  (local resources only)
```

`SandboxInfo` exposes:

```text
id: str
status: SandboxStatus(state: str, reason: str | None, message: str | None, ...)
created_at: datetime
metadata: dict[str, str] | None
image: SandboxImageSpec | None
expires_at / snapshot_id / platform / allocation / extensions
```

OpenSandbox 1.2.0 status values include `Pending`, `Running`, `Pausing`, `Paused`, `Stopping`,
`Terminated`, `Failed`, and `Unknown`. Stage B normalizes these to uppercase and only attaches to
`RUNNING`; `TERMINATED` is treated as a definitive terminal state. Other states fail closed without
changing durable state.

The adapter uses the public SDK's structured exception information:

- `SandboxApiException.status_code`: 404/410 mean remote not found; other statuses are provider
  unavailable/mismatch failures;
- `SandboxConnectionException`, `SandboxTimeoutException`, and related SDK exceptions mean the
  control plane is temporarily unavailable;
- no `if "404" in str(error)` guessing is used.

Inspection goes through `SandboxManager.get_sandbox_info(exact_id)` only. It never lists sandboxes,
never guesses by metadata, and never calls `Sandbox.connect()`.

## Durable verification fields

`DurableSandboxBindingRecord` adds optional evidence:

```text
provider_control_plane_digest: str | None
provider_created_at: datetime | None
provider_metadata_digest: str | None
```

The SQLite store adds the same nullable columns and migrates old Stage A databases in place. Old
records load with these fields `None`.

These fields are proof material only. They are excluded from
`atlas/m5-sandbox-continuity/v1`; the M6 continuity digest does not change because of them.

`provider_control_plane_digest` is derived from provider name, normalized control-plane base URL, and
`use_server_proxy`. It excludes API keys, credentials, and client request timeouts.

`provider_created_at` is the OpenSandbox control-plane creation time, not the Atlas durable commit
time.

`provider_metadata_digest` is computed over the Atlas-prefixed provider metadata captured after
creation and re-read during inspection. Metadata is mutable, so it is correlation evidence—not
cryptographic ownership proof.

## Creation ordering

```text
provider.create()
→ control-plane inspect exact sandbox
→ capture provider_created_at / metadata digest / control-plane digest
→ provision workspace
→ durable binding commit
→ runtime handle cache
→ M7 READY evidence
```

If the control-plane inspection or durable commit fails, the newly created sandbox is best-effort
destroyed and no runtime binding is installed.

## Verification algorithm

`ProjectSandboxManager.reattach_verified(task_id, context)` runs under the existing task lock:

```text
load durable record
→ terminal READY check (LOST/CLOSED fail immediately)
→ require provider continuity evidence (legacy records fail)
→ current config_digest == durable config_digest
→ provider capabilities/provider name match
→ current control-plane digest == durable control-plane digest
→ inspect exact durable sandbox_id
→ state must be RUNNING
→ observed sandbox_id must match
→ provider_created_at must match exactly after UTC normalization
→ metadata digest and run/task/worker/provider correlation must match
→ only now: provider.attach_existing(sandbox_id)
→ build runtime ProjectSandboxBinding from durable identity + current trusted context
→ install runtime cache
```

No replacement sandbox is created anywhere in the path.

If runtime installation fails after a successful connect, the provider closes only its local client
and leaves the durable record `READY`; the remote sandbox is not destroyed.

## Failure classification

| Condition | Durable state | connect | create |
|---|---|---:|---:|
| LOST durable | unchanged LOST | 0 | 0 |
| CLOSED durable | unchanged CLOSED | 0 | 0 |
| READY without live handle, but reattach not requested | unchanged READY | 0 | 0 |
| legacy Stage A record lacks evidence | unchanged READY | 0 | 0 |
| current config digest mismatch | unchanged READY | 0 | 0 |
| control-plane identity mismatch | unchanged READY | 0 | 0 |
| provider unavailable/timeout | unchanged READY | 0 | 0 |
| exact ID not found (404/410) | READY → LOST | 0 | 0 |
| provider state `Terminated` | READY → LOST | 0 | 0 |
| provider state pending/paused/failed/unknown | unchanged READY | 0 | 0 |
| created_at mismatch | unchanged READY | 0 | 0 |
| metadata mismatch | unchanged READY | 0 | 0 |
| verification passed, connect failed | unchanged READY | attempted (failed) | 0 |
| successful reattach | READY, revision unchanged | 1 | 0 |

A remote-not-found result is a definitive provider statement and uses the existing CAS transition
`READY → LOST`. A failed inspection, config mismatch, control-plane mismatch, created-at mismatch, or
metadata mismatch is **not** proof that the physical sandbox disappeared, so durable `READY` is
preserved.

## Legacy Stage A records

Stage A records created before Stage B have no provider verification evidence. They remain readable
and usable by the same process that has a live handle, but `reattach_verified()` refuses to connect:

```text
SandboxLegacyContinuityUnavailable
```

Atlas does not fill in missing evidence by reading the current provider state. Doing so would be
self-certification.

## Local detach

`OpenSandboxProvider.detach_local(sandbox_id)` removes the local `_sandboxes` entry and calls the
SDK's local `Sandbox.close()`. It never calls `destroy()`/`kill()`.

## Non-goals

Stage B does **not** implement:

```text
exclusive cross-process ownership
preventing Process A and Process B from both operating S1
local OS binding lock
takeover
distributed fencing
true M6 OS-process restart
```

The next stage is **Stage C — Local Cross-Process Binding Ownership Lock**.
