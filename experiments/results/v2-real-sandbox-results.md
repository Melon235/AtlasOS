# V2 Real Atlas Sandbox — Results

Status: **REAL ATLAS SANDBOX PROVIDER = IMPLEMENTED;
LIVE REAL-SANDBOX EVALUATION = BLOCKED_NO_CREDENTIALS**

The Atlas M5 execution environment now routes project work through a real AnyCode-compatible
`SandboxProvider` (Daytona) instead of the deterministic local provider. The live path has no fake
fallback. At the evaluated implementation HEAD the Host had no Daytona credential, so no remote
sandbox was created and no real-isolation PASS is claimed.

## Provenance

```text
implementation HEAD:       7c34f22ed8339c54b08c54862d3e8d9f43591e7a
real-sandbox evaluator:    experiments/eval_real_sandbox.py at 7c34f22 (git_dirty false)
live UAT harness:          experiments/uat_v2.py at 7c34f22 (git_dirty false)
provider:                  daytona via anycode create_sandbox_provider("daytona")
anycode-py:                0.9.0
daytona SDK:               0.227.0
image:                     python:3.12@sha256:17b9724493d916f56565c131f389faff2597f46dec6ce09516f95125bc9f519f
workspace:                 /workspace/project
network:                   none
credential source:         Host environment / Daytona SDK only; never persisted
fixture digest:            30ec285bbdfcc53338c8b3ee67ed2a10f87078ed3f901abcfb8158587e4403f5
```

## Architecture

```text
Atlas Runtime
  -> M3 authorization
  -> M4 execution authority
  -> M5 trusted SandboxBinding
  -> AnyCode SandboxProvider protocol
  -> create_sandbox_provider("daytona")
  -> Daytona remote isolated sandbox
  -> /workspace/project
```

Atlas owns provider selection, trusted configuration, binding lifecycle, workspace policy, and
fail-closed behavior. The provider owns the remote process, remote filesystem, network enforcement,
and resource destruction. Atlas does not import the Daytona SDK directly and does not implement a
second sandbox runtime.

## What was verified deterministically

- `experiments/uat_cli.py` constructs the real provider through
  `create_sandbox_provider("daytona")`, calls `capabilities()` before create, refuses the fake
  provider on the live path, and selects provider/workspace/image/network only from trusted Host
  configuration.
- M5 accepts AnyCode's `networking="allowlist"` report as network-none-capable, requires remote
  isolation, persistent filesystem, and file transfer, and fails closed on any mismatch.
- Unit coverage proves the Daytona adapter maps `network="none"` to `network_block_all=True` and
  does not put credentials in labels, secrets, or commands.
- The scripted smoke run exercises the same CLI, M1–M7 stack, M5 Git-bundle provisioning, all six
  project tools, M7 sandbox lifecycle evidence, workspace export, and cleanup through the local fake
  provider. It is classified `SCRIPTED_SMOKE`, not real acceptance.
- Workspace export is a control-plane tar transfer through `provider.read_file`, with Host-side
  traversal checks; the Host fixture is never mounted or mutated.
- Failure cleanup calls `provider.destroy`; a LOST sandbox is terminal and never recreated or
  replaced by Host execution.

## Real evaluator result at `7c34f22`

`experiments/eval_real_sandbox.py` failed closed before create:

```text
status:                    BLOCKED_NO_CREDENTIALS
failure_class:             CREDENTIAL_BLOCKED
sandbox_created:           false
sandbox_destroyed:         false
provider_is_real:          false
host fixture unchanged:    true
fake_provider_used:        0
```

## Live UAT result at `7c34f22`

`experiments/uat_v2.py` (no `--scripted`) failed closed before provider construction:

```text
final_status:              BLOCKED_NO_CREDENTIALS
failure_class:             CREDENTIAL_BLOCKED
provider_name:             daytona
provider_is_real:          false
sandbox_created:           false
sandbox_ready:             false
sandbox_destroyed:         false
host fixture unchanged:    true
host_workspace_fallbacks:  0
fake_provider_used:        0
```

No fake-provider or scripted result is counted as real user acceptance.

## Required real-run evidence (not yet produced)

A credential-backed run must produce all of the following before
`REAL ATLAS SANDBOX ISOLATION = PASS`:

```text
real Daytona provider used
real remote sandbox created
commands actually executed remotely
file operations inside the sandbox
all project tools use the same binding/handle/workspace
host-only sentinel not visible in the sandbox
sandbox-only sentinel not visible on the Host
host fixture digest unchanged
network_policy=none with no observed egress (or explicitly UNKNOWN with capability evidence)
no Host fallback and no sandbox recreation
sandbox destroyed after success and failure
external acceptance runs on the exported workspace
three live coding UAT runs recorded in order
```

## Quality gates (implementation)

```text
ruff check .                 PASS
ruff format --check .        PASS
pyright                      0 errors, 0 warnings, 0 informations
pytest                       PASS (307 tests, twice)
integrated verifier          PASS at 7c34f22 (git_dirty=false)
```

## Reproduction

```powershell
cd D:\Work\AtlasOS
uv sync --extra sandbox
$env:DAYTONA_API_KEY = "<Host secret>"
$env:PYTHONPATH = "src;."

.\.venv\Scripts\python.exe experiments\eval_real_sandbox.py
.\.venv\Scripts\python.exe experiments\uat_v2.py --runs-root experiments\runs\uat
```

## Known limitations

- OS-process sandbox reattachment is **not implemented in this stage**. A restarted process cannot
  prove ownership of the old remote sandbox and fails closed; Atlas never creates a replacement.
- The runtime-scoped M5 binding limitation remains.
- The UAT image is a generic pinned Python image; external acceptance runs on the Host export, so
  model-side test tooling may differ from the Host venv.
- Missing Host credentials produce `BLOCKED_NO_CREDENTIALS`; they never select a fallback provider.
- Isolation claims are limited to provider-declared capability, Atlas routing evidence, and the
  empirical Host/sandbox separation checks. No cryptographic or formally verified kernel isolation
  is claimed.
