# V2 M4 Formal Results — Durable Action Ledger & Idempotency

## Provenance

```text
starting HEAD:          3abcaf7
implementation commit:  450c4bb
verification commit:    450c4bb (git_dirty=false)
final docs commit:      pending
formal record:          experiments/runs/v2-m4-durable-actions.jsonl
```

## Quality gates

```text
ruff check .             All checks passed
ruff format --check .    160 files already formatted
pyright                  0 errors, 0 warnings, 0 informations
pytest                   204 passed
M4 integration:          9 passed
M4 unit ledger:          10 passed
M3 regression:           14 passed
Control-plane E2E:       5 passed
```

## Formal metrics

| Metric | Result |
|---|---|
| actions_recorded | 5 |
| side_effect_attempts | 3 |
| actual_side_effects | 3 |
| AnyCode execute claims | 6 |
| replays | 1 |
| in_progress / uncertain lifecycle | 1 |
| conflicts | 1 |
| provider_idempotent_retries | 1 |
| **provider_duplicate_effects** | **0** |
| local_only_uncertain_outcomes | 1 |
| **blind_retries_from_uncertain** | **0** |
| same_key_different_input_conflicts | 1 |
| ledger_recovery_successes | 1 |
| **stale_claims_auto_deleted** | **0** |
| **model_controlled_key_violations** | **0** |
| M1 regression | true |
| M2 regression | true |
| M3 regression | true |

## Verified behavior

### AnyCode authority

- AnyCode `ToolIdempotencyStore` remains the only execution-dedupe authority.
- M4 has no claim/complete/replay lock.
- M4 uses the existing `ToolExecutor`.
- Same key + same input → replay, no second Tool.execute.
- Same key + different input → conflict, no Tool.execute.
- Concurrent same key → one execute claim; other replay/in_progress.
- In_progress → no Tool.execute.
- Read tools are not replay-cached.

### Trusted identity

- `action_id` is stable across retries via the ledger key hash.
- `attempt_id` changes per attempt and is never an idempotency key.
- Runtime instance ID is persisted.
- New trusted key → new action identity.
- Resume reuses the same action identity.
- A model-supplied idempotency field that differs from the trusted key fails closed before
  ledger/claim/execution.
- Modified args with the old key conflict; no silent new key.

### Crash/recovery

- C0 no ledger; C1 ledger before claim; C2 claim before Tool; C3 effect before return;
  C4 return before complete; C5 complete before ledger; C6 terminal.
- Claimed/unresolved outcomes → UNCERTAIN, no auto-retry, no claim deletion.
- Complete-store failure → UNCERTAIN.
- Completed AnyCode result + incomplete ledger → safe recovery may finalize.
- Reconciler success finalizes; still-unknown stays UNCERTAIN.
- No TTL or stale timeout unlocks a claim.

### M3/M1/M2

- M3 DENY: no claim and no ledger record.
- HITL reject: no claim and no ledger record.
- M3-authorized final input reaches M4.
- Worker metadata reaches ledger.
- Full M1/M2/M3 regression passes.

## Guarantees

```text
durable local idempotency
safe replay
duplicate suppression
crash-aware UNCERTAIN handling
effectively-once only where the downstream provider honors the same key
```

No claim of exactly-once execution.

## Known limitations

- AnyCode has no universal public idempotency inspection API; M4 fails closed to UNCERTAIN
  without a safe adapter and does not query private tables.
- Ledger and AnyCode idempotency DB are separate SQLite stores without 2PC.
- HITL remains synchronous/current-run; no durable pending approval (M6).
- No real provider idempotency adapter; deterministic fakes only.
- No ledger pruning/TTL in V1.
- No cross-process/lease/distributed coordination.
- M4 gate remains opt-in via `AtlasToolRuntime(action_policy=..., action_ledger=...)`.

## Reproduction

```powershell
cd D:\Work\AtlasOS

.venv\Scripts\ruff.exe check .
.venv\Scripts\ruff.exe format --check .
.venv\Scripts\pyright.exe
.venv\Scripts\python.exe -m pytest

.venv\Scripts\python.exe -m pytest tests\integration\test_durable_actions.py
.venv\Scripts\python.exe -m pytest tests\integration\test_action_safety.py
.venv\Scripts\python.exe -m pytest tests\integration\test_control_plane_e2e.py

# Formal record requires clean worktree.
.venv\Scripts\python.exe examples\m4_durable_action_eval.py
```
