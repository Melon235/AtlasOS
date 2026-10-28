# V2 M4 Idempotency Guarantees

## AnyCode remains authoritative

```text
ToolIdempotencyStore.claim() / complete()
= the only execution deduplication authority
```

M4 adds no claim engine, replay lock, conflict lock, or replacement ToolExecutor. It records
lifecycle around the existing executor.

## Trusted key precedence

The audited AnyCode behavior:

```text
ToolDefinition.idempotency_key_field
  > executor fallback idempotency_key
```

For M4-managed side effects, Atlas requires a trusted runtime key. The bridge passes that key as
the executor fallback. If a model-facing idempotency field is present and differs from the trusted
key, M4 fails closed before ledger/claim/execution; it does not silently overwrite or accept the
model value.

If no trusted key exists:

```text
fail closed before ledger/claim/execution
```

Batch calls use their own runtime call identities; a batch ID is never one shared idempotency key
for all side effects.

## Same key semantics

```text
same key + same validated input   → AnyCode replay, Tool.execute not called again
same key + different input        → AnyCode conflict, Tool not executed
same key concurrent               → exactly one execute claim; other attempt replays or reports in_progress
```

M4 never silently creates a new key to escape a conflict.

## Read tools

Only `side_effecting=True` tools enter durable execution semantics. Read results remain live and
are not replayed from the M4 ledger.

## Guarantee tiers

### Provider-enforced idempotency

```text
trusted stable Atlas/AnyCode key
+ downstream provider honors the same key
→ effectively-once where provider semantics guarantee it
```

Provider clients receive the trusted key through `ToolUseContext.idempotency_key` (or their
provider-specific adapter). M4 only defines the contract; no real provider integration is added.

### Local-only idempotency

```text
local AnyCode durable claim
+ downstream has no idempotency support
→ crash may leave UNCERTAIN
```

This must not be described as exactly-once. The safe behavior is no automatic retry and optional
manual/reconciler resolution.

## Replay vs execution

The ledger records `REPLAYED` when a prior completed record with the same trusted key and request
digest exists and AnyCode returns the cached result. It records `CONFLICT` for same-key/different-
input. It does not copy the cached ToolResult; replay remains AnyCode's responsibility.

## Expiration

No automatic pruning of IN_PROGRESS or UNCERTAIN. A future pruning policy may apply only to
COMPLETED records with explicitly safe retention rules.
