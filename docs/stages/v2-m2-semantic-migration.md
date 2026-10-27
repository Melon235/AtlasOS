# HISTORICAL — superseded by [v2-m2-scoped-worker-migration.md](v2-m2-scoped-worker-migration.md).

# V2 M2 Semantic Migration — Parent Authority → Run Authority Ceiling

Baseline before correction: `b8d2f32`.
Scope: only the source model for **how an agent acquires capability**. Router, Reviewer,
Policy/HITL gates, GrantIssuer, ExecutionContext/DelegationGrant, persistence, recovery,
fail-closed behavior, and M1 boundaries were retained.

## Old vs new

| Concern | Pre-correction | Semantic correction |
|---|---|---|
| Main authority source | trusted root grant `principal -> planner` with run scopes | `RunAuthority` ceiling; planner starts capability-free and must call `authorize_agent()` |
| Child authority source | `requested_scopes ⊆ parent effective scopes` for all child grants | central authorization checks `requested_scopes ⊆ RunAuthority.allowed_scopes`; spawn parent irrelevant |
| Explicit attenuation | same path as central authorization | separate `delegate_capability()`; only that path checks parent effective scopes |
| Grant chain | `principal -> planner -> worker` for every child | central: `principal -> target`; explicit: `parent -> target` appended |
| Orchestrator | capability authority | orchestration only; no specialist capability inheritance |
| Run ceiling | implicit in planner context | immutable, canonical, persisted `run_authority.json` |
| HITL | bounded by parent scopes | bounded by run ceiling; cannot override ceiling or policy DENY |
| Reviewer/Policy | authority source | decision inputs only; cannot expand ceiling |
| Main agent | bypassed central pipeline | same central pipeline as child |
| Recovery | per-agent contexts | run authority hash + per-agent contexts; no re-review |

## Call-site audit before change

The pre-correction parent-scope assumption was in:

- `DelegationRuntime.propose()` / `_pipeline` — every child request used
  `DelegationValidator.validate_request(parent, actor, request)`;
- `DelegationValidator.validate_request()` — `requested_scopes ⊆ effective_scopes(parent)`;
- `GrantIssuer.issue()` — always appended `actor -> delegatee` to parent chain;
- constructor — `root_context` carried the run's scopes as the main agent's grant;
- `build_child_agent_config()` — accepted a parent-derived outcome;
- recovery — restored contexts but had no separate run ceiling truth.

## Migration stages

### R1 — Audit

Call sites documented above; no behavior changed.

### R2 — Add RunAuthority

Added `authority.py`:

- frozen `RunAuthority(principal, run_id, allowed_scopes, expires_at)`;
- canonical scope validation;
- `run_authority_hash`;
- atomic `RunAuthorityStore` (`run_authority.json`);
- `TrustedRunIdentity` and `bootstrap_run_authority()` which returns a ceiling plus a
  **capability-free** base context. The main agent gets no operational scope at bootstrap.

### R3 — Central Authorization

Added `AuthorizationRequest` (`target_agent`, `intent`, `requested_scopes`, `task_summary`,
`reason`; `extra="forbid"`) and `AuthorizationRuntime.authorize_agent()`:

```text
requester actor from ToolUseContext
hard ceiling check
→ router → reviewer → policy/hitl → grant issuer
```

The requester and target are separate; requester scopes are not consulted. Outside-ceiling
requests return `outside_run_authority` before Router/Reviewer/Policy/HITL/GrantIssuer.

### R4 — Main and Child

Both call `authorize_agent()`. A child config is built only from a committed outcome
(`build_child_agent_config`). Central contexts use a one-grant `principal -> target` chain.

### R5 — Explicit Delegation

Existing subset logic moved behind `delegate_capability()`. It is a distinct API and does not
consult/expand through the run ceiling. The old `propose()` name remains only as a compatibility
alias to central authorization; new code should use the explicit APIs.

### R6 — Persistence / Recovery

- `run_authority.json`: ceiling and hash.
- `run_execution_context.json`: all per-agent contexts.
- Resume validates both, restores exact committed contexts, and never re-runs decision
  components.
- Crash before commit creates no target context; crash after commit restores it.

### R7 — Acceptance

New A/D/R matrix and E2E scenarios; old M2 tests were classified below.

## Old M2 test classification

| Group | Classification | Reason |
|---|---|---|
| T01–T02 root bootstrap / request identity | **REPLACED** | old root grant bootstrap no longer exists; replaced by RunAuthority/A01/A02 and request identity tests |
| T03–T09 chain, expiry, scopes | **UNCHANGED** | explicit-delegation structural checks remain |
| T10 parent subset as central invariant | **REPLACED** | central is now `requested_scopes ⊆ RunAuthority`; parent subset lives in D01/D02 |
| T11 intersection | **UNCHANGED** | capability resolver unchanged |
| T12 ancestor expiry | **SEMANTICALLY UPDATED** | central grant expiry is bounded by run authority expiry |
| T13–T18 router | **UNCHANGED** | deterministic route table reused |
| T19–T25 reviewer/gates/issuer pipeline | **SEMANTICALLY UPDATED** | same components now run for both central authorization and explicit delegation; fixtures use RunAuthority |
| T26–T33 policy/HITL fail-closed/pending | **SEMANTICALLY UPDATED** | bounded by run ceiling; composition retained |
| T34–T36 issuer chain | **SEMANTICALLY UPDATED** | central grant is one `principal -> target` grant, not parent chain append |
| T37 revalidation | **UNCHANGED** | child context still revalidated before commit |
| T38–T40 child config/siblings/field preservation | **SEMANTICALLY UPDATED** | contexts come from central outcomes |
| T41–T45 recovery | **SEMANTICALLY UPDATED** | run authority sidecar added; contexts per agent |
| T46–T49 M1 regression | **UNCHANGED** | M1 untouched |
| T50 E2E | **SEMANTICALLY UPDATED** | scenarios now start from a low-privilege planner and run ceiling |

No test was weakened to pass: updates changed the authority source under test and added stricter
central checks.

## New tests

- `tests/integration/test_central_authorization.py` — A01–A25, D01–D08, R01–R09 and E2E
  low-privilege/independent-specialist/unknown-route/attenuation scenarios.
- Existing M2 tests were retained where their semantics remain valid; fixtures now build
  `RunAuthority` + capability-free base context.

## Invariants after migration

```text
SPAWN LINEAGE != AUTHORITY LINEAGE
ORCHESTRATOR != CAPABILITY AUTHORITY
RUN AUTHORITY CEILING >= ANY AGENT GRANT
CENTRAL: requested_scopes ⊆ RunAuthority.allowed_scopes
EXPLICIT: requested_scopes ⊆ parent effective scopes
MODEL INTENT != RUNTIME AUTHORITY
HUMAN APPROVAL != CEILING ELEVATION
ONLY RUNTIME-COMMITTED GRANT CREATES AGENT AUTHORITY
```
