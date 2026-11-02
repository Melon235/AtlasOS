# V2 Real User Acceptance — Results

Status: **REAL ATLAS SANDBOX ISOLATION = PASS; REAL CODING UAT = PASS 2/3
(Run 1 MODEL_FAILURE retained)**

The real coding task and external acceptance were exercised with a live DeepSeek provider, the thin
CLI entrypoint, the frozen M1–M7 Atlas runtime, and a self-hosted OpenSandbox Docker sandbox. No fake
provider was used on the live path and no external cloud sandbox credential was required.

## OpenSandbox real-sandbox stage (current)

### Provenance

```text
implementation HEAD:      4b8afa768267938a2dd13ee53dc4ebefcb020e22
real-sandbox eval HEAD:   4b8afa7 (git_dirty false)
live UAT eval HEAD:       4b8afa7 (git_dirty false)
provider:                 opensandbox (Atlas OpenSandboxProvider adapter)
OpenSandbox server:       1.2.0
OpenSandbox Python SDK:   1.2.0
Docker:                   29.6.0 (WSL2 Ubuntu engine)
image:                    atlas-uat-python:3.12 (Python 3.12.16 + Git 2.47.3 + pytest 9.1.1)
workspace:                /workspace/project
network:                  none (OpenSandbox egress sidecar, dns+nft)
credential source:        no external cloud credential; optional local OPEN_SANDBOX_API_KEY
fixture digest:           30ec285bbdfcc53338c8b3ee67ed2a10f87078ed3f901abcfb8158587e4403f5
```

### Configuration and routing

- Provider, endpoint, image, workspace, network, CPU/memory, and TTL come only from
  [`experiments/configs/v2-uat.yaml`](../configs/v2-uat.yaml); model output, task text, tool
  arguments, and worker-profile text cannot change them.
- `experiments/uat_cli.py` constructs the thin `OpenSandboxProvider` adapter, checks capabilities
  before create, binds one task to one M5 sandbox, and routes all six project tools through that same
  handle and `/workspace/project`.
- OpenSandbox is execution infrastructure only. Atlas remains the runtime, M3 authorization, M4
  execution authority, M5 binding authority, M6 approval authority, and M7 evidence authority.
- The adapter maps `network="none"` to OpenSandbox `NetworkPolicy(defaultAction="deny")`. The local
  server used `[egress] mode="dns+nft"`; an `opensandbox/egress:v1.1.7` sidecar was observed attached
  to the sandbox and the empirical egress probe passed.

### Evaluation commands

```powershell
$env:PYTHONPATH = "src;."
.\.venv\Scripts\python.exe experiments\eval_real_sandbox.py
.\.venv\Scripts\python.exe experiments\uat_v2.py --runs-root experiments\runs\uat
```

The real evaluator at `4b8afa7` passed:

```text
provider:                 opensandbox
provider_is_real:         true
provider_is_self_hosted:  true
server health:            healthy
sandbox:                  created / ready / destroyed
sandbox id:               0436c794-2047-4032-83b6-a7d5c618d134
file upload/read:         PASS
python --version:         Python 3.12.16 (exit 0)
small script:             stdout 45 (exit 0)
network policy:           requested none / accepted / empirically PASS
host-only sentinel:       not visible in sandbox
sandbox-only sentinel:    not visible on Host; read-back PASS
host fixture digest:      unchanged
explicit export:          PASS (1 file)
status:                   PASS
```

### Three-run live coding batch at `4b8afa7`

| Run | Run ID | Result | Classification | External acceptance |
| --- | --- | --- | --- | --- |
| 1 | `uat-fd65019b7d8c4a21a9050d75fa392173` | FAIL | `MODEL_FAILURE` | No files changed; oracle failed; test suite passed vacuously |
| 2 | `uat-09ad802b361b4e88a15147cec0c8c597` | PASS | none | Changed `csv_parser.py`, `tests/test_csv_parser.py`; oracle + pytest PASS |
| 3 | `uat-10449880cf68480c9479c1f3e29ac2cc` | PASS | none | Same expected files; oracle + pytest PASS |

Run 1's agent explored the project but made no edits before the run ended; sandbox lifecycle,
isolation, cleanup, and M7 evidence were all valid. It is recorded as a genuine model failure, not
hidden or repaired. Runs 2 and 3 passed independent external acceptance.

### Per-run sandbox evidence

```text
Run 1: sandbox 376afa4a-8361-47d8-bc20-60aef532d58a
       created/ready/destroyed, same binding, network-none PASS,
       host sentinels safe, Host digest unchanged, M7 READY+CLOSED,
       0 gaps/conflicts, 11 commands, 3 uploads, 2 reads, 0 writes
Run 2: sandbox 88863482-6dbf-4f31-bc53-e89bb197a4ed
       created/ready/destroyed, same binding, network-none PASS,
       host sentinels safe, Host digest unchanged, M7 READY+CLOSED,
       0 gaps/conflicts, 13 commands, 3 uploads, 2 reads, 2 writes
Run 3: sandbox 380017e9-0b9a-4a52-ab9f-17d2023e460a
       created/ready/destroyed, same binding, network-none PASS,
       host sentinels safe, Host digest unchanged, M7 READY+CLOSED,
       0 gaps/conflicts, 14 commands, 3 uploads, 2 reads, 1 write
```

External acceptance for Runs 2 and 3 ran on the explicit exported workspace, not on the Host
fixture. It included `python -m pytest -q`, the hidden oracle, changed/unexpected/removed-file
inspection, and Host digest comparison. Agent self-report was not used as PASS authority.

The sandbox cleanup check after all runs found no `sandbox-*` or `sandbox-egress-*` containers
remaining.

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
