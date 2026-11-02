# V2 Integrated System Verification — Formal Results

Status: **PASS**

Formal evaluation was run from a clean worktree at:

```text
commit:      51dc85fd0862740120bc6da440841d67f727daae
git_dirty:   false
timestamp:   2026-11-02T13:28:27.209614Z
python:      3.12.15
anycode-py:  0.9.0
sqlite:      3.53.1
provider:    deterministic scripted/model adapter
sandbox:     deterministic local FakeSandboxProvider
```

The evaluator is
[`experiments/eval_v2_integrated.py`](../../experiments/eval_v2_integrated.py). It ran both
deterministic attempts in separate temp directories and compared every metric before recording.
The raw record is in `experiments/runs/v2-integrated-verification.jsonl` (the `experiments/runs/`
directory is gitignored).

## Result

```text
V2 INTEGRATED SYSTEM VERIFICATION = PASS
V2 RELIABILITY DRILL               = PASS (controlled runtime-boundary drills)
```

## Scenario results

| Scenario | Result | Provider effects | Trajectory events | Proposed | Decisions | Suspensions | Approvals | Resume revalidations | Executed | Replayed | Tool results | Backfilled |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| S1 normal execution | PASS | 1 | 23 | 4 | 9 | 0 | 0 | 0 | 2 | 0 | 4 | 0 |
| S2 approval + restart | PASS | 1 | 20 | 2 | 6 | 1 | 1 | 3 | 1 | 0 | 2 | 0 |
| S3 multi-call barrier | PASS | 1 | 28 | 3 | 10 | 1 | 2 | 5 | 1 | 0 | 3 | 0 |
| S4 approval vs new policy | PASS | 0 | 11 | 0 | 2 | 1 | 1 | 1 | 0 | 0 | 1 | 0 |
| S5 sandbox lost | PASS | 0 | 12 | 0 | 3 | 1 | 1 | 0 | 0 | 0 | 0 | 0 |
| S6 crash + replay | PASS | 1 | 18 | 0 | 6 | 1 | 1 | 4 | 2 | 1 | 1 | 0 |
| S7 evidence repair | PASS | 1 | 1 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 1 |
| S8 cancel + late approval | PASS | 0 | 8 | 0 | 2 | 1 | 1 | 0 | 0 | 0 | 0 | 0 |

All eight scenarios passed. S1 used controlled tool discovery, a coding worker, sandbox file read
and shell execution, one external provider effect, and a correlated ToolResult history. S2/S3
proved the whole-response approval barrier and fresh-runtime resume. S4 proved approval does not
override a later DENY. S5 proved M5 fail-closed behavior after sandbox loss. S6 proved post-effect
crash/replay. S7 injected an M7 append failure after M4 authority and backfilled the evidence. S8
proved late approval cannot revive a cancelled run.

## Positive metrics

```json
{
  "runs_completed": 3,
  "tools_disclosed": 20,
  "workers_constructed": 7,
  "actions_proposed": 9,
  "m3_allow": 24,
  "m3_deny": 2,
  "m3_require_human": 12,
  "suspensions": 6,
  "approvals": 7,
  "resume_revalidations": 13,
  "m4_executed": 7,
  "m4_replayed": 1,
  "m4_uncertain": 0,
  "sandbox_bindings": 5,
  "tool_results": 11,
  "trajectory_events": 121,
  "trajectory_backfills": 1
}
```

## Hard-zero counters

```json
{
  "hidden_tools_executed_by_guess": 0,
  "preapproval_calls_entering_m4": 0,
  "duplicate_provider_effects": 0,
  "host_workspace_fallbacks": 0,
  "sandbox_recreations_after_lost": 0,
  "late_approval_resumes": 0,
  "llm_calls_during_exact_resume": 0,
  "m7_runtime_decisions": 0,
  "m7_m4_claims": 0,
  "m7_provider_executions": 0,
  "m7_run_resumes": 0,
  "m7_sandbox_creations": 0,
  "m7_approval_transitions": 0,
  "m7_reconciliation_llm_calls": 0
}
```

## Reliability drills

```text
R1 post-side-effect crash     PASS (S6: one provider effect, duplicate 0, replay evidence)
R2 pending approval restart   PASS (S2: durable suspension/approval, no LLM during settlement,
                                     M3 revalidation, M5 continuity at the controlled boundary)
R3 M7 evidence loss           PASS (S7: backfill, execution count unchanged, no LLM)
R4 sandbox loss               PASS (S5: fail closed, no recreation, no host fallback)
```

R2 is a controlled fresh-runtime boundary, not an OS process restart. The M5 v1 binding metadata is
runtime-scoped; a real OS restart with a sandbox fails closed rather than recreating. That
limitation is recorded in `docs/verification/v2-system-verification.md`.

## Notes

- The deterministic local sandbox is test infrastructure, not a production isolation provider.
- No scenario bypasses M1–M7 authority or injects trajectory evidence to create runtime state.
- The M7 evidence-repair scenario appends only a backfilled `EXECUTION_OUTCOME`; M4 ledger count and
  provider effect count are unchanged.
