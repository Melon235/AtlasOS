# V2 Integrated System Verification

Status: **INTEGRATED SYSTEM VERIFICATION PASS; REAL ATLAS SANDBOX PROVIDER IMPLEMENTED;
LIVE SANDBOX UAT BLOCKED_NO_CREDENTIALS**

```text
V2 INTEGRATED SYSTEM VERIFICATION = PASS
V2 RELIABILITY DRILL              = PASS (controlled runtime-boundary drills)
REAL ATLAS SANDBOX PROVIDER       = IMPLEMENTED (Daytona through AnyCode SandboxProvider)
REAL SANDBOX ISOLATION            = NOT CLAIMED (no credential-backed real run yet)
V2 REAL USER ACCEPTANCE           = BLOCKED_NO_CREDENTIALS at evaluated HEAD 7c34f22;
                                    previous clean-run MODEL_FAILURE retained
V2 OVERALL ACCEPTANCE             = NOT CLAIMED
```

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
given as visible tests, and the harness also runs the fixture test suite and compares the Host
fixture digest.

The current environment has a live DeepSeek credential. At the evaluated HEAD `7c34f22`, the Host
had **no Daytona credential**, so the real-provider path failed closed before creating a sandbox:

```text
REAL ATLAS SANDBOX PROVIDER = IMPLEMENTED
REAL SANDBOX ISOLATION      = NOT CLAIMED
REAL CODING UAT             = BLOCKED_NO_CREDENTIALS
```

The deterministic scripted path still exercises the same CLI, M1–M7 stack, real M5 binding,
single-flight provisioning, sandbox file/command tools, M7 evidence, export, and cleanup through
the in-repository fake provider. It is reported as `SCRIPTED_SMOKE`, never as a real-isolation PASS.

A production UAT must install the optional Daytona integration and configure Host credentials, then
run:

```powershell
uv sync --extra sandbox   # or: pip install -e ".[sandbox]"
$env:DAYTONA_API_KEY = "<Host secret>"
$env:PYTHONPATH = "src;."
.\.venv\Scripts\python.exe experiments\eval_real_sandbox.py
.\.venv\Scripts\python.exe experiments\uat_v2.py --runs-root experiments\runs\uat
```

with no `--scripted` flag. `experiments/uat_cli.py` refuses the fake provider on the live path.

The previous clean live run at `674cbdc` fixed the parser (external oracle PASS) but authored a test
with an import error, so `python -m pytest -q` failed; the UAT harness correctly classified it
`MODEL_FAILURE`/FAIL. That result is retained, not repaired by changing the fixture or ignoring
pytest. The live LLM result, failure classification, changed files, Host isolation, test exit code,
patch digest, and M7 trajectory cross-check are recorded in
[`experiments/results/v2-user-acceptance.md`](../../experiments/results/v2-user-acceptance.md).

## 7. Real Atlas Sandbox provider integration

**Atlas Sandbox** is the product capability. M5 owns binding, trusted configuration, lifecycle, and
fail-closed behavior. The AnyCode-compatible `SandboxProvider` owns the real remote resource,
process execution, remote filesystem, network enforcement, and destruction. Atlas does not import
the Daytona SDK directly and does not implement a second sandbox runtime.

```text
Atlas Runtime -> M3 -> M4 -> M5 ProjectSandboxBinding
  -> AnyCode SandboxProvider protocol
  -> create_sandbox_provider("daytona")
  -> DaytonaSandboxProvider
  -> remote isolated sandbox
  -> /workspace/project
```

Trusted configuration lives in
[`experiments/configs/v2-uat.yaml`](../../experiments/configs/v2-uat.yaml):

```yaml
sandbox:
  provider: daytona
  image: python:3.12@sha256:...
  workspace_root: /workspace/project
  network: none
```

Provider and workspace cannot be selected by the model, task text, tool arguments, or worker
profile. Credentials come only from the Host environment/SDK configuration
(`DAYTONA_API_KEY`, or `DAYTONA_JWT_TOKEN` + `DAYTONA_ORGANIZATION_ID`); Atlas checks presence and
never stores the value in specs, labels, commands, trajectory, ledgers, sessions, or reports.

Before create, Atlas requires remote/isolated execution, persistent filesystem, file transfer, and a
network policy compatible with `none`. AnyCode reports `networking="allowlist"` for Daytona because
the adapter can enforce an allowlist or a complete block; the adapter maps `network="none"` to
`network_block_all=True`. The UAT/evaluator also runs an empirical egress probe. Missing capability
or an unexpected egress success fails closed; there is no silent downgrade.

Provisioning uploads a clean Git bundle with `provider.write_file`, creates `/workspace/project`,
clones inside the sandbox, and verifies the source commit. Task-end export downloads a tar archive
created inside the sandbox with `provider.read_file`; the Host extracts it with traversal checks and
runs external acceptance on that copy. The Host fixture is never mounted or mutated. Normal
completion and failure cleanup call `provider.destroy`; a LOST sandbox is terminal and is never
recreated or replaced by Host execution.

At `7c34f22`, the deterministic tests, integrated verifier, and scripted smoke passed. The real
evaluator and live UAT recorded `BLOCKED_NO_CREDENTIALS` with `fake_provider_used_in_real_uat=0`,
`host_workspace_fallbacks=0`, and an unchanged Host fixture digest. Real isolation is **not
claimed** until a credential-backed run produces the full evidence set.

## 8. Known limitations

- **M5 binding metadata is runtime-scoped in v1.** A fresh OS process cannot prove it still owns a
  sandbox handle; it fails closed rather than recreating. Real HITL restart with a sandbox is
  therefore blocked until M5 owns durable handle continuity. The external-only approval path can
  restart safely, but the coding UAT cannot claim that.
- **OS-process sandbox reattachment is not implemented in this stage.** A restarted process cannot
  attach to the old remote sandbox, and Atlas does not create a replacement.
- Live credential-backed UAT requires Host Daytona credentials; without them the formal status is
  `BLOCKED_NO_CREDENTIALS`.
- The local fake provider is deterministic test infrastructure; it is not a production isolation
  boundary and is never used on the live UAT path.
- The UAT image is a generic pinned Python image; external acceptance runs on the Host export, so
  model-side test tooling may differ from the Host venv.
- Live LLM behavior is nondeterministic. UAT acceptance is external outcome-based, not token-level
  reproduction.
- The integrated S2/S6 “restart” is a controlled runtime-boundary restart, not an OS process kill;
  OS-process durability remains covered by the existing M4.1/M6 process tests and the M7 evidence
  repair scenario.
