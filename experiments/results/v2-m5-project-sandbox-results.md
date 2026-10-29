# V2 M5 Project Sandbox Formal Results

## Provenance

```text
starting HEAD:         51e2b7d
implementation commit: ea7b8ce
verification commit:   ea7b8ce（git_dirty=false）
formal record:         experiments/runs/v2-m5-project-sandbox.jsonl
```

## Quality gates

```text
ruff check .             All checks passed
ruff format --check .    181 files already formatted
pyright                  0 errors, 0 warnings, 0 informations
pytest                   233 passed
M5 focused:              18 passed
M4 regression:            9 passed
M1–M4 reliability:        7 passed
M3 regression:           14 passed
Control plane:            5 passed
```

## Formal metrics

| Metric | Result |
|---|---|
| sandbox_bindings_created | 1 |
| provider_create_calls | 1 |
| provider_destroy_calls | 0 |
| sandbox_ready | 1 |
| sandbox_lost | 1 |
| sandbox_closed | 0 |
| project_provisions | 1 |
| project_provision_failures | 0 |
| workspace_tool_calls | 6 |
| sandbox_file_reads | 4 |
| sandbox_file_writes | 3 |
| sandbox_file_edits | 1 |
| sandbox_greps | 1 |
| sandbox_list_files | 1 |
| sandbox_bash_calls | 9 |
| **host_workspace_tool_executions_for_sandbox_tasks** | **0** |
| **host_fallbacks** | **0** |
| **duplicate_sandbox_creations** | **0** |
| **sandbox_recreations_after_loss** | **0** |
| **workspace_serialization_violations** | **0** |
| provider_capability_failures | 0 |
| **network_policy_violations** | **0** |
| **secret_reference_violations** | **0** |
| **model_controlled_provider_violations** | **0** |
| **model_controlled_workspace_violations** | **0** |
| **M3_bypasses** | **0** |
| **M4_bypasses** | **0** |
| M4_replays | 1 |
| **sandbox_replay_tool_executions** | **0** |
| **host_repo_mutations** | **0** |
| patch_exports | 1 |
| patch_digest_valid | true |
| **unauthorized_executions** | **0** |

## Verified behavior

- Capability guard accepts a provider with file transfer, persistent filesystem, network-none,
  and non-process isolation; mismatches fail before create.
- Clean Git repo provisions once via a Git bundle; source HEAD is verified; dirty/oversized repos
  fail before sandbox create.
- One task → one handle; 20 concurrent binding requests create once and return the same handle.
- Different tasks receive different handles.
- All six logical workspace tools use the same handle and `/workspace/project`:
  `file_read`, `file_write`, `file_edit`, `grep`, `list_files`, `bash`.
- Same one registry, names, schemas, descriptions, side-effect flags, and M3 action identity.
- M3 authorizes before sandbox execution; DENY/HITL reject produce zero provider writes.
- M4 replay skips the sandbox write; same-key/different-input conflicts; reads are not cached.
- Host repository digest remains unchanged after sandbox writes, edits, bash, and patch export.
- Same-binding operations serialize; different bindings can execute concurrently.
- Lost/cancelled sandbox becomes terminal; no recreation and no host fallback.
- Output cap and timeout are preserved; sandbox result content is returned as untrusted text.
- Patch export includes modified tracked files and new files, with base commit and digest; patch
  is not applied to Host.
- Network is `none`; secret references are empty; image/provider are trusted config only.
- No sandbox lifecycle tools or alternate local filesystem/shell tools are registered.
- M2 worker metadata reaches M5 tools.

## Limitations

- Formal PASS uses deterministic `FakeSandboxProvider`; live Modal/cloud backends are optional
  smoke tests and are not required.
- M5 v1 fixes network=none, secrets={}, workspace=/workspace/project; no allowlists or credential
  proxy.
- No automatic resume/snapshot restore/workspace replay or patch apply. Binding metadata is runtime-scoped in v1; restart fails closed rather than recreating a sandbox.
- Possible orphan remote sandbox after a crash between provider create and durable handle record is
  documented operational debt.
- Bash is constrained by `cwd`, not by a complete command AST; sandbox isolation remains the
  provider's responsibility.

## Reproduction

```powershell
cd D:\Work\AtlasOS

.venv\Scripts\ruff.exe check .
.venv\Scripts\ruff.exe format --check .
.venv\Scripts\pyright.exe
.venv\Scripts\python.exe -m pytest

.venv\Scripts\python.exe -m pytest tests\integration\test_project_sandbox.py
.venv\Scripts\python.exe -m pytest tests\integration\test_durable_actions.py
.venv\Scripts\python.exe -m pytest tests\integration\test_m1_m4_reliability.py
.venv\Scripts\python.exe -m pytest tests\integration\test_action_safety.py
.venv\Scripts\python.exe -m pytest tests\integration\test_control_plane_e2e.py

# Formal record requires clean worktree.
.venv\Scripts\python.exe examples\m5_project_sandbox_eval.py
```
