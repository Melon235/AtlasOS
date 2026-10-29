# V2 M5 — Project Sandbox Binding

Status: PASS. Baseline M4.1 final `51e2b7d`.

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

## Known limitations

- M5 v1 uses a deterministic local FakeSandboxProvider for formal PASS; no real cloud provider is
  required.
- Network is fixed to `none`; domain allowlists and secret injection are out of scope.
- No automatic resume/snapshot/workspace replay after runtime restart.
- No patch apply.
- Orphan remote resources after a crash between provider create and durable handle recording are
  documented operational debt.
- Bash commands can access other paths inside the sandbox; only `cwd` is constrained to the
  logical workspace. Host isolation is the provider's responsibility.

## Tests / evaluation

```text
tests/integration/test_project_sandbox.py
examples/m5_project_sandbox_eval.py
```
