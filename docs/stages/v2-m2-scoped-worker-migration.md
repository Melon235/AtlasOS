# V2 M2 Migration — Capability Authorization → Scoped Worker Construction

Starting repository state:

```text
HEAD:       cad34ce
worktree:   clean
M3 final:   cad34ce / verification 697711b
```

## Inventory matrix (pre-change)

The old `src/atlasos/delegation/` package was audited file-by-file:

| Old file / object | Decision | Reason |
|---|---|---|
| `authority.py` / `RunAuthority`, `RunAuthorityStore` | **DELETE** | capability ceiling no longer active |
| `capabilities.py` / scope canonicalization, intersection | **DELETE** | capability model removed |
| `validator.py` / parent scope subset | **DELETE** | no capability attenuation in M2 |
| `router.py` / intent × capability gates | **DELETE** | M3 owns concrete action policy |
| `reviewer.py` / capability reviewer | **DELETE** | M3 owns action anomaly reviewer |
| `gates.py` / delegation Policy/HITL | **DELETE** | M3 owns action gates |
| `issuer.py` / `GrantIssuer` | **DELETE** | no capability grants |
| `runtime.py` / `AuthorizationRuntime` | **DELETE** | no active capability runtime |
| `store.py` / per-agent capability contexts | **DELETE** | replaced by worker record persistence |
| `models.py` / capability requests/outcomes | **DELETE** | replaced by worker models |
| `bootstrap.py` | **DELETE** (already removed earlier) | no root capability bootstrap |
| old M2 tests | **REMOVED** | capability semantics obsolete |
| old M2 demo/eval | **REPLACED** | worker construction eval |

Reused concepts:

| Concern | New M2 use |
|---|---|
| AnyCode `ExecutionContext` | child worker context, metadata |
| AnyCode `AgentConfig` | worker model/tools/prompt/context |
| AnyCode `Agent` constructor | optional `WorkerInstance.instantiate()` over M1 registry/executor |
| M1 ToolRegistry / base tools | validated profile tool namespace; no second registry |
| M3 ActionSafetyGate | downstream action authorization; never invoked by M2 |

## New repository shape

```text
src/atlasos/workers/
  models.py    WorkerProfile, WorkerAssignment, WorkerTaskContext,
               WorkerRuntimeMetadata, WorkerInstanceRecord, WorkerInstance
  catalog.py   immutable trusted WorkerCatalog and four default profiles
  renderer.py  deterministic trusted/untrusted prompt rendering
  factory.py   ScopedWorkerFactory
  store.py     atomic WorkerInstance sidecar
```

## Semantic comparison

| Old M2 | Final M2 |
|---|---|
| planner requests capabilities for child | planner selects a trusted worker profile |
| `requested_scopes ⊆ RunAuthority` | no scopes or authority ceiling |
| `GrantIssuer` commits authority | factory persists a worker construction record |
| cap router/reviewer/policy/HITL | none in M2; M3 owns action safety |
| delegation chain | child context with `delegation = ()` |
| capability HITL | none during construction |
| Worker identity from grant | `AgentConfig.name = <profile>:<worker_id>` |
| capability recovery | worker record + deterministic rerender/hash |
| `build_child_agent_config` from capability outcome | `ToolUseContext` from child context |

## Old test migration

| Old group | Disposition |
|---|---|
| root grant bootstrap | **REMOVED_OBSOLETE** |
| chain validation / cycles / scopes / expiry | **REMOVED_OBSOLETE** |
| capability router | **MOVED_TO_M3** (M3 deterministic action policy) |
| capability reviewer | **MOVED_TO_M3** (action reviewer semantics) |
| delegation policy/HITL | **MOVED_TO_M3** |
| grant issuer / parent subset | **REMOVED_OBSOLETE** |
| capability recovery | **REWRITTEN** as worker record/renderer recovery |
| M1 regression | **UNCHANGED** |
| M3 action safety | **UNCHANGED** |

## No active old authority runtime

After migration, `src/atlasos/delegation/` no longer exists. No production module imports
`AuthorizationRuntime`, `RunAuthority`, `GrantIssuer`, `requested_scopes`, or capability gates.
Historical central-authorization documents remain marked historical; they are not runtime code.

## M3 integration change

M3's `ActionContext`/`ActionAudit` received optional trusted worker fields (`worker_id`,
`profile_id`, `parent_agent`, `operating_scope`) and reads nested `atlas_worker` metadata. This is
an additive integration seam; M3 policy/decision semantics are unchanged and all M3 tests remain
green.

## One-vote closure

- Capability authorization no longer active.
- RunAuthority/GrantIssuer removed.
- Planner cannot choose tools/scope/turns/delegation.
- WorkerProfile is immutable host data.
- Child context is distinct and clears delegation.
- Task text cannot change metadata.
- M2 performs no HITL/action authorization.
- No second registry/executor/reviewer/policy.
- Worker defaults cannot delegate.
- Recovery uses record + deterministic rerender, not prompt parsing.
