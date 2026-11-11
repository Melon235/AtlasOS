# Stage 1 — Recovery Safety Audit (2026-11-06)

## Scope and provenance

- Repository: `D:\Work\AtlasOS`, branch `master`.
- Audited revision before this stage's fix: `3fbf3dd` (`fix: release safe sandbox ownership on recovery session close`).
- Stage 1 fix commit: `1f2517d` (`fix: refuse uncertain sandbox disposal and expose cleanup results`).
- Runtime: Python 3.12.15 on Windows 10, real self-hosted OpenSandbox at `localhost:8080`,
  image `atlas-uat-python:3.12`. No LLM call is made by the Stage D acceptance harness.
- Evidence:
  - `experiments/results/stage-d-s1-fix-2.json` — real OS-process Stage D acceptance, 9/9 scenarios.
  - `tests/integration/test_stage_d_cleanup.py` — new regression suite.
  - Full repository tests, Ruff, and Pyright results below.

## Findings

| ID | Finding | Status |
|---|---|---|
| S1.1 | A SETTLED `ResumeOutcome` may contain `UNCERTAIN` settlement results. The old wrapper could still enter `dispose_sandbox()` and destroy the exact S1 awaiting reconciliation. The in-process provider uncertainty marker is not durable, so a new recovery process did not necessarily block disposal. | **CONFIRMED ISSUE → VERIFIED FIX** |
| S1.2 | `dispose_sandbox()` returns `False` when `provider.destroy()` does not confirm destruction, but `resume_durable_sandbox_suspension(..., dispose_sandbox_on_settlement=True)` ignored that return value and returned the settled outcome as if cleanup had succeeded. | **CONFIRMED ISSUE → VERIFIED FIX** |
| S1.3 | `RecoveryCleanupResult` existed, but the public wrapper only annotated the original recovery exception when ownership was unresolved. Runtime-close and idempotency-teardown failures could be hidden behind an original resume exception. | **CONFIRMED ISSUE → VERIFIED FIX** |
| — | Two cleanup operations can fail at once (runtime close and idempotency teardown). `RecoveryCleanupResult` records only the first error and one reason string. Both booleans remain observable, but the second exception object is not retained. | **POTENTIAL RISK (documented, not changed)** |
| — | A `provider.destroy()` exception in the real OpenSandbox provider was not fault-injected in this stage. The deterministic fake-provider tests cover `destroy() -> ok=False` and `destroy() -> raises`; the real Stage D run covers the UNCERTAIN refusal path. | **NOT TESTED against real provider destroy failure** |
| — | After a cleared suspension whose durable settlement contains `UNCERTAIN`, disposal is now permanently refused until an operator reconciliation workflow exists. No override was added in this stage because an override without a reconciliation record would recreate the safety hole. | **ARCHITECTURAL BOUNDARY** |

## Fixes

### S1.1 — UNCERTAIN blocks disposal before any provider call

`TrustedRecoverySession.dispose_sandbox()` now refuses when any of these is true:

1. the current manager has a provider-uncertainty marker,
2. the last `ResumeOutcome` carries an `UNCERTAIN` settlement result (including
   `outcome.suspension.settlement`),
3. the **durable** suspension settlement carries `UNCERTAIN`, even when the marker belongs to a
   previous OS process.

The durable read fails closed: an unreadable suspension store is treated as uncertain. Refusal
raises `TrustedRecoveryDisposalError` before reattachment or `manager.close()`. The public wrapper
turns this into a failure with the cleanup result attached; it never returns a settled outcome.

`TrustedRecoverySession.close()` uses the same three-source uncertainty check, so ownership is
retained for manual review in the new-process case as well.

### S1.2 — destroy failure is visible

`dispose_sandbox()` still returns `False` when the provider does not confirm destruction.
`resume_durable_sandbox_suspension()` now checks that return value and raises
`TrustedRecoveryDisposalError` with `disposed=False` and the populated `RecoveryCleanupResult`.
Provider exceptions are wrapped the same way, with the original exception chained as `__cause__`.
Callers can no longer observe "settled" plus normal cleanup after a failed destruction request.

### S1.3 — cleanup visibility without masking the original failure

- `RecoveryCleanupResult.describe()` renders ownership, runtime-close, idempotency-close, reason,
  and first error in one stable line.
- The public wrapper annotates **every** original recovery exception with that line via
  `BaseException.add_note()`, and attaches the structured result as `error.atlas_cleanup` when
  the exception object supports it. The original exception remains the raised exception.
