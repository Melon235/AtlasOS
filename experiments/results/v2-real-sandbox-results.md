# V2 Real Atlas Sandbox — Results

Status: **SELF-HOSTED OPENSANDBOX PROVIDER = PASS;
REAL ATLAS SANDBOX ISOLATION = PASS;
NETWORK-NONE ENFORCEMENT = PASS (empirical);
REAL CODING UAT = PASS 2/3**

Atlas M5 project workloads now execute in a real self-hosted OpenSandbox container backed by the
local Docker Engine. There is no fake-provider fallback on the live path and no external cloud
sandbox credential is required.

## Provenance

```text
implementation HEAD:       4b8afa768267938a2dd13ee53dc4ebefcb020e22
real-sandbox eval HEAD:    4b8afa7 (git_dirty false)
live UAT eval HEAD:        4b8afa7 (git_dirty false)
provider:                  opensandbox via Atlas OpenSandboxProvider adapter
OpenSandbox server:        1.2.0
OpenSandbox Python SDK:    1.2.0
Docker Engine:             29.6.0 (WSL2 Ubuntu)
execd image:               opensandbox/execd:v1.1.0
egress image:              opensandbox/egress:v1.1.7
sandbox image:             atlas-uat-python:3.12
workspace:                 /workspace/project
network:                   none (egress sidecar, dns+nft)
fixture digest:            30ec285bbdfcc53338c8b3ee67ed2a10f87078ed3f901abcfb8158587e4403f5
```

## Architecture

```text
Atlas Runtime
  -> M3 authorization
  -> M4 durable execution
  -> M5 trusted SandboxBinding
  -> AnyCode SandboxProvider protocol
  -> OpenSandboxProvider (thin adapter)
  -> OpenSandbox Python SDK
  -> local OpenSandbox server
  -> Docker Engine
  -> isolated Atlas Sandbox
  -> /workspace/project
```

OpenSandbox is execution infrastructure only. Atlas remains the sole runtime and retains M3
authorization, M4 execution/replay authority, M5 binding authority, M6 approval authority, and M7
evidence authority. Atlas does not call Docker.

## Real isolation evaluator at `4b8afa7`

`experiments/eval_real_sandbox.py` ran against the live OpenSandbox server:

```text
status:                    PASS
provider_is_real:          true
provider_is_external_cloud: false
provider_is_self_hosted:   true
opensandbox_server_health: healthy
runtime_backend:           docker
sandbox_created:           true
sandbox_ready:             true
sandbox_destroyed:         true
sandbox id:                0436c794-2047-4032-83b6-a7d5c618d134
workspace:                 /workspace/project
file upload/read:          PASS
python --version:          Python 3.12.16 (exit 0)
small Python script:       stdout 45 (exit 0)
network_policy_requested:  none
network_policy_accepted:   true
network_policy_verified:   PASS
host-only sentinel:        not visible in sandbox
sandbox-only sentinel:     not visible on Host; read-back PASS
host fixture digest:       unchanged
explicit workspace export: PASS
```

The sandbox also had an `opensandbox/egress:v1.1.7` sidecar attached. An empirical TCP egress probe
returned blocked; a second manual probe in an Alpine sandbox confirmed raw-IP TCP and HTTP were
blocked while the egress sidecar was attached.

## Three live coding UAT runs

| Run | Run ID | Result | Classification |
| --- | --- | --- | --- |
| 1 | `uat-fd65019b7d8c4a21a9050d75fa392173` | FAIL | `MODEL_FAILURE` |
| 2 | `uat-09ad802b361b4e88a15147cec0c8c597` | PASS | none |
| 3 | `uat-10449880cf68480c9479c1f3e29ac2cc` | PASS | none |

Run 1 explored the fixture but made no edits; the external oracle failed and the test suite passed
vacuously. That is a model failure, not a sandbox/runtime failure, and is retained. Runs 2 and 3
changed exactly `csv_parser.py` and `tests/test_csv_parser.py`, passed the hidden oracle and
`python -m pytest -q`, and passed changed/unexpected/removed-file inspection.

### Per-run sandbox metrics

| Metric | Run 1 | Run 2 | Run 3 |
| --- | ---: | ---: | ---: |
| sandbox created / ready / destroyed | true / true / true | true / true / true | true / true / true |
| sandbox id | `376afa4a-...` | `88863482-...` | `380017e9-...` |
| same binding for all project tools | true | true | true |
| network requested / accepted / verified | none / true / PASS | none / true / PASS | none / true / PASS |
| host-only sentinel visible | false | false | false |
| sandbox-only sentinel visible on Host | false | false | false |
| host fixture digest unchanged | true | true | true |
| commands executed | 11 | 13 | 14 |
| files uploaded / read / written | 3 / 2 / 0 | 3 / 2 / 2 | 3 / 2 / 1 |
| M7 sandbox lifecycle | READY, CLOSED | READY, CLOSED | READY, CLOSED |
| M7 evidence gaps / conflicts | 0 / 0 | 0 / 0 | 0 / 0 |
| host workspace fallbacks | 0 | 0 | 0 |
| sandbox recreations | 0 | 0 | 0 |

No `sandbox-*` or `sandbox-egress-*` Docker containers remained after the evaluator or any UAT run.

## Setup verified

The verified topology is Windows Atlas client → WSL2 Ubuntu OpenSandbox server on
`127.0.0.1:8080` → WSL Docker Engine. Windows `localhost:8080` reached the WSL server. The UAT image
is built locally from `experiments/fixtures/uat_image/Dockerfile` and includes Git and pytest.
Details: [`docs/stages/opensandbox-local.md`](../../docs/stages/opensandbox-local.md).

## Quality gates

```text
ruff check .                 PASS
ruff format --check .        PASS
pyright                      0 errors, 0 warnings, 0 informations
pytest                       PASS ×2
integrated verifier          PASS
```

## Hard-zero metrics

```text
host_workspace_fallbacks          0
unexpected_host_writes            0
sandbox_recreations_after_lost    0
model_selected_provider           0
model_selected_workspace          0
fake_provider_used_in_real_uat    0
external_cloud_credentials_required 0
```

## Known limitations

- **OS-PROCESS REATTACHMENT = NOT IN SCOPE.** OpenSandbox can reconnect by id, but Atlas does not
  treat provider reconnect as proof of trusted handle continuity. The adapter deliberately fails
  closed on a runtime-local cache miss and never creates a replacement sandbox.
- M5 binding metadata remains runtime-scoped.
- Network-none enforcement depends on the local OpenSandbox egress sidecar; the verified run used
  `dns+nft` and empirical probes. This is not a cryptographic/kernel-formal isolation proof.
- The local OpenSandbox server is loopback-only development infrastructure. It is not hardened for
  public or multi-tenant deployment.
- No snapshot restore, secure runtime (gVisor/Kata/Firecracker), or patch apply in this stage.
- Live LLM behavior is nondeterministic: Run 1 failed to edit, while Runs 2 and 3 passed external
  acceptance.

## Reproduction

```powershell
# WSL: start the loopback OpenSandbox server (see docs/stages/opensandbox-local.md)
# Build the UAT image once:
wsl docker build -t atlas-uat-python:3.12 /mnt/d/Work/AtlasOS/experiments/fixtures/uat_image

cd D:\Work\AtlasOS
$env:PYTHONPATH = "src;."
.\.venv\Scripts\python.exe experiments\eval_real_sandbox.py
.\.venv\Scripts\python.exe experiments\uat_v2.py --runs-root experiments\runs\uat
```
