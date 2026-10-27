# M1–M3 Control-Plane Formal Results

## Provenance

```text
starting HEAD:          5ed1305
implementation commit:  368a7e3
git_dirty:              false
timestamp:              2026-10-27T15:32:23Z
formal record:          experiments/runs/v2-control-plane-e2e.jsonl
git status:             clean
```

## Quality gates

```text
ruff check .             All checks passed
ruff format --check .    146 files already formatted
pyright                  0 errors, 0 warnings, 0 informations
pytest                   185 passed
```

## Formal metrics

| Metric | Result |
|---|---|
| workers_created | 2 |
| tool_search_calls | 9 |
| tools_disclosed | 14 (sum across two runs) |
| hidden_tool_guess_rejections | 2 |
| M3_action_requests | 24 |
| ALLOW | 15 |
| DENY | 1 |
| REQUIRE_HUMAN | 8 |
| Reviewer_invocations | 1 |
| HITL_approvals | 3 |
| HITL_rejections | 3 |
| unknown_actions | 2 |
| hard_security_denials | 1 |
| **unauthorized_executions** | **0** |
| **hard_deny_overrides** | **0** |
| **M3_bypasses** | **0** |
| **stale_approvals_accepted** | **0** |
| **modified_input_old_approval_reuse** | **0** |
| **trusted_field_mutations** | **0** |
| **ExecutionContext_alias_violations** | **0** |
| **recovery_hash_match** | **true** |
| M1 regression | pass |
| M3 regression | pass |

## Module verdicts

```text
M1:                PASS
M2:                PASS
M3:                PASS
M1 × M2:           PASS
M2 × M3:           PASS
M1 × M2 × M3:      PASS
```

### M1

- hidden searchable target rejected as `tool_not_disclosed`, no execution;
- `tool_search` discovers GitHub/logs/code tools;
- disclosure state monotonic within run;
- `dispatch_action` targets pass M3;
- searchable tools stay out of the native surface;
- recovery restores disclosure state;
- run-shared disclosure is explicit; a separate run/state sidecar has no disclosure.

### M2

- research/coding workers built from trusted profiles;
- distinct ExecutionContexts with principal/tenant/classification preserved;
- malicious task text does not change profile, coarse scope, base tools, can_delegate, or context;
- coding worker has new worker/task IDs and no recursive delegation;
- worker recovery preserves worker ID, task, profile, scope, context and rendered hash;
- no capability grant/policy/HITL in construction.

### M3

- every real action passes the global gate (main/worker/native/searchable/dispatch target/batch);
- safe read ALLOW + aligned/no reviewer;
- email REQUIRE_HUMAN, reject/approve and idempotency behavior;
- stale approval and modified-input old-approval reuse rejected;
- outside-workspace hard deny before HITL;
- unknown catalog action REQUIRE_HUMAN, never guessed;
- no approver fails closed;
- batch calls independently authorized;
- main agent and worker have no bypass.

## E2E determinism

Fixed worker/task IDs and deterministic fake tools. The test repeats worker rendering/hash,
M1 search state, and M3 safe action digest. Timestamps are not asserted.

## Limitations

- M1 disclosure is run-shared, not per-worker private; a separate run/state sidecar is isolated.
- HITL is synchronous/current-run; no durable pending approval (M6).
- No Action Ledger (M4), sandbox (M5), velocity limiter, OPA/Rego, or identity forwarding.
- Fake tools/local approvers only; no real GitHub/MCP/email/LLM/network.
- M3 gate remains opt-in at `AtlasToolRuntime(action_policy=...)`.
- The formal evaluator's aggregate counters supplement, not replace, the focused E2E assertions.

## Reproduction

```powershell
cd D:\Work\AtlasOS

.venv\Scripts\ruff.exe check .
.venv\Scripts\ruff.exe format --check .
.venv\Scripts\pyright.exe
.venv\Scripts\python.exe -m pytest

.venv\Scripts\python.exe -m pytest tests\integration\test_control_plane_e2e.py

# Formal record requires clean worktree.
.venv\Scripts\python.exe examples\m1_m2_m3_control_plane_eval.py
```
