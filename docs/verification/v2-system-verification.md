# V2 Integrated System Verification

Status: **REAL ATLAS SANDBOX ISOLATION PASS; REAL CODING UAT PASS 2/3;
SELF-HOSTED OPENSANDBOX PROVIDER PASS**

```text
V2 INTEGRATED SYSTEM VERIFICATION = PASS
V2 RELIABILITY DRILL              = PASS (controlled runtime-boundary drills)
SELF-HOSTED OPEN SOURCE PROVIDER  = PASS (OpenSandbox 1.2.0 + local Docker 29.6.0)
REAL ATLAS SANDBOX ISOLATION      = PASS (real evaluator + 3 live UAT sandboxes)
NETWORK-NONE ENFORCEMENT          = PASS (empirical; OpenSandbox egress sidecar)
REAL CODING UAT                   = PASS 2/3 (Run 1 MODEL_FAILURE; Runs 2–3 PASS)
OS-PROCESS REATTACHMENT           = NOT IN SCOPE
V2 OVERALL ACCEPTANCE             = NOT CLAIMED (coding UAT is 2/3)
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
given as visible tests, and the harness also runs the fixture test suite, checks
changed/unexpected/removed files, and compares the Host fixture digest.

Three independent live runs were executed on the clean OpenSandbox implementation HEAD
`4b8afa7` (`git_dirty=false`). Runs are recorded in order; no sample was discarded.

| Run | Run ID | Result | Classification | Notes |
| --- | --- | --- | --- | --- |
| 1 | `uat-fd65019b7d8c4a21a9050d75fa392173` | FAIL | `MODEL_FAILURE` | Agent explored but made no edits; sandbox lifecycle, isolation, cleanup, and M7 were valid |
| 2 | `uat-09ad802b361b4e88a15147cec0c8c597` | PASS | none | Changed `csv_parser.py` and `tests/test_csv_parser.py`; external oracle + pytest PASS |
| 3 | `uat-10449880cf68480c9479c1f3e29ac2cc` | PASS | none | Same expected files; external oracle + pytest PASS |

Per-run sandbox evidence:

```text
Run 1: OpenSandbox created/destroyed, network-none PASS, host sentinels safe,
       Host fixture unchanged, M7 READY+CLOSED, 0 gaps/conflicts
Run 2: same lifecycle/isolation evidence; 9 tool observations, 9 execution outcomes,
       external acceptance PASS, test exit 0, M7 PASS
Run 3: same lifecycle/isolation evidence; 9 tool observations, 8 execution outcomes,
       external acceptance PASS, test exit 0, M7 PASS
```

The real-sandbox evaluator also passed at `4b8afa7`:

```text
provider: OpenSandbox (self-hosted)
server: healthy
sandbox: created, ready, id 0436c794-2047-4032-83b6-a7d5c618d134
file transfer: PASS
commands: python 3.12.16, small script stdout 45
network_policy: requested none, accepted, empirically PASS
host/sandbox sentinels: safe; Host fixture digest unchanged
explicit workspace export: PASS
sandbox destroyed: true
status: PASS
```

Run 1 is a genuine model failure, not a runtime or sandbox failure. The harness did not ignore it or
change the fixture; the two later runs demonstrate the real coding path can pass external acceptance.
The detailed record is in
[`experiments/results/v2-user-acceptance.md`](../../experiments/results/v2-user-acceptance.md).

## 7. Self-hosted OpenSandbox integration

**Atlas Sandbox** is the product capability. M5 owns binding, trusted configuration, lifecycle, and
fail-closed behavior. The AnyCode-compatible `SandboxProvider` owns the real sandbox resource,
process execution, remote filesystem, network enforcement, and destruction. Atlas does not talk to
Docker directly and does not implement a second sandbox runtime.

```text
Atlas Runtime -> M3 -> M4 -> M5 ProjectSandboxBinding
  -> AnyCode SandboxProvider protocol
  -> OpenSandboxProvider adapter
  -> OpenSandbox Python SDK
  -> local OpenSandbox server
  -> Docker Engine
  -> isolated Atlas Sandbox
  -> /workspace/project
```

Trusted configuration lives in
[`experiments/configs/v2-uat.yaml`](../../experiments/configs/v2-uat.yaml):

```yaml
sandbox:
  provider: opensandbox
  endpoint: localhost:8080
  image: atlas-uat-python:3.12
  workspace_root: /workspace/project
  network: none
  timeout_minutes: 20
  cpu: "1"
  memory: "1Gi"
```

Provider, endpoint, image, workspace, network, CPU/memory, and TTL cannot be selected by the model,
task text, tool arguments, or worker profile. OpenSandbox needs no external cloud credential. The
local service can optionally use `OPEN_SANDBOX_API_KEY`; the verified run used a loopback-only
server with no cloud credential.

Before create, Atlas requires isolated execution, persistent filesystem, file transfer, and a
network policy compatible with `none`. The adapter maps `network="none"` to OpenSandbox
`NetworkPolicy(defaultAction="deny")`. The server used the OpenSandbox egress sidecar in `dns+nft`
mode; a real `opensandbox/egress:v1.1.7` sidecar was observed attached to the sandbox. Atlas records
policy requested, accepted, and empirically verified separately. An unexpected egress success fails
closed.

Provisioning uploads a clean Git bundle with `provider.write_file`, creates `/workspace/project`,
clones inside the sandbox, and verifies the source commit. Task-end export downloads a tar archive
created inside the sandbox with `provider.read_file`; the Host extracts it with traversal checks and
runs external acceptance on that copy. The Host fixture is never mounted or mutated. Normal
completion and failure cleanup call `provider.destroy`; all evaluated runs left no sandbox or egress
containers running. A LOST sandbox is terminal and is never recreated or replaced by Host execution.

The verified local topology — Windows Atlas client, WSL2 OpenSandbox server, Docker Engine, and the
UAT image build — is documented in
[`docs/stages/opensandbox-local.md`](opensandbox-local.md).

## 8. Known limitations

- **OS-process sandbox reattachment is not implemented in this stage.** OpenSandbox itself can
  reconnect by id, but Atlas does not treat provider reconnect as proof of trusted handle
  continuity. A fresh Atlas process fails closed; it does not call `Sandbox.connect()` in the M5
  restart path and does not create a replacement.
- **M5 binding metadata is runtime-scoped in v1.** Real HITL restart with the same live sandbox is
  therefore still blocked.
- Live coding UAT passed 2 of 3 runs. Run 1 was `MODEL_FAILURE` (no edits); this is retained as
  model nondeterminism, not hidden or repaired.
- Network-none enforcement depends on the local OpenSandbox egress sidecar and its `dns+nft`
  configuration. The verified run passed an empirical egress probe; this is not a claim of
  cryptographically proven or kernel-verified network isolation.
- The local OpenSandbox server is development infrastructure, bound to loopback. It is not hardened
  for multi-tenant or public deployment; use server API keys and a reviewed topology before exposing
  it.
- The sandbox image is a local UAT image built from `python:3.12` plus pytest. It includes Git for M5
  bundle provisioning. It is not a general-purpose hardened image.
- No snapshot restore, secure-runtime (gVisor/Kata/Firecracker), or patch apply in this stage.
- The integrated S2/S6 “restart” is a controlled runtime-boundary restart, not an OS process kill;
  OS-process durability remains covered by the existing M4.1/M6 process tests and the M7 evidence
  repair scenario.
