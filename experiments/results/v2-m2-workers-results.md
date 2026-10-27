# V2 M2 Final Results — Scoped Worker Construction

## Provenance

```text
starting HEAD:              cad34ce
implementation commit:      5c217f8
final docs commit:          0494a42
git_dirty:                  false
timestamp:                  2026-10-27T15:05:48Z
formal record:              experiments/runs/v2-m2-workers-eval.jsonl

full pytest:                180 passed (run twice from clean tree)
M2 focused:                 8 passed
M2 × M3 integration:        7 passed
M3 regression:              14 passed
git status:                 clean
```

## Quality gates

```text
ruff check .             All checks passed
ruff format --check .    143 files already formatted
pyright                  0 errors, 0 warnings, 0 informations
pytest                   180 passed
```

## Formal metrics

| Metric | Result |
|---|---|
| Worker instances created | 4 (research, coding, general, malicious-task) |
| Profile resolution success rate | **1.000** |
| Trusted-field mutation violations | **0** |
| ExecutionContext alias violations | **0** |
| Prompt/runtime scope divergence | **0** |
| Recursive delegation violations | **0** |
| Worker M3 bypasses | **0** |
| Recovery context hash equality | **true** |
| M1 regression | **pass** |
| M3 regression | **pass** |

## What was removed

The active capability-authorization implementation was deleted:

```text
src/atlasos/delegation/
  authority.py, capabilities.py, validator.py, router.py,
  reviewer.py, gates.py, issuer.py, runtime.py, store.py, models.py
```

The old M2 capability tests and demo/eval scripts were removed. No active runtime code imports
`RunAuthority`, `GrantIssuer`, `AuthorizationRuntime`, `requested_scopes`, or capability gates.

Historical capability documents (central authorization, capability migration, old formal
results) remain only as marked historical records. They do not describe active code.

## What was added

```text
src/atlasos/workers/
  models.py    WorkerProfile, WorkerAssignment, WorkerTaskContext,
               WorkerRuntimeMetadata, WorkerInstanceRecord, WorkerInstance
  catalog.py   immutable four-profile WorkerCatalog
  renderer.py  deterministic trusted/untrusted context renderer
  factory.py   ScopedWorkerFactory
  store.py     atomic WorkerInstance sidecar
```

M2 creates focused workers without LLM calls, policy, action gate, HITL, or capability grants.

## M2 × M3 evidence

M3 sees trusted worker metadata via ExecutionContext attributes:

```text
agent identity:   research_worker:<worker_id>
worker_id
profile_id
task_id
operating_scope
parent_agent
principal
tenant
classification
```

Tests verify research normal GitHub action, research anomalous email (HITL), coding file action,
general-worker gate coverage, main-vs-worker equality, recovery, and malicious-task isolation.
The frozen M3 action policy/gate semantics were not changed; M3 now additively reports trusted
worker fields in `ActionContext`/`ActionAudit`.

## Old M2 test disposition

```text
UNCHANGED:              M1 regression / M3 action safety
REWRITTEN:              worker persistence/recovery tests (now WorkerStore + rendered hash)
MOVED_TO_M3:            capability router/reviewer/policy/HITL tests
REMOVED_OBSOLETE:       root grant, chain, parent scope subset, GrantIssuer,
                         RunAuthority, capability recovery tests
```

Old focused M2 suite (52 tests) was not mechanically preserved. It tested a removed authority
model; replacing it avoids two active architectures.

## M1 / M3 regression

M1 tool discovery/search/dispatch/MCP tests remain unchanged and pass. M3's 14 action-safety tests
remain unchanged and pass, including zero unauthorized execution and no hard-deny overrides.

## Known limitations

- M2 workers do not recursively delegate; `can_delegate=false` and no delegate tool.
- Operating scope is coarse behavioral metadata, not a permission allowlist.
- Worker profiles are immutable Python definitions; there is no agent-writable profile loader.
- No authentication/IAM; trusted host supplies parent context and worker profile ownership.
- Worker records are local trusted-host sidecars, not a distributed identity database.
- M2 does not authorize tool actions; every action still goes through M3.
- M3 remains opt-in at tool-runtime construction.

## Reproduction

```powershell
cd D:\Work\AtlasOS

.venv\Scripts\ruff.exe check .
.venv\Scripts\ruff.exe format --check .
.venv\Scripts\pyright.exe
.venv\Scripts\python.exe -m pytest

.venv\Scripts\python.exe -m pytest tests\integration\test_scoped_workers.py
.venv\Scripts\python.exe -m pytest tests\integration\test_worker_action_safety.py
.venv\Scripts\python.exe -m pytest tests\integration\test_action_safety.py

# Formal record requires clean worktree.
.venv\Scripts\python.exe examples\m2_scoped_worker_eval.py
```
