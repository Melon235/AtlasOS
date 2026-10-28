# V2 M4 — Durable Action Ledger & Idempotency

Status: implementation and verification in progress. Baseline: control-plane final `3abcaf7`.

```text
M3 = MAY THIS ACTION EXECUTE?
M4 = HAS THIS ACTION EXECUTED, AND CAN IT BE SAFELY REPLAYED?
```

## Architecture

```text
Concrete Tool Call
      ↓
M3 ActionSafetyGate
  DENY / rejected HITL → STOP (no claim, no ledger)
      ↓ authorized final input
M4 DurableActionBridge
  create IN_PROGRESS ledger record
      ↓
AnyCode ToolExecutor
  IdempotencyStore.claim()
  execute / replay / in_progress / conflict
  IdempotencyStore.complete()
      ↓
M4 update ledger COMPLETED / UNCERTAIN
```

M4 is a thin recorder/delegate below M3. It does not own a registry, authorize actions,
implement policy/HITL, or execute tools. `ActionSafetyGate` still wraps the recording delegate;
AnyCode still owns registration, execution and execution deduplication.

## Authority separation

```text
AnyCode IdempotencyStore
  = the only execution-deduplication authority

Atlas ActionLedger
  = durable execution history and recovery classification
```

The ledger never decides that a Tool may execute. A stale or unfinished ledger record cannot
unlock execution; every side-effecting attempt still goes through AnyCode `claim()`.

## Ledger record

Minimal fields:

```text
action_id / attempt_id / runtime_instance_id
run_id / task_id / worker_id / agent
tool_name / canonical_action / resource
request_digest / digest_version
idempotency_key_hash
state / claim_outcome / completion_kind
result_digest / retry_safe
created_at / updated_at / completed_at
```

Not persisted:

```text
raw arguments or ToolResult
secrets, tokens, credentials
full email body / arbitrary payload
```

The request digest is observational. AnyCode's validated-input fingerprint remains the conflict
authority.

## States

```text
IN_PROGRESS   tracking started, outcome not durably confirmed
COMPLETED     terminal lifecycle with a completion_kind
UNCERTAIN     external effect may have happened; must not auto-retry
```

`completion_kind` is one of:

```text
EXECUTED_SUCCESS
EXECUTED_ERROR
REPLAYED
CONFLICT
BLOCKED_IN_PROGRESS
RECONCILED_SUCCESS
RECONCILED_NOT_EXECUTED
```

## Identity separation

```text
action_id
  logical side-effecting action

attempt_id
  one runtime attempt of that action

idempotency_key
  stable cross-retry execution dedupe identity

M3 call/digest
  separate authorization audit fact
```

Attempts do not generate new idempotency keys. `ActionIdentityProvider` maps a trusted stable key
to a persisted action ID via the ledger. New independent actions require new trusted keys.

## Integration

`AtlasToolRuntime(action_ledger=..., action_policy=...)` installs:

```text
raw AnyCode ToolExecutor
  → DurableActionBridge
  → ActionSafetyGate
  → AtlasDispatchExecutor
```

`action_ledger` without `action_policy` is rejected; M4 never runs before/without M3.

## Non-side-effecting tools

Only `ToolDefinition.side_effecting=True` enters ledger/idempotency semantics. Read tools are
never replay-cached by M4.

## Guarantees

```text
durable idempotency
safe replay
duplicate suppression
crash-aware uncertain outcome handling
effectively-once where a downstream provider honors the same key
```

No claim of exactly-once execution. Local-only external side effects may be uncertain after a
crash. See [failure model](v2-m4-failure-model.md) and
[idempotency guarantees](v2-m4-idempotency.md).
