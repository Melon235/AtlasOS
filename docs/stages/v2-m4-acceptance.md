# V2 M4 Acceptance Matrix

## Ledger

| ID | Requirement | Verification |
|---|---|---|
| M4-01 | immutable/strict record | `test_m4_01_record_is_immutable_and_strict` |
| M4-02 | only IN_PROGRESS/COMPLETED/UNCERTAIN | `test_m4_02_state_enum_is_small` |
| M4-03 | no raw sensitive payload | `test_m4_03_no_raw_arguments_or_results_in_record` |
| M4-04 | deterministic request digest | `test_m4_04_request_digest_deterministic` |
| M4-05 | explicit digest version | `test_m4_05_digest_version_explicit` |
| M4-06 | action_id != attempt_id | `test_m4_06_action_and_attempt_ids_are_separate` |
| M4-07 | runtime_instance_id stored | `test_m4_07_runtime_instance_id_stored` |
| M4-08 | restart persistence | `test_m4_08_ledger_persists_across_restart` |
| M4-09 | completed reload | `test_m4_09_completed_record_reloads` |
| M4-10 | uncertain reload | `test_m4_10_uncertain_record_reloads` |

## AnyCode integration

| ID | Requirement | Verification |
|---|---|---|
| M4-11 | same key/input replay | `test_m4_11_to_16_*` |
| M4-12 | same key/different input conflict | `test_m4_11_to_16_*` |
| M4-13 | concurrent same key, one execute | `test_m4_11_to_16_*` |
| M4-14 | replay does not call Tool.execute | `test_m4_11_to_16_*` |
| M4-15 | conflict does not call Tool.execute | `test_m4_11_to_16_*` |
| M4-16 | in_progress does not call Tool.execute | `test_m4_11_to_16_*` |
| M4-17 | read tool not replay-cached | `test_m4_17_read_tool_not_cached` |

## Failure injection

| ID | Requirement | Verification |
|---|---|---|
| M4-18 | crash before ledger | `test_m4_18_to_24_crash_windows` |
| M4-19 | crash after ledger before claim | same |
| M4-20 | crash after claim before Tool | same |
| M4-21 | crash after external effect before return | same |
| M4-22 | crash after Tool return before complete | same |
| M4-23 | complete-store failure | same |
| M4-24 | crash after complete before ledger | same + recovery test |

## Stable identity

| ID | Requirement | Verification |
|---|---|---|
| M4-25 | retry same trusted identity | `test_m4_25_to_30_identity_and_model_key` |
| M4-26 | new independent action new identity | same |
| M4-27 | attempt change does not change key | same |
| M4-28 | model cannot override trusted key | same |
| M4-29 | resume preserves action identity | same |
| M4-30 | modified args conflict with old key | same |

## Provider idempotency

| ID | Requirement | Verification |
|---|---|---|
| M4-31 | same provider key, one effect | `test_m4_31_to_33_provider_idempotency` |
| M4-32 | crash/retry provider key no duplicate | provider cache + bridge resume test |
| M4-33 | different payload/provider key conflict | `test_m4_31_to_33_provider_idempotency` |

## Recovery

| ID | Requirement | Verification |
|---|---|---|
| M4-34 | completed AnyCode result restores safely | `test_m4_34_to_39_recovery_and_non_idempotent` |
| M4-35 | stale unresolved → UNCERTAIN | same |
| M4-36 | unresolved claim not auto-deleted | same |
| M4-37 | unknown outcome not auto-retried | same |
| M4-38 | reconciler success finalizes | same |
| M4-39 | reconciler unknown stays UNCERTAIN | same |

## Regressions

| Area | Verification |
|---|---|
| M3 DENY/HITL reject no claim/ledger | `test_m3_deny_and_hitl_reject_do_not_claim` |
| M3 final authorized input reaches M4 | `test_m4_runtime_places_ledger_below_m3` |
| Worker metadata reaches ledger | `test_m4_worker_metadata_reaches_ledger` |
| M1/M2/M3 full regression | full `pytest` + control-plane E2E |

## One-vote checks

- no second idempotency claim engine;
- ledger cannot authorize execution;
- no stale claim deletion/TTL unlock;
- no blind UNCERTAIN retry;
- no attempt-id-as-key;
- no model-owned key in managed mode;
- no silent new key on conflict;
- no read-result cache;
- no raw secret payload;
- no exactly-once claim;
- no replacement ToolExecutor;
- no M3 semantic rewrite;
- DENY/HITL reject consume no claim;
- provider/local guarantees kept distinct.
