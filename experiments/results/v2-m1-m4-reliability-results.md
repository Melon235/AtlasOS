# M1–M4 Reliability Formal Results

## Provenance

```text
starting HEAD:         850a14f
experiment commit:     d429565
verification commit:   d429565 (git_dirty=false)
formal record:         experiments/runs/v2-m1-m4-reliability.jsonl
```

## Test results

```text
full pytest:            211 passed
M1–M4 reliability:       7 passed
M4 focused:              9 integration + 10 ledger unit
M3 regression:          14 passed
Control-plane E2E:       5 passed
```

```text
ruff check .             All checks passed
ruff format --check .    165 files already formatted
pyright                  0 errors, 0 warnings, 0 informations
```

## Formal metrics

| Metric | Result |
|---|---|
| workers_created | 1 |
| tool_search_calls | 6 |
| tools_disclosed | 7 |
| hidden_tool_guess_rejections | 1 |
| M3_action_requests | 21 |
| M3_DENY | 2 |
| M3_REQUIRE_HUMAN | 2 |
| M3_bypasses | **0** |
| M4_actions_recorded | 14 |
| M4_completed | 10 |
| M4_uncertain | 3 |
| AnyCode_execute_claims | 12 |
| AnyCode_replays | 3 |
| AnyCode_in_progress | 3 |
| AnyCode_conflicts | 2 |
| tool_execute_calls | 10 |
| provider_side_effect_attempts | 7 |
| provider_actual_side_effects | 7 |
| **provider_duplicate_effects** | **0** |
| read_tool_real_executions | 4 |
| **blind_retries_from_uncertain** | **0** |
| **stale_claims_auto_deleted** | **0** |
| **ttl_based_unsafe_retries** | **0** |
| same_key_different_input_conflicts | 2 |
| **model_key_bypass** | **0** |
| **replay_tool_executions** | **0** |
| recovery_repairs | 1 |
| **trusted_state_mutations** | **0** |
| **unauthorized_executions** | **0** |
| M1 / M2 / M3 / M4 regression | true |

The formal evaluator runs the complete deterministic scenario twice and compares every metric except
timestamp before writing the record. Both runs were identical.

## Experiment outcomes

### M1

- hidden `payment.charge` rejected as `tool_not_disclosed`;
- no ledger record, no AnyCode claim, no provider effect;
- search disclosed payment while native surface stayed unchanged;
- unrelated tools remained hidden;
- read tool executed twice and returned fresh results.

### M2

- malicious task text could not change profile, scope, base tools, worker identity, or
  idempotency semantics;
- worker context distinct, principal preserved;
- resume preserved context hash.

### M3

- DENY and HITL reject produced no M4 record and no claim;
- modified input re-authorized and only final body reached execution;
- pending/crashed approval was not restored as approved after restart.

### M4

- normal provider-enforced side effect once;
- exact replay returned previous result with no Tool execution;
- same-key/different-input conflict with no new effect;
- 50 repetitions of two concurrent callers produced exactly one provider effect per race;
- read calls were not replay-cached;
- model key rotation failed closed;
- two independent logical actions with identical args used different action IDs and both executed;
- batch entries had independent identities/outcomes; duplicate and conflict used an explicitly
  supplied trusted logical key, while the M3-denied entry had no claim;
- no stale claim deletion and no TTL-based retry; aged unresolved records (up to simulated 30 days) remained unresolved.

### Failure injection

```text
F0 M3 approval crash     no ledger/claim/effect; pending not restored
F1 before ledger         no ledger/claim/effect
F2 after ledger          IN_PROGRESS, no claim; retry still claimed
F3 after claim           UNCERTAIN; no effect; retry in_progress
F4 after effect          one effect; UNCERTAIN; no blind retry
F5 complete failure      UNCERTAIN; no second execution
F6 complete before ledger IN_PROGRESS; safe recovery to COMPLETED
F7 terminal              replay; one effect
```

Cancellation after claim/effect produced UNCERTAIN and no retry.

## Minimal integration fix

`AtlasDispatchExecutor.execute_batch` now accepts an optional trusted
`idempotency_keys: dict[str, str]`. This expresses a logical idempotency identity independently of
the per-call batch result ID. Without it, batch B/C/D could not share an execution identity while
retaining separate results. AnyCode remains the claim authority; M4 still records each attempt.

## Limitations

- AnyCode `SQLiteToolIdempotencyStore` requires the optional `anycode-py[persistence]`
  (`aiosqlite`) extra, unavailable here. Runtime restart was tested with one shared AnyCode
  in-memory store plus persistent M1/M2/M4 sidecars; cross-process AnyCode SQLite durability is not
  proven by this run.
- Durable HITL is M6; pending approval intentionally does not survive restart.
- Provider idempotency is a deterministic fake contract, not a real Stripe/GitHub/email adapter.
- `CONFIRMED_NOT_EXECUTED` cannot safely release an unresolved AnyCode claim; no deletion was
  performed.
- No TTL/pruning policy exists; stale unresolved claims remain.
- Reconciliation is interface + fake implementation.

## Reproduction

```powershell
cd D:\Work\AtlasOS

.venv\Scripts\ruff.exe check .
.venv\Scripts\ruff.exe format --check .
.venv\Scripts\pyright.exe
.venv\Scripts\python.exe -m pytest

.venv\Scripts\python.exe -m pytest tests\integration\test_m1_m4_reliability.py
.venv\Scripts\python.exe -m pytest tests\integration\test_durable_actions.py
.venv\Scripts\python.exe -m pytest tests\integration\test_action_safety.py
.venv\Scripts\python.exe -m pytest tests\integration\test_control_plane_e2e.py

# Formal record requires clean worktree.
.venv\Scripts\python.exe examples\m1_m4_failure_injection_eval.py
```
