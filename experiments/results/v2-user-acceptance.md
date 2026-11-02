# V2 Real User Acceptance 鈥?Results

Status: **REAL ATLAS SANDBOX PROVIDER = IMPLEMENTED; LIVE SANDBOX UAT =
BLOCKED_NO_CREDENTIALS; PREVIOUS CLEAN RUN = MODEL_FAILURE (retained)**

The real coding task and external acceptance were exercised with a live DeepSeek provider and the
thin CLI entrypoint. The real Atlas Sandbox path now uses the AnyCode-compatible Daytona provider;
there is no fake-provider fallback on the live path. At the evaluated implementation HEAD the Host
had no Daytona credential, so the real evaluator and live UAT failed closed with
`BLOCKED_NO_CREDENTIALS`. Real isolation is not claimed from a fake or scripted run.

## Real Atlas Sandbox stage (current)

### Provenance

```text
implementation HEAD:      7c34f22ed8339c54b08c54862d3e8d9f43591e7a
real-sandbox eval HEAD:   7c34f22 (git_dirty false)
live UAT eval HEAD:       7c34f22 (git_dirty false)
provider:                 daytona (AnyCode create_sandbox_provider)
provider SDK:             daytona 0.227.0, anycode-py 0.9.0
image:                    python:3.12@sha256:17b972... (full image, includes Git)
workspace:                /workspace/project
network:                  none
credential source:        Host environment / Daytona SDK only
fixture digest:           30ec285bbdfcc53338c8b3ee67ed2a10f87078ed3f901abcfb8158587e4403f5
```

### Configuration and routing

- Provider, image, workspace, and network come only from
  [`experiments/configs/v2-uat.yaml`](../configs/v2-uat.yaml); model output, task text, tool
  arguments, and worker-profile text cannot change them.
- `experiments/uat_cli.py` constructs the real provider through
  `create_sandbox_provider("daytona")`, checks capabilities before create, binds one task to one M5
  sandbox, routes all six project tools through the same handle, and refuses the fake provider on the
  live path.
- Credentials are read only from the Daytona SDK's Host configuration. They are never written to
  the spec, labels, commands, phase/run stores, M4 ledger, M5 binding, trajectory, CLI output, or
  this report.
- M5 accepts AnyCode's `networking="allowlist"` report only as a network-none-capable provider; the
  Daytona adapter maps `network="none"` to `network_block_all=True`, and the evaluator/UAT performs
  an empirical egress probe. Any capability mismatch or observed egress succeeds fails closed.

### Evaluation commands

```powershell
$env:PYTHONPATH = "src;."
.\.venv\Scripts\python.exe experiments\eval_real_sandbox.py
.\.venv\Scripts\python.exe experiments\uat_v2.py --runs-root experiments\runs\uat
```

### Results at `7c34f22`

```text
real sandbox evaluator:    BLOCKED_NO_CREDENTIALS
  sandbox_created:         false
  fake_provider_used:      0
  host fixture unchanged:  true

live coding UAT:           BLOCKED_NO_CREDENTIALS
  user_entrypoint_exit:    1
  provider_name:           daytona
  provider_is_real:        false
  sandbox_created:         false
  host fixture unchanged:  true
  fake_provider_used_in_real_uat: 0

scripted smoke (fake):     SCRIPTED_SMOKE (not real acceptance)
```

The credential failure is classified as `CREDENTIAL_BLOCKED`; no fallback provider is selected and
no PASS is claimed. A credential-backed rerun must produce the full real-sandbox evidence set
(remote create, file transfer, command execution, same-binding tools, empirical Host/sandbox
separation, network-none probe, clean destroy, exported workspace, external acceptance) before
`REAL ATLAS SANDBOX ISOLATION = PASS` can be reported.

## Previous acceptance results (pre-real-provider)

### Provenance

```text
landing commit:   674cbdc8841c08bb7bf83b528eb422dfa57f6608
clean-run HEAD:   674cbdc (git_dirty false)
exploratory HEAD: 3006f23 (dirty: results artifact untracked)
provider:         deepseek
model:            deepseek-chat
fixture digest:   30ec285bbdfcc53338c8b3ee67ed2a10f87078ed3f901abcfb8158587e4403f5
entrypoint:       experiments/uat_cli.py (thin CLI adapter)
UAT harness:      experiments/uat_v2.py
```

API credentials are loaded from the existing `.env`/environment; no key, prompt secret, or
chain-of-thought is written to this report or the trajectory.

### Task and fixture

Natural-language task:

```text
This project has a CSV parsing bug. Find the bug, fix it, add the necessary regression tests,
and run the tests to confirm. Do not change unrelated code.
```

Fixture: [`experiments/fixtures/uat_repo/`](../../experiments/fixtures/uat_repo/) 鈥?a small CSV
parser with a seeded quoted-comma/escaped-quote bug. The independent oracle
[`experiments/fixtures/uat_acceptance.py`](../../experiments/fixtures/uat_acceptance.py) is outside
the sandbox repo and checks quoted commas, escaped quotes, plain rows, and an unseen acceptance row.
The UAT harness also runs `python -m pytest -q` in the exported workspace.

