# V2 M5 — Project Sandbox Binding

Status: deterministic M5 verification PASS at baseline M4.1 final `51e2b7d`; real Atlas Sandbox
provider integration implemented in the V2 real-sandbox stage. Live sandbox execution requires Host
Daytona credentials and is reported separately, never inferred from the fake-provider PASS.

> Atlas does not implement a second sandbox runtime; M5 binds one project task to one
> AnyCode-managed sandbox so all workspace-bound tools operate on the same isolated project copy,
> while M3 remains the authorization authority and M4 remains the durable execution authority.

## Architecture

```text
Atlas Runtime
  M1 → M2 → M3 → M4 → AnyCode ToolExecutor
                         ↓
                  ToolDefinition.execute
                         ↓
              SandboxWorkspaceTools
                         ↓
              trust binding → SandboxHandle
                         ↓
              AnyCode SandboxProvider
                         ↓
                 /workspace/project
```

`ProjectSandboxManager` owns only binding lifecycle. It does not execute tools, authorize actions,
or implement idempotency. No second Runtime, ToolRegistry, ToolExecutor, or durable state store is
created.

## Trusted binding

`ProjectSandboxBinding` records:

```text
run_id / task_id / worker_id
provider_name / sandbox_handle
workspace_root = /workspace/project
source_commit / source_digest / bundle_digest
state
created_at
```

`SandboxProjectConfig` is trusted deployment config. Model/task text cannot select provider,
image, workspace, network, or secrets. M5 v1 fixes:

```text
network = none
secret_references = {}
workspace_root = /workspace/project
image = pinned trusted value
```

`SandboxSpec.context` reuses the existing `ExecutionContext`; no AtlasSandboxExecutionContext or
new identity system exists.

## One task, at most one sandbox

```text
task_id → zero or one SandboxHandle
```

Per-task single-flight lock guarantees 20 concurrent `ensure_binding(task)` calls create one
sandbox. Different tasks may have different handles. There is no execution epoch or generation.

If a sandbox is lost, invalid, destroyed, or cancelled:

```text
binding → LOST / CLOSED
task → terminal
```

No recreation, no resumption on a new sandbox, and no host fallback. M4 replay history remains
bound to the original filesystem.

## Six logical tools, one physical backend

The one registry keeps the original logical definitions:

```text
bash
file_read
file_write
file_edit
grep
list_files
```

Same name, description, input model, side_effecting flag, and M3 action identity. Only
`ToolDefinition.execute` is replaced with the sandbox implementation. M1 never sees a Host/Sandbox
duplicate surface; M3/M4 action identity is unchanged.

All six use the same `SandboxHandle` and `/workspace/project`. Per-binding `asyncio.Lock` serializes
workspace operations. Different bindings can run concurrently. The lock is not an idempotency or
authorization mechanism.

## File semantics

- `file_read` uses `SandboxProvider.read_file` and preserves offset/limit/line formatting/range
  errors.
- `file_write` uses `write_file`, preserves side-effect semantics and parent creation.
- `file_edit` performs trusted read/count/replace/write, preserving zero/multiple-match errors.
- `grep` and `list_files` execute a fixed Python helper with base64-encoded arguments, never raw
  shell interpolation, and preserve common-case output/limits.
- `bash` uses `SandboxProvider.execute` with `cwd` inside the workspace. Output caps and timeouts
  are preserved.

## Provisioning

Clean local Git repo only:

```text
validate clean worktree
resolve HEAD
git bundle create
size bound
upload /tmp/project.bundle
git clone /tmp/project.bundle /workspace/project
verify HEAD == source commit
```

Dirty worktrees, submodules/LFS, oversized bundles, and missing Git fail before READY. Host Git
commands use argument lists, never shell interpolation. The source repo is never mounted into the
sandbox.

## Loss / cancellation / recovery

```text
cancel → provider.cancel/destroy → CLOSED
loss → LOST
```

Both are terminal. M5 v1 does not auto-resume, snapshot-restore, or reprovision. A provisioning
crash may leave an orphan remote sandbox; Atlas best-effort destroys known handles and documents
the possible leak rather than adding a distributed coordinator.

## Patch export

At task end, inside the sandbox:

```text
git status --porcelain
git add -N --all
git diff --binary --no-ext-diff HEAD
```

Result:

```text
base_commit
patch_text
patch_digest
changed_file_count
```

The patch is untrusted artifact data. It is never auto-applied to Host. Host remains unchanged.

## M3 / M4

