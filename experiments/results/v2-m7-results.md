# V2 M7 — Durable Agent Trajectory: Formal Results

Status: **PASS / FROZEN**

Formal evaluation was run from a clean worktree at:

```text
commit:      cb7ba5994a656b90facbd93193b39dd93bca10c3
git_dirty:   false
timestamp:   2026-11-02T11:57:16.984721Z
python:      3.12.15
anycode-py:  0.9.0
sqlite:      3.53.1
```

The deterministic evaluator is
[`examples/m7_trajectory_eval.py`](../../examples/m7_trajectory_eval.py). It ran both formal
scenarios twice in separate temp directories and compared every metric before recording. The raw
record is in `experiments/runs/v2-m7-trajectory.jsonl` (the `experiments/runs/` directory is
gitignored, matching earlier stages).

## Result

```text
M7 — Durable Agent Trajectory
PASS / FROZEN

MODEL ACTION != AUTHORIZED ACTION
AUTHORIZED ACTION != EXECUTED ACTION
EXECUTED ACTION != OBSERVED RESULT
M7 IS DURABLE EVIDENCE, NOT AN AUDIT-COMPLETE TRANSACTION LOG
```

This freeze-fix replaces the earlier incorrect expectation that two real resume-revalidation
occurrences should collapse into one logical event.

## Repeated occurrence is not duplicate evidence

The earlier formal record reported `37 physical / 32 logical`. The five-event difference was caused
by an event-identity defect: resume revalidation events included `call_id`, `phase`,
`decision_basis_digest`, and decision fields, but no occurrence identity. A failed first resume and
its retry therefore produced the same `event_id` even though M3 evaluated the action twice.

The corrected rule is:

```text
ONE RUNTIME OCCURRENCE + DUPLICATE EVIDENCE WRITE
  -> SAME event_id
  -> logical dedupe

TWO REAL RUNTIME OCCURRENCES, EVEN WITH IDENTICAL RESULT
  -> DIFFERENT event_id
  -> BOTH REMAIN IN TRAJECTORY
```

`ACTION_DECISION` now carries a runtime-provided `occurrence_id`:

- preflight: the fixed `model_response_id` (the batch occurrence boundary);
- resume revalidation: M6's settlement-attempt identity
  `settlement:<suspension_id>:<suspension.revision>`. `revision` advances at every
  `begin_settlement` CAS, so it is stable within one attempt and different for a retry;
- normal execution: the runner invocation occurrence.

The deterministic event id includes `protocol_version`, `run_id`, `event_type`, `call_id`, `phase`,
`occurrence_id`, and `decision_basis_digest`. It does not use `recorded_at`, wall-clock retry time,
or random retry timestamps.

The current formal scenario therefore retains both real resume attempts:

```json
{
  "trajectory_physical_events": 37,
  "trajectory_logical_events": 37,
  "trajectory_logical_duplicates_removed": 0,
  "resume_revalidation_events": 10,
  "resume_revalidation_occurrences": 2,
  "repeated_resume_occurrences_retained": true
}
```

The ten resume-revalidation events are two real occurrences, five decisions each. No real occurrence
was deduplicated.

## Formal scenario A — main M1–M7 chain

```text
call-a  file_read       ALLOW   sandbox project file read
call-b  email.send      HUMAN   executable side effect
call-c  docs.search     ALLOW   read-only dispatch
```

```json
{
  "action_proposed_count": 3,
  "action_decision_count": 15,
  "execution_outcome_count": 2,
  "approval_event_count": 1,
  "suspension_event_count": 4,
  "native_tool_observations_duplicated": 0,
  "trajectory_events_backfilled": 0,
  "trajectory_evidence_gaps": 0,
  "trajectory_evidence_conflicts": 0,
  "trajectory_decode_failures": 0,
  "consistency_checks": 5,
  "consistency_passes": 5,
  "consistency_failures": 0,
  "consistency_unknown": 0,
  "provider_side_effects": 1,
  "duplicate_provider_side_effects": 0,
  "llm_calls_before_resume": 1,
  "llm_calls_after_resume": 1,
  "llm_calls_after_continuation": 2,
  "m4_provenance_correlated": true
}
```

The main scenario verified one worker, one three-call response, whole-response suspension before
M4, durable approval, process-boundary resume without an LLM call, M3 revalidation on both real
resume attempts, M4 execute then replay after an injected crash, ToolResult continuation,
suspension clear, M5 continuity, and run terminal evidence. M4 ledger records correlate to the M3
`call_id` regardless of the trajectory feature flag.

## Formal scenario B — evidence repair after authority commit

Scenario B uses M4 because its ledger is the durable execution authority:

```text
M4 authoritative execution outcome committed
  -> injected M7 EXECUTION_OUTCOME append failure
  -> new process boundary (new RunStore/recorder, same M4 authority)
  -> TrajectoryReconciler
  -> backfilled EXECUTION_OUTCOME
```

```json
{
  "trajectory_physical_events": 1,
  "trajectory_logical_events": 1,
  "trajectory_events_backfilled": 1,
  "execution_outcome_count": 1,
  "reconciler_appended_events": 1,
  "trajectory_evidence_gaps": 0,
  "trajectory_evidence_conflicts": 0,
  "consistency_checks": 1,
  "consistency_passes": 1,
  "consistency_failures": 0,
  "consistency_unknown": 0,
  "m4_ledger_records_before": 1,
  "m4_ledger_records_after": 1,
  "provider_side_effects": 1,
  "duplicate_provider_side_effects": 0,
  "backfilled_outcome_correlates": true,
  "reconciliation_only_evidence": true
}
```

Reconciliation appended exactly the missing evidence, retained the one authoritative M4 ledger
record, and produced no second provider effect.

## Hard-zero authority metrics

Both scenarios:

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

M7 evidence did not execute, authorize, replay, resume, approve, create a sandbox, create an M4
claim, transition an approval, or call the model.

## Focused regression tests

```text
same occurrence + duplicate physical write -> physical 2, logical 1, conflict 0
two real resume attempts (same call/phase/basis, different occurrence)
  -> physical 2, logical 2
M7 enabled vs disabled M4 m3_call_id
  -> both "call-email", same COMPLETED state, same provider effect count
disabled mode -> no trajectory evidence
```

The two real resume attempts have the same `call_id`, phase, and decision basis but different
occurrence identities, and both remain in the trajectory.

## Test gates

```text
ruff check .                 PASS
ruff format --check .        PASS (216 files)
pyright                      PASS (0 errors)
pytest                       PASS (293 passed, 1 deselected) twice
focused M7 tests             PASS (20)
```

## Evidence-gap semantics

Observable or inferable missing evidence is represented as `EVIDENCE_GAP`. Some historical evidence
may be unreconstructable when the authoritative subsystem does not retain queryable historical
state. M7 does not invent missing history.

M7 is durable evidence, not an audit-complete transaction log.

## Known limitations

- M7 is best-effort durable evidence. A failed append may be observable as a gap only when authority
  retains enough queryable state to infer it; otherwise the historical evidence may be
  unreconstructable.
- Native AnyCode durable `tool_result` is audited defensively but is not the primary observation
  source because Atlas does not enable AgentRunner durability.
- Reconciliation cannot reconstruct a decision or observation that its authority never recorded; it
  reports a gap instead.
- The trajectory stores metadata and digests, not raw prompts, tool payloads, or model reasoning.
- M7 is disabled by default and is not part of `semantic_config_hash`.