- Ownership-unresolved behavior still raises `TrustedRecoveryOwnershipUnresolved` with
  `.outcome` and `.cleanup`.
- Disposal failures raise `TrustedRecoveryDisposalError` with `.outcome`, `.cleanup`, and
  `.disposed = False`.

### Stage D evaluator update

The D3-7 "crash during side effect" scenario previously expected automatic disposal after an
`UNCERTAIN` settlement. It now requires:

- disposal refusal (`disposal_refused` contains `UNCERTAIN`),
- zero `destroy` calls,
- retained ownership,
- an explicit operator cleanup only after the refusal is recorded.

The final scenario status is `UNCERTAIN_DISPOSAL_REFUSED`; the explicit cleanup is recorded as
`operator_cleanup` evidence. This preserves the test's purpose while applying the new safety rule.

## Regression tests

New tests in `tests/integration/test_stage_d_cleanup.py`:

1. `test_disposal_is_refused_when_settlement_is_uncertain` — fake provider write raises after
   effect; wrapper with `dispose=True` raises, destroy count stays 0, durable binding remains
   READY.
2. `test_disposal_failure_is_not_reported_as_success` — `destroy()` returns `ok=False`; wrapper
   raises `TrustedRecoveryDisposalError`, durable state becomes LOST, no false success.
3. `test_disposal_exception_exposes_cleanup_and_retains_ownership` — `destroy()` raises; wrapper
   raises typed failure, cleanup retains ownership, lock is still held.
4. `test_cleanup_reports_runtime_close_failure` — runtime close exception is visible in
   `runtime_closed=False` and `reason`.
5. `test_cleanup_reports_idempotency_teardown_failure` — teardown exception is visible in
   `idempotency_closed=False` and `reason`.
6. `test_wrapper_exposes_cleanup_when_recovery_and_cleanup_fail` — original continuity failure is
   still the raised exception while cleanup failure details appear in notes.

## Verification

Commands run from `D:\Work\AtlasOS`:

```powershell
.\.venv\Scripts\python.exe -m ruff check src tests
# All checks passed!

.\.venv\Scripts\python.exe -m pyright
# 0 errors, 0 warnings, 0 informations

.\.venv\Scripts\python.exe -m pytest -q
# 436 collected / 436 passed

.\.venv\Scripts\python.exe -m pytest tests/integration/test_stage_d_cleanup.py -q
# 12 passed

.\.venv\Scripts\python.exe -m pytest tests/integration/test_stage_d_cleanup.py tests/integration/test_stage_d_recovery.py tests/integration/test_stage_d_recovery_resume.py tests/integration/test_sandbox_ownership.py tests/integration/test_verified_reattachment.py tests/integration/test_project_sandbox.py -q
# 107 passed

.\.venv\Scripts\python.exe -m pytest tests/integration/test_m6_agent_runner.py tests/integration/test_m6_failure_matrix.py tests/integration/test_m6_runtime_suspension.py tests/integration/test_m6_sqlite_process.py tests/integration/test_m6_sandbox_resume.py tests/integration/test_durable_actions.py tests/integration/test_m1_m4_reliability.py -q
# 56 passed
```

Real Stage D acceptance:

```powershell
.\.venv\Scripts\python.exe -u experiments/eval_os_process_hitl_resume.py `
  --runs-root experiments/runs/stage-d-s1-fix-2 `
  --output experiments/results/stage-d-s1-fix-2.json
```

Result: `status=PASS`, `all_passed=true`, 9/9 scenarios. D3-7:
`status=UNCERTAIN_DISPOSAL_REFUSED`, `disposed=false`, `destroy=0`,
`operator_cleanup.status=DESTROYED`.

An intermediate run (`stage-d-s1-fix.json`) failed only because the child harness did not yet
propagate the refused-disposal cleanup result into its output. The harness was corrected and the
final run above passed; no production behavior was relaxed to obtain the pass.

## Files changed

- `src/atlasos/trusted_recovery.py`
- `tests/integration/test_stage_d_cleanup.py`
- `experiments/stage_d_process.py`
- `experiments/eval_os_process_hitl_resume.py`
- `experiments/results/stage-d-s1-fix-2.json` (new evidence)

## Not changed (scope discipline)

- No second runtime, policy, approval, idempotency, or lock system.
- No changes to M4 settlement semantics or M6 state machine.
- No changes to M7 evidence semantics.
- No attempt to add a distributed fencing or remote exactly-once claim.
