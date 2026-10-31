# V2 M6 — Durable Approval Interrupt: Formal Results

Status: **PASS / FROZEN**

Formal evaluation was run from a clean worktree at:

```text
commit:      62a667e65a4649eb0606b68a7a59625978a71636
git_dirty:   false
timestamp:   2026-10-31T08:06:23.882151Z
python:      3.12.15
anycode-py:  0.9.0
sqlite:      3.53.1
```

The deterministic evaluator is
[`examples/m6_durable_approval_eval.py`](../../examples/m6_durable_approval_eval.py). It ran the
complete scenario twice in separate directories and compared every metric before recording. The
raw record is appended to
[`experiments/runs/v2-m6-durable-approval.jsonl`](../runs/v2-m6-durable-approval.jsonl).

## Result

```text
M6 — Durable Approval Interrupt
PASS / FROZEN

Runtime Suspension Protocol
PASS / FROZEN

M1 × M2 × M3 × M4 × M5 × M6
PASS
```

## Formal counters

```json
{
  "runtime_suspensions_created": 10,
  "runtime_suspension_reloads": 0,
  "runtime_suspension_conflicts": 0,
  "approval_challenges_created": 10,
  "approval_create_dedupes": 0,
  "approval_approved": 6,
  "approval_rejected": 1,
  "approval_resolution_conflicts": 0,
  "orphan_approvals": 0,
  "approval_superseded": 0,
  "tool_batches_preflighted": 11,
  "tool_calls_preflighted": 14,
  "tool_calls_entered_m4_before_required_approval": 0,
  "llm_reexecutions_during_suspension_resume": 0,
  "m3_revalidations": 12,
  "m3_new_denies_after_approval": 1,
  "stale_action_digests": 0,
  "stale_decision_basis": 1,
  "stale_tool_bindings": 1,
  "m4_attempts_after_approval": 6,
  "m4_replays": 1,
  "m4_conflicts": 0,
  "m4_uncertain": 0,
  "provider_side_effects": 5,
  "duplicate_provider_side_effects": 0,
  "tool_results_committed": 9,
  "tool_results_committed_twice": 0,
  "suspensions_cleared": 7,
  "suspensions_cleared_before_result_commit": 0,
  "late_approval_resumes": 0,
  "late_approval_attempts": 1,
  "model_self_approval_attempts": 0,
  "timeout_auto_approvals": 0,
  "m1_resume_violations": 0,
  "m2_identity_violations": 0,
  "m5_resume_violations": 0,
  "unauthorized_executions": 0
}
```

`late_approval_attempts` is an observed refusal of a late approval for a cancelled run; it is not
a successful late resume. `late_approval_resumes` remains `0`.

## Scenario checks

All of the following were `true` in both complete evaluation runs:

```text
batch_preflight_before_m4
restart_exact_pending_without_llm
approve_resumes_exact_action
reject_no_m4
current_deny_overrides_approval
changed_basis_invalidates_receipt
changed_binding_fails_closed
m1_unavailable_fails_closed
m2_identity_mismatch_fails_closed
m5_lost_sandbox_fails_closed
m4_replay_reconstructs_result_once
tool_result_durable_before_clear
two_resumes_one_continuation
two_reviewers_one_terminal
terminal_run_ignores_late_approval
unsupported_schema_fails_closed
approval_db_has_no_raw_secret
normal_allow_path_unchanged
wake_signal_loss_does_not_lose_run
agent_loop_llm_not_recalled_for_pending_action
cross_process_create_approve_receipt
cross_process_duplicate_create_single_row
```

## Hard-zero invariants

```text
tool_calls_entered_m4_before_required_approval = 0
llm_reexecutions_during_suspension_resume = 0
duplicate_provider_side_effects = 0
tool_results_committed_twice = 0
suspensions_cleared_before_result_commit = 0
late_approval_resumes = 0
model_self_approval_attempts = 0
timeout_auto_approvals = 0
m1_resume_violations = 0
m2_identity_violations = 0
m5_resume_violations = 0
unauthorized_executions = 0
```

## Focused verification

```text
tests/integration/test_m6_runtime_suspension.py    22 passed
tests/integration/test_m6_agent_runner.py           2 passed
tests/integration/test_m6_failure_matrix.py        10 passed
tests/integration/test_m6_sandbox_resume.py         1 passed
tests/integration/test_m6_sqlite_process.py          4 passed
```

The cross-process tests use real independent Python processes against the SQLite approval store
(P01 create -> approve -> receipt, P02 approve/reject race, P03 duplicate create, P04 restart
after approval).

## Repository gates

Final clean-tree verification was run at
`87070af8933b3e0dfb1251d8722b711ca4325b0e` (implementation `62a667e`, formal record `e794162`;
later commits are the results document and a self-contained-import fix in the evaluator, with no
runtime behavior change):

```text
ruff check .             PASS
ruff format --check .    PASS
pyright                  PASS (0 errors)
python -m pytest         PASS (273 passed, 1 live test deselected, run twice)
```

The full suite includes the pre-existing M1-M5, control-plane, sandbox, MCP, context, scheduler,
and durability tests. The M5/MCP tests require native child-process pipes; they were verified in
an unconfined run because the workspace sandbox blocks named-pipe creation.

## Invariants demonstrated

```text
Atlas has one Runtime.
ExecutionSuspension is Runtime state, not approval state.
A suspended run restarts without calling the LLM to reconstruct the pending action.
All ToolCalls in one model response are preflighted before any reaches M4.
No side effect occurs before required approvals are terminal.
Approval cannot override a current M3 DENY.
M4 remains the sole execution dedupe/replay authority.
M4 replay reconstructs a missing ToolResult without repeating the effect.
ToolResult is durable before the suspension is cleared.
Changed tool binding/schema cannot reuse an old approval.
A lost M5 sandbox is never recreated on resume.
The model cannot approve itself.
Two reviewers cannot overwrite each other.
Two runtime processes cannot commit two continuations.
A late approval cannot resume a cancelled run.
Normal non-HITL execution preserves prior behavior.
```

No exactly-once claim is made. M4 provides durable dedupe/replay; downstream provider idempotency
remains the provider's responsibility.