M3 still authorizes every logical tool call before `ToolDefinition.execute`. M5 does not call
providers before M3. M4 still wraps side-effecting `file_write`, `file_edit`, and `bash` with
AnyCode idempotency; reads are not cached. A replay skips the sandbox side effect. A lost sandbox
cannot continue M4 history on a replacement.

## Lifecycle is not model-facing

No `sandbox.create`, `sandbox.destroy`, `sandbox.export`, `sandbox.reset`, or `sandbox.attach` tools
exist. External API tools remain Host control-plane operations; the local workspace tool surface is
the only sandbox-bound surface. No alternate `filesystem.*`, `shell.*`, `python.exec`, or
`code.run` bypass is registered.

## Atlas Sandbox provider (real integration)

**Atlas Sandbox** is the product capability. M5 owns trusted provider selection, binding lifecycle,
workspace policy, and fail-closed behavior. The AnyCode-compatible `SandboxProvider` owns the actual
remote resource, process execution, remote filesystem, network enforcement, and resource
destruction. Atlas does not import the Daytona SDK or implement a second sandbox runtime.

The current implementation is:

```text
Atlas Runtime
  -> M3 -> M4 -> M5 ProjectSandboxBinding
  -> AnyCode SandboxProvider protocol
  -> create_sandbox_provider("daytona")
  -> DaytonaSandboxProvider
  -> remote isolated sandbox
  -> /workspace/project
```

Trusted configuration is [`experiments/configs/v2-uat.yaml`](../../experiments/configs/v2-uat.yaml):

```yaml
sandbox:
  provider: daytona
  image: python:3.12@sha256:...   # full image: includes Git for M5 bundle provisioning
  workspace_root: /workspace/project
  network: none
```

Provider, image/snapshot, workspace, and network policy never come from model output, tool
arguments, worker-profile text, or the task prompt. Daytona credentials come only from the SDK's
Host environment (`DAYTONA_API_KEY`, or `DAYTONA_JWT_TOKEN` + `DAYTONA_ORGANIZATION_ID`); Atlas
checks presence and never reads, logs, or persists the value. Missing credentials fail closed with
`CREDENTIAL_BLOCKED`; there is no fake-provider fallback.

Capability negotiation before create requires remote/isolated execution, persistent filesystem, file
transfer, and a network policy compatible with `none`. AnyCode reports `networking="allowlist"` for
Daytona because the adapter can enforce an allowlist or a complete block; the adapter maps
`network="none"` to `network_block_all=True`. Atlas accepts that report only as a network-none
capability, and the UAT/evaluator additionally performs an empirical egress probe. Unsupported
capabilities fail before sandbox creation.

Provisioning uploads a clean Git bundle with `provider.write_file`, creates the workspace directory,
clones inside the sandbox, and verifies the source commit. The Host fixture is never mounted.
Task-end export downloads an uncompressed tar archive created inside the sandbox with
`provider.read_file`; the Host extracts it with path-traversal checks and never applies it to the
Host fixture. A separate patch export remains available for provenance.

The lifecycle is `CREATE -> READY -> READ/WRITE/EXECUTE -> CLOSED/DESTROYED`. A lost sandbox becomes
`LOST`, is never recreated, and is never replaced by Host execution. Normal completion and failure
cleanup call `provider.destroy`; `sandbox.retain_on_failure` is an operator-only debug switch.

## Known limitations

- Deterministic regression tests use the in-repository `FakeSandboxProvider`. Live UAT requires a
  real provider and Host Daytona credentials; without them the formal status is
  `BLOCKED_NO_CREDENTIALS`, not PASS.
- The UAT image is a generic pinned Python image. External acceptance runs on the Host export; the
  model-side sandbox may not contain the same test tooling as the Host venv.
- Network is fixed to `none`; domain allowlists and secret injection are out of scope.
- No automatic resume/snapshot/workspace replay after runtime restart. Binding metadata is
  runtime-scoped in v1; a restarted runtime cannot prove it still owns the same sandbox and fails
  closed rather than recreating.
- OS-process sandbox reattachment is **not implemented in this stage**.
- No patch apply.
- Orphan remote resources after a crash between provider create and durable handle recording are
  documented operational debt; UAT failure cleanup is best effort and recorded.
- Bash commands can access other paths inside the sandbox; only `cwd` is constrained to the
  logical workspace. Host isolation is the provider's responsibility.

## Tests / evaluation

```text
tests/integration/test_project_sandbox.py
examples/m5_project_sandbox_eval.py
```
