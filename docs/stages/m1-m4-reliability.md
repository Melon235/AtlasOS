# M1–M4 Integrated Reliability Experiment

Status: PASS (with documented limitations). Scope is verification and failure injection, not a new
runtime stage.

The experiment verifies the existing:

```text
M1 Progressive Tool Runtime
M2 Scoped Worker Construction
M3 Global Action Safety
M4 Durable Action Ledger
AnyCode ToolIdempotencyStore
```

No production authorization/idempotency semantics were rewritten. One additive M1 integration seam
was required: `AtlasDispatchExecutor.execute_batch(..., idempotency_keys=...)` allows a trusted
logical key to be supplied independently of the per-call batch ID. Without it, batch entries could
not express “duplicate/conflict of B” while retaining distinct result slots.

## Deterministic fixtures

Fake providers:

```text
FakeIdempotentProvider       same key + same payload → cached result/no second effect
                             same key + different payload → provider conflict
FakeNonIdempotentProvider    effect occurs then transport failure
FreshCounter                 each read returns a new value
```

Fake tools: `incident.read`, `logs.search`, `payment.charge`, `email.send`,
`github.issue.create`, `unsafe_external.mutate`, `fresh_counter.read`,
`vendor.unknown_mutation`, plus deterministic crash/cancel variants.

Approvals are scripted approve/reject/modify/crash; no network, LLM, GitHub, email, MCP, or real
provider is used.

## Experiments

| Area | Coverage |
|---|---|
| M1 | hidden `payment.charge` rejected as `tool_not_disclosed`; disclosure works; native surface unchanged |
| M2 | malicious task cannot change profile/scope/tools/identity; worker context remains distinct |
| M3 | DENY and HITL reject occur before AnyCode claim/M4 record; modified input reauthorizes |
| M4 | normal execution, replay, conflict, read freshness, provider/local uncertainty |
| Concurrency | 50 deterministic two-caller races; exactly one provider effect per race |
| Failure injection | F1 before ledger; F2 after ledger before claim; F3 after claim before Tool; F4 after external effect; F5 complete failure; F6 complete-before-ledger; F7 terminal |
| Recovery | completed AnyCode result repairs ledger; unresolved/uncertain never auto-retries |
| Restart | M1 disclosure, M2 worker, M4 ledger and completed action replay survive runtime restart |
| Pending HITL | crashed approval is not restored as approved; no claim/effect after restart |
| Cancellation | after claim/effect becomes UNCERTAIN; no blind retry |
| Reconciler | success finalizes; unknown remains UNCERTAIN; not-executed is reconcilable but does not delete unresolved claim |
| Model key | model-supplied key fails closed; no provider effect or ledger lifecycle |

## Failure model mapping

```text
F1  before_record hook        no ledger, no claim, no effect
F2  after_record hook         ledger IN_PROGRESS, no claim; retry must claim
F3  Tool crash before effect  claim unresolved, ledger UNCERTAIN
F4  effect then crash         provider effect once, ledger UNCERTAIN, no retry
F5  complete() failure        tool result unknown, ledger UNCERTAIN
F6  complete then ledger crash AnyCode completed, ledger IN_PROGRESS; repair possible
F7  ledger COMPLETED          replay/no second effect
```

## Determinism

Fixed worker IDs, fixed action keys, deterministic fake providers, no random crash probabilities.
The formal evaluator runs the full scenario twice and compares all metrics except timestamp.

## Limitations

- AnyCode `SQLiteToolIdempotencyStore` requires the optional `anycode-py[persistence]`
  (`aiosqlite`) extra, which is not installed in this environment. The restart experiment shares
  one AnyCode in-memory store across runtime instances and persists M1/M2/M4 sidecars; it does not
  prove cross-process AnyCode SQLite durability here.
- Durable HITL is M6; pending approval is intentionally not restored.
- No real provider adapters; provider idempotency is deterministic fake behavior.
- No TTL/pruning logic; stale unresolved claims are never auto-deleted.
- `CONFIRMED_NOT_EXECUTED` cannot safely clear an unresolved AnyCode claim; this remains a
  documented limitation rather than a deletion.
- Reconciliation is an interface + fake implementation, not a production external system.

## Metrics invariants

```text
unauthorized_executions = 0
M3_bypasses = 0
provider_duplicate_effects = 0
blind_retries_from_uncertain = 0
stale_claims_auto_deleted = 0
ttl_based_unsafe_retries = 0
model_key_bypass = 0
replay_tool_executions = 0
trusted_state_mutations = 0
```

See [formal results](../../experiments/results/v2-m1-m4-reliability-results.md) and the
implementation files:

```text
tests/integration/test_m1_m4_reliability.py
examples/m1_m4_failure_injection_eval.py
```
