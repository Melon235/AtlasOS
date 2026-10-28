# V2 M4 Failure Model

## Crash windows

| Point | Durable state at crash | Recovery semantics |
|---|---|---|
| C0 before ledger | no ledger record, no claim | no effect to recover; a new authorized attempt may start normally |
| C1 ledger IN_PROGRESS, before claim | ledger record, no AnyCode claim | no claim exists; recovery may schedule a new attempt, but it must still pass M3 and AnyCode `claim()` |
| C2 claim=execute, before Tool | unresolved AnyCode claim, ledger IN_PROGRESS/UNCERTAIN | **UNCERTAIN**; do not delete claim, do not auto-retry |
| C3 external effect occurs, before Tool returns | unresolved claim, possible external effect | **UNCERTAIN**; no automatic retry |
| C4 Tool returns, before AnyCode complete | claim unresolved; no durable result | **UNCERTAIN** unless an upstream-safe inspection proves a completed result |
| C5 AnyCode complete succeeds, before ledger update | AnyCode result is durable; ledger IN_PROGRESS | recovery may finalize ledger COMPLETED from the trusted AnyCode result |
| C6 ledger COMPLETED | terminal | replay follows AnyCode replay semantics; no second effect |

## Exception semantics

A Tool exception does not prove that no external effect occurred. Side-effecting failures are
recorded as UNCERTAIN unless they are known pre-execution failures (validation/registration/claim
failure). No claim is deleted merely because the Tool raised.

## Complete-store failure

If the Tool finished but AnyCode `complete()` failed, AnyCode reports an unknown outcome with
`retry_safe=False`; M4 records UNCERTAIN. No automatic retry.

## Cancellation

Cancellation after a claim is treated as UNCERTAIN because the external effect may have occurred.
Cancellation is not converted into a claim deletion or safe retry.

## Timeouts and stale records

Age may identify a recovery candidate, never a safe retry. There is no TTL that deletes an
unresolved claim or turns UNCERTAIN into NOT_EXECUTED.

## Recovery classification

`RecoveryClassifier` returns:

```text
COMPLETED     ledger already terminal
ACTIVE        unfinished record from the current runtime instance
UNCERTAIN     unresolved/unknown; no auto retry
RECONCILABLE  no claim exists, or reconciler confirms not executed; AnyCode claim still required
```

Optional `ActionReconciler` results:

```text
CONFIRMED_SUCCESS        → ledger COMPLETED
CONFIRMED_NOT_EXECUTED   → RECONCILABLE, but an unresolved AnyCode claim is not deleted
STILL_UNKNOWN            → UNCERTAIN
```

## Store consistency

The SQLite Action Ledger and AnyCode idempotency store are separate stores with no 2PC/distributed
transaction. Short-lived inconsistency is expected and is handled by recovery/reconciliation, not
by a transaction coordinator.

## Known gap

AnyCode V1 exposes `claim/complete/delete/prune_completed` but no universal public inspection API
for unresolved/completed results. M4 therefore fails closed to UNCERTAIN when no safe observation is
available. A production adapter may add a minimal upstream-safe `inspect(tool_name,key)` protocol;
Atlas does not query AnyCode private tables.
