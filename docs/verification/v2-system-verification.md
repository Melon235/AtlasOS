# V2 Integrated System Verification

Status: **IMPLEMENTED; FORMAL INTEGRATED RUN PENDING; REAL UAT BLOCKED BY ENVIRONMENT**

This stage is not M8. It adds no runtime authority. It drives the frozen M1–M7 stack through
system-level scenarios, a thin user-facing CLI adapter, and reliability drills.

## 1. Frozen authorities

```text
M1 FINDS TOOLS.
M2 BUILDS WORKERS.
M3 AUTHORIZES ACTIONS.
M4 RECORDS AND RECOVERS EXECUTION.
M5 BINDS PROJECT WORKLOADS TO AN ISOLATED EXECUTION ENVIRONMENT.
M6 DURABLY INTERRUPTS/RESUMES FOR HUMAN APPROVAL.
M7 RECORDS DURABLE BEHAVIORAL EVIDENCE.
ANYCODE OWNS EXECUTION DEDUPLICATION.
ATLAS RUNTIME IS THE ONLY RUNTIME.
```

The verification code only drives the existing runtime, observes existing state, injects controlled
faults, and checks externally visible results. It adds no policy engine, executor, idempotency
layer, approval store, suspension protocol, sandbox manager, trajectory store, or agent loop.

## 2. Entry points

The repository had no complete user-facing CLI at the start of this stage. The thin adapter is
[`experiments/uat_cli.py`](../../experiments/uat_cli.py):

```text
natural-language task
  -> parse CLI input
  -> load configuration/provider
  -> construct the existing Atlas ContextEngine + AtlasToolRuntime
  -> run one existing engine task
  -> print user-visible result and machine-readable run_id
```

It does not own scheduling, policy, approval, execution, recovery, or evidence logic.

- Integrated deterministic driver: [`experiments/eval_v2_integrated.py`](../../experiments/eval_v2_integrated.py)
- Real-user acceptance harness: [`experiments/uat_v2.py`](../../experiments/uat_v2.py)
- UAT fixture: [`experiments/fixtures/uat_repo/`](../../experiments/fixtures/uat_repo/)
- Independent acceptance oracle: [`experiments/fixtures/uat_acceptance.py`](../../experiments/fixtures/uat_acceptance.py)

## 3. Scenario matrix

| Scenario | What it proves |
| --- | --- |
| S1 normal execution | M1 disclosure, M2 worker, M3 ALLOW before M4, M5 sandbox execution, M7 proposed/decision/outcome/observation, host unchanged |
| S2 approval + restart | whole-response suspension before M4, durable approval, fresh runtime resume, no LLM during settlement, M3 revalidation, M5 continuity at the controlled runtime boundary, durable ToolResult continuation |
| S3 multi-call barrier | three calls, one human, zero M4 entries before approval, full ordered batch after approval |
| S4 policy change | human approval does not override a later M3 DENY; old decisions remain in trajectory |
| S5 sandbox LOST | approval does not bypass M5; fail closed, no recreation, no host fallback |
| S6 side-effect crash/replay | one provider effect, duplicate 0, M4 replay semantics, valid ToolResult continuation |
| S7 evidence repair | authority committed, M7 evidence missing, new process boundary, reconciler backfills without execution |
| S8 cancellation + late approval | terminal run cannot be revived; no effect, no resume |
| R1 | S6 post-side-effect crash drill |
| R2 | S2 pending-approval restart drill |
| R3 | S7 evidence-loss drill |
| R4 | S5 sandbox-loss drill |

S2/S6 “process boundary” in the integrated evaluator is a fresh runtime/store/recorder composition
over the same durable authority. A true OS-process restart with a live sandbox is not claimed; see
the M5 limitation below.

## 4. Invariants verified

```text
WorkerProfile != Permission Grant
operating_scope != Capability List
Tool Disclosure != Authorization
Prompt != Trusted Runtime State
MODEL ACTION != AUTHORIZED ACTION
AUTHORIZED ACTION != EXECUTED ACTION
EXECUTED ACTION != OBSERVED RESULT
TRAJECTORY EVIDENCE != RUNTIME AUTHORITY

IF ANY TOOL CALL IN ONE MODEL RESPONSE REQUIRES HUMAN APPROVAL,
NO TOOL CALL FROM THAT RESPONSE MAY ENTER M4 BEFORE APPROVAL.

TOOL RESULT MUST BE DURABLE BEFORE SUSPENSION IS CLEARED.
```

## 5. Integrated hard-zero counters

```text
hidden_tools_executed_by_guess          0
preapproval_calls_entering_m4           0
duplicate_provider_effects              0
host_workspace_fallbacks                0
sandbox_recreations_after_lost          0
late_approval_resumes                   0
llm_calls_during_exact_resume           0
m7_runtime_decisions                    0
m7_m4_claims                            0
m7_provider_executions                  0
m7_run_resumes                          0
m7_sandbox_creations                    0
m7_approval_transitions                 0
m7_reconciliation_llm_calls             0
```

Final numbers are recorded in
[`experiments/results/v2-integrated-verification.md`](../../experiments/results/v2-integrated-verification.md).

## 6. Real user acceptance

The UAT fixture is a small Python CSV parser with a seeded quoted-comma/escaped-quote bug. The
external acceptance oracle is not mounted into the sandbox; it checks behavior the agent was not
given as visible tests, and the harness also runs the fixture test suite and compares the host
fixture digest.

The current environment has:

- a live DeepSeek credential (`.env`, never printed or stored);
- **no production AnyCode sandbox provider** (no E2B/Daytona/Modal/Runloop/Vercel SDK/credential
  configured).

Therefore the coding UAT runs through the thin CLI using the repository's deterministic local
sandbox provider for infrastructure validation, but it is **not** counted as a real-isolation PASS.

```text
REAL USER ACCEPTANCE = BLOCKED_NO_REAL_SANDBOX_PROVIDER
```

A production UAT must install/configure one AnyCode sandbox provider, then run:

```bash
PYTHONPATH=src:. .venv/bin/python experiments/uat_v2.py --runs-root experiments/runs/uat
```

with no `--scripted` flag, and a real provider selected in `experiments/uat_cli.py`.

The live LLM result, failure classification, changed files, host isolation, test exit code, patch
digest, and M7 trajectory cross-check are recorded in
[`experiments/results/v2-user-acceptance.md`](../../experiments/results/v2-user-acceptance.md).

## 7. Known limitations

- **M5 binding metadata is runtime-scoped in v1.** A fresh OS process cannot prove it still owns a
  sandbox handle; it fails closed rather than recreating. Real HITL restart with a sandbox is
  therefore blocked until M5 owns durable handle continuity. The external-only approval path can
  restart safely, but the coding UAT cannot claim that.
- No real cloud sandbox provider is installed/configured.
- The local fake provider is deterministic test infrastructure; it is not a production isolation
  boundary.
- Live LLM behavior is nondeterministic. UAT acceptance is external outcome-based, not token-level
  reproduction.
- The integrated S2/S6 “restart” is a controlled runtime-boundary restart, not an OS process kill;
  OS-process durability remains covered by the existing M4.1/M6 process tests and the M7 evidence
  repair scenario.