### Three-run exploratory batch

| Run | Result | Classification | Notes |
| --- | --- | --- | --- |
| 1 | FAIL | `RUNTIME_FAILURE` (harness export defect) | The agent created a Windows `nul` entry; the CLI's `shutil.copytree` export failed after the run. Fixed by a reserved-name-safe export. |
| 2 | FAIL | `MODEL_FAILURE` | The live model explored but made no code edits; external acceptance failed. |
| 3 | PASS (coding/external/host/trajectory) | `BLOCKED_NO_REAL_SANDBOX_PROVIDER` | 13 proposed calls, 9 execution outcomes, 13 tool observations, changed `csv_parser.py` and `tests/test_csv_parser.py`, test exit 0, acceptance PASS, host unchanged, trajectory complete. |

The first run did not reach a coding verdict because the harness export failed; the defect was in the
UAT harness, not M1鈥揗7. The second run is an honest model failure, not an Atlas runtime failure. The
third run is an external-acceptance PASS in the local deterministic sandbox.

### Clean formal run

After fixing the export defect and committing the harness, a clean live run was executed at
`674cbdc` (`git_dirty=false`):

```text
run_id:                     uat-f95cb139228f43d088170d635ac0e75a
changed files:              csv_parser.py, tests/test_csv_parser.py
external oracle:            PASS (quoted comma, escaped quote, unknown row)
test command:               python -m pytest -q
test exit code:             1
failure class:              MODEL_FAILURE
acceptance result:          FAIL
host fixture unchanged:     true
trajectory:                 PASS (1 worker, 10 proposed, 20 decisions, 6 execution outcomes,
                                  10 observations, 1 sandbox READY, 1 run terminal, 0 gaps/conflicts)
```

The external parser oracle passed, but the agent's own test file failed to import `csv_parser`
under pytest from the repository root. That is a model/test-authoring failure, not an M1鈥揗7 runtime
failure. The UAT harness correctly rejected the self-report and marked the run FAIL.

Run 3 details:

```text
run_id:                     uat-4b9374f6bb4948a3afcdc5be2c268153
worker events:              1
action proposed:            13
action decisions:           26
execution outcomes:         9
sandbox lifecycle events:   1
tool observations:          13
run terminal events:        1
evidence gaps:              0
evidence conflicts:         0
test exit code:             0
host fixture unchanged:     true
changed files:              csv_parser.py, tests/test_csv_parser.py
unexpected files:           none
removed files:              none
patch digest:               4343a90ccc02b48326dff1b5d923c326751a7376272188ead2f67dcbcd83b396
```

The model's self-report was not used as acceptance. The harness imported the exported parser and ran
the independent oracle, ran the repository tests, checked changed/unexpected/removed files, and
recomputed the host fixture digest.

### M7 trajectory cross-check

The harness read the trajectory from `run_id` alone and found worker lifecycle, sandbox lifecycle,
`ACTION_PROPOSED`, `ACTION_DECISION`, `EXECUTION_OUTCOME`, `TOOL_OBSERVATION`, and `RUN_TERMINAL`
events. It cross-checked the result against the exported files and the external test exit code.
Trajectory evidence was not the sole PASS authority.

### Real HITL / restart

**BLOCKED.** M5 v1 binding metadata is runtime-scoped: a new OS process cannot prove it still owns
the same sandbox handle and fails closed rather than recreating. Real HITL restart with a live
sandbox therefore cannot be claimed. The controlled integrated S2 scenario proves the same-response
suspension, durable approval, fresh-runtime resume, M3 revalidation, and M5 continuity at a
controlled runtime boundary; it does not substitute for an OS-process live-sandbox restart.

There is also no production GitHub integration configured; a safe external-write UAT would use the
test external-side-effect tool through the real approval surface, but that path is blocked by the
sandbox continuity limitation for a coding run.

### Previous final status (pre-real-provider)

```text
USER ENTRY POINT            PASS (thin CLI adapter)
REAL LLM                    PASS (DeepSeek live coding runs)
TOOL DISCOVERY              PARTIAL (M1 core runtime active; no tool.search call in these runs)
WORKER CONSTRUCTION         PASS (M2 coding worker)
POLICY ENFORCEMENT          PASS (M3 decisions present)
SANDBOX ISOLATION           BLOCKED_NO_REAL_SANDBOX_PROVIDER
CODING TASK                 FAIL in clean formal run (MODEL_FAILURE);
                            one exploratory run passed external acceptance
EXTERNAL VERIFICATION       PASS in exploratory run 3; FAIL in clean run (pytest import error)
HOST ISOLATION              PASS
TRAJECTORY                  PASS (all runs; clean run complete and conflict-free)
REAL HITL / RESTART         BLOCKED (M5 v1 runtime-scoped binding)
REAL USER ACCEPTANCE        FAIL / NOT CLAIMED
```

A production operator should install/configure an AnyCode sandbox provider, run the CLI without
`--scripted` against a real provider, and run the HITL/restart task with a persistent M5 handle
contract. Until then, V2 overall acceptance cannot be declared PASS.
