# V2 M7 — Durable Agent Trajectory: Formal Results

Status: **PASS / FROZEN**

Formal evaluation was run from a clean worktree at:

```text
commit:      110b4396dcc6cf75d5cc162e9bde12c5efe9b3f0
git_dirty:   false
timestamp:   2026-11-02T10:49:10.501262Z
python:      3.12.15
anycode-py:  0.9.0
sqlite:      3.53.1
```

The deterministic evaluator is
[`examples/m7_trajectory_eval.py`](../../examples/m7_trajectory_eval.py). It ran the complete
scenario twice in separate temp directories and compared every metric before recording. The raw
record is in `experiments/runs/v2-m7-trajectory.jsonl` (the `experiments/runs/` directory is
gitignored, matching earlier stages).

The formal counters describe implementation commit `110b439`. A later reader-audit test for native
`tool_result` deduplication was added after the formal run; it does not change any implementation
behavior or any formal counter. The final tree therefore has one more focused test and one more
overall test than the formal commit.

## Result

```text
M7 — Durable Agent Trajectory
PASS / FROZEN

MODEL ACTION != AUTHORIZED ACTION
AUTHORIZED ACTION != EXECUTED ACTION
EXECUTED ACTION != OBSERVED RESULT
M7 IS EVIDENCE, NOT AUTHORITY
```

## Scenario

One run (`run-m6`), one scoped worker, one fixed three-call model response:

```text
call-a  file_read       ALLOW   sandbox project file read
call-b  email.send      HUMAN   executable side effect
call-c  docs.search     ALLOW   read-only dispatch
```

The whole response was suspended before M4; the ApprovalStore persisted the human decision; a
fresh runtime resumed the exact pending invocation without an LLM call; M3 revalidated; M4 executed
once and replayed once after an injected crash between M4 completion and M6 settlement; the
ToolResult continuation was committed; the suspension cleared; the next turn completed. M5 kept the
same READY sandbox binding across the crash, and the run terminal status was recorded.

## Formal counters

```json
{
  "trajectory_physical_events": 37,
  "trajectory_logical_events": 32,
  "trajectory_events_backfilled": 0,
  "trajectory_evidence_gaps": 0,
  "trajectory_evidence_conflicts": 0,
  "trajectory_decode_failures": 0,
  "action_proposed_count": 3,
  "action_decision_count": 10,
  "execution_outcome_count": 2,
  "approval_event_count": 1,
  "suspension_event_count": 4,
  "native_tool_observations_duplicated": 0,
  "consistency_checks": 5,
  "consistency_passes": 5,
  "consistency_failures": 0,
  "consistency_unknown": 0,
  "provider_side_effects": 1,
  "duplicate_provider_side_effects": 0,
  "llm_calls_before_resume": 1,
  "llm_calls_after_resume": 1,
  "llm_calls_after_continuation": 2
}
```

The physical/logical difference is deliberate: the failed first resume and the successful retry
each recorded the same five logical resume-revalidation decisions, so the reader deduplicated five
physical duplicates by deterministic `event_id`. No conflict or last-write-wins was applied.

## Hard-zero authority metrics

```json
{
  "runtime_decisions_made_by_m7": 0,
  "m4_claims_created_by_m7": 0,
  "provider_executions_triggered_by_m7": 0,
  "run_resumes_triggered_by_m7": 0,
  "sandbox_creations_triggered_by_m7": 0,
  "approval_transitions_triggered_by_m7": 0,
  "llm_calls_triggered_by_reconciliation": 0
}
```

The evaluator also verified that reconciliation appended zero events, that the M4 ledger count and
approval challenge version were unchanged, and that the sandbox provider creation count was
unchanged. M7 evidence did not execute, authorize, replay, resume, approve, create a sandbox, or
call the model.

## Scenario checks

```json
{
  "one_run_one_worker_three_calls": true,
  "whole_response_suspension_before_m4": true,
  "restart_exact_pending_without_llm": true,
  "approval_persisted": true,
  "resume_revalidated_m3": true,
  "m4_executed_then_replayed": true,
  "tool_result_continuation_committed": true,
  "suspension_cleared": true,
  "m5_continuity_verified": true,
  "run_terminal_recorded": true,
  "reconciliation_appended_zero": true,
  "sensitive_metadata_only": true,
  "m7_never_became_authority": true
}
```

## What was verified

- All three `ACTION_PROPOSED` events share `model_response_id="resp-0"` and ordinals `0,1,2`, and
  precede the first `ACTION_DECISION`.
- `ACTION_DECISION` records both `phase="preflight"` and `phase="resume_revalidation"` without
  overwriting the earlier decision.
- `SUSPENSION_CREATED`, `APPROVAL_RESOLVED`, `RESUME_STARTED`, `SUSPENSION_CLEARED` correlate to
  the same suspension and model response; the approval correlates to `call-b`.
- `EXECUTION_OUTCOME` uses M4's real vocabulary and correlates to the ledger `action_id` /
  `attempt_id`; the two outcomes are `EXECUTED_SUCCESS` then `REPLAYED`.
- `TOOL_OBSERVATION` covers all three calls, including the replay, and carries digests/sizes only.
- M1 disclosure, M2 worker create/restore, M3 decisions, M4 outcomes, M5 READY/CLOSED, M6
  suspension/approval/resume/clear, and `RUN_TERMINAL` all appear in one run.
- A test secret and reasoning text are absent from the durable transcript.
- The reader reconstructs the run from `run_id` alone; reconciliation repairs evidence only and
  reports no gap or conflict.

## Test gates

```text
ruff check .                 PASS
ruff format --check .        PASS (215 files)
pyright                      PASS (0 errors)
pytest at formal commit       PASS (289 passed, 1 deselected) twice
pytest on final tree          PASS (290 passed, 1 deselected) twice
```

Focused M7 tests: 16 passed at the formal commit; 17 on the final tree (native observation
dedupe).

## Known limitations

- M7 is best-effort evidence. A failed append can leave a gap until reconciliation.
- Native AnyCode durable `tool_result` is audited defensively but is not the primary observation
  source because Atlas does not enable AgentRunner durability.
- Reconciliation cannot reconstruct a decision or observation that its authority never recorded;
  it reports a gap instead.
- The trajectory stores metadata and digests, not raw prompts, tool payloads, or model reasoning.
- M7 is disabled by default and is not part of `semantic_config_hash`.
