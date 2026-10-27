# V2 M2 — Central Capability Authorization Runtime

Status: **PASS after semantic correction**; formal measurements at clean commit `5d1aace` are in
`experiments/results/v2-m2-correction-results.md`.
Baseline: M1 final `9b0d861`; pre-correction M2 docs at `b8d2f32`; V1 frozen at `bd6134b`.

## What M2 answers

M2 gives the runtime a trustworthy answer to:

- who is the principal;
- which agent is acting;
- what trusted run ceiling and authorization operation produced its authority;
- what its effective capabilities are;
- whether the runtime safely authorized the agent;
- whether that committed authority survives crash/resume.

It does **not** decide whether a specific tool call is authorized. `github.issue.create` does
not require `github.write` in this stage; that belongs to M3.

```text
SPAWN LINEAGE != AUTHORITY LINEAGE
ORCHESTRATOR != CAPABILITY AUTHORITY
MODEL INTENT != RUNTIME AUTHORITY
REQUEST != ROUTE != REVIEW != APPROVAL != GRANT
```

Only a runtime-committed grant creates agent authority.

## Reused AnyCode 0.9.0 seams

| Concern | AnyCode seam | Atlas use |
|---|---|---|
| identity / chain | `anycode.identity.context.ExecutionContext` | copied, never replaced |
| capabilities | `DelegationGrant.scopes` | the only capability store |
| actor | `ToolUseContext.agent.name` | runtime canonicalizes to `agent:<name>` |
| delegation policy | `PolicyRequest`, `PolicyEnforcer` | one request per authorization/delegation |
| human approval | `ApprovalManager` / `ApprovalGate` | existing HITL seam |
| reviewer model | `AgentRunner` + structured output | one-shot, empty tool registry |
| child context | `AgentConfig.execution_context` | set only from a committed outcome |

No `AtlasExecutionContext`, `AtlasDelegationGrant`, `AtlasPolicyDecision`, or second approval
engine exists. There is no second policy engine or checkpoint engine.

## Authority sources

```text
Principal / Trusted Host
        ↓
Run Authority Ceiling (RunAuthority)
        ↓
Central Authorization Runtime
        ↓
Per-Agent ExecutionContext
```

`RunAuthority(principal, run_id, allowed_scopes, expires_at)` is a frozen, canonicalized,
durable maximum envelope for the run. It is **not** any agent's current authority and is never
derived from an agent's scopes. The trusted host also supplies the main agent identity and
capability-free base metadata context; the bootstrap does **not** mint operational scopes for
the main agent.

The main agent and every child must pass through the same central pipeline:

```text
AuthorizationRequest
→ ceiling check
→ Router
→ Reviewer
→ Policy / HITL
→ GrantIssuer
→ per-agent ExecutionContext
→ persist
```

## Two distinct operations

### Central authorization — `authorize_agent()`

Used when an orchestrator asks the runtime to authorize a specialist (including itself):

```text
requester: agent:planner (may only have agent.orchestrate)
target:    agent:email_worker
requested: email.send
check:     requested_scopes ⊆ RunAuthority.allowed_scopes
```

The requester's effective scopes are irrelevant. The central grant is:

```text
DelegationGrant(
    delegator=principal,
    delegatee=target_agent,
    scopes=requested_scopes,
)
```

The stored chain is exactly `principal → target`; no spawn parent is encoded. The main agent is
not a capability authority.

### Explicit delegation — `delegate_capability()`

Used when an already-authorized agent deliberately attenuates part of its own authority:

```text
GitHubWorker effective: {github.read, github.write}
helper requested:       {github.read}
check:                  requested_scopes ⊆ parent effective scopes
grant:                  github_worker → issue_reader
chain:                  principal → github_worker → issue_reader
```

The run ceiling cannot be used to escalate the parent. `email.send` may be inside the run ceiling
but is still DENY for an explicit delegation from a parent that only has `github.read`.

The two APIs are separate. There is no model-controlled `mode` field and no shared ambiguous
semantics.

## Modules

```text
src/atlasos/delegation/
  authority.py    RunAuthority, RunAuthorityStore, trusted run bootstrap, hashing
  capabilities.py atomic scopes, canonicalization, intersection, expiry, hashing
  models.py       AuthorizationRequest, DelegationRequest, RouteDecision, outcomes
  validator.py    explicit-delegation structural/attenuation validation
  router.py       deterministic intent × capability route table
  reviewer.py     stateless AnyCode structured-output reviewer
  gates.py        authorization-only Policy/HITL adapters
  issuer.py       canonical grant writer (central and explicit)
  runtime.py      AuthorizationRuntime; both authority operations
  store.py        per-agent run_execution_context.json sidecar
```

## Capability model

```text
<domain>.<action>
^[a-z][a-z0-9_-]*\.[a-z][a-z0-9_-]*$
```

Atomic, no wildcard, no resource qualifier, no inheritance. Scopes are lowercased,
deduplicated, and stable-sorted. `github.write` does not imply `github.read`.

Effective capabilities are the intersection of every grant's scopes:

```text
S_effective = S1 ∩ S2 ∩ ... ∩ Sn
```

An empty delegation has zero capabilities.

## Hard validation and failure ordering

Central authorization checks, before any decision component:

- trusted `RunAuthority` exists and matches the run;
- requested scopes are non-empty and syntactically valid;
- `requested_scopes ⊆ RunAuthority.allowed_scopes`;
- requester actor exists in trusted runtime state.

Explicit delegation checks:

- parent context exists and has a chain;
- first grant delegator == context principal;
- chain is contiguous;
- current actor == chain tail;
- no expired grant;
- delegatee != actor;
- delegatee not already in the ancestor path;
- non-empty, syntactically valid scopes;
- requested scopes ⊆ parent effective scopes.

Ceiling violation returns `outside_run_authority` immediately. Router, Reviewer, Policy, HITL,
and GrantIssuer are not called. Human approval cannot override the ceiling.

## Deterministic routing

`IntentRouter` is table-driven, with no LLM, embeddings, vector store, or risk scalar. POLICY
and HITL are orthogonal gates.

| intent | scope | gates |
|---|---|---|
| research | github.read, rag.read | {} |
| read_data | filesystem.read, database.read | {} |
| modify_data | github.write, filesystem.write, database.write | {POLICY} |
| execute_code | shell.execute | {POLICY} |
| external_communication | email.send | {HITL} |
| manage_credentials | credentials/secrets/keys/tokens domains | {POLICY, HITL} |
| administrative | admin/iam/billing/security domains | {POLICY, HITL} |
| unmatched | any | {HITL} |

Multiple scopes union their gates. Unknown intent/scope combinations never default to AUTO.
Results are canonical sorted tuples and serialize identically for identical input.

## Decision Reviewer

`AnyCodeDecisionReviewer` uses AnyCode's `AgentRunner` and structured output with:

- empty `ToolRegistry`;
- `allowed_tools=[]` (only `_structured_output` is exposed);
- temperature 0, one turn, small token budget;
- no runtime, issuer, context, or grant reference;
- input = principal, actor, effective scopes, request, router output;
- output = AGREE / DISAGREE / UNCERTAIN + reason codes.

Composition only escalates:

```text
AGREE       -> gates unchanged
DISAGREE    -> gates ∪ {HITL}
UNCERTAIN   -> gates ∪ {HITL}
error/timeout/invalid output -> gates ∪ {HITL}
```

The Reviewer cannot remove POLICY or HITL, cannot expand the ceiling, and cannot issue a grant.

## Policy and HITL gates

One authorization request produces exactly one `PolicyRequest` when POLICY is required:

```text
action   = "delegate"
boundary = "a2a"
resource = "agent:<target>"
input    = intent, requested_scopes, task_summary, current_actor
context  = trusted run/parent ExecutionContext
```

POLICY runs before HITL. Policy DENY is final; the human gate is never asked and cannot override
it. Missing/unconfigured/erroring policy fails closed. Missing/disabled/erroring approval fails
closed. Human approval can only approve a request already inside the run ceiling. No pending
approval is persisted; while approval is pending the target capability does not exist.

## Issuance and per-agent contexts

`GrantIssuer` is runtime-internal and is not registered as a tool.

Central issuance creates exactly:

```text
principal → target_agent
```

Explicit issuance appends exactly:

```text
current_agent → target_agent
```

The child context is a `model_copy` of the trusted base/parent with only `delegation` replaced;
principal, subject, workload identity, tenant, classification, regions, credential references,
trace id, and attributes are preserved. The parent is frozen/immutable. The child is
structurally revalidated before the sidecar write. Different agents have independent contexts;
there is no global mutable current-capability set.

`AuthorizationRuntime.build_child_agent_config(base, outcome)` attaches only a committed context
to an `AgentConfig`. A denied outcome raises `DelegationDenied`; raw parent context is never used
as child authority.

## Durability

Two sidecars under the run directory:

```text
run_authority.json           trusted ceiling (hash-stable)
run_execution_context.json   actor -> committed ExecutionContext
```

The context sidecar stores all per-agent contexts, not conversation. Recovery validates both
and never re-runs Router/Reviewer/Policy/HITL. Crash before grant commit leaves no new context;
crash after commit restores it exactly. This is not a second checkpoint engine and not a durable
pending-approval workflow (M6).

## M1 boundary

M2 adds a separate package and does not modify the M1 tool runtime, core/searchable split,
`tool.search`, Top-3 disclosure, dispatchable set, `dispatch_action`, MCP hidden-by-default,
provider-safe naming, or M1 recovery. Capability authority is independent of tool visibility:

```text
allowed_tools != dispatchable tools != delegated capabilities
```

M2 still does **not** enforce tool capabilities. That is M3.

## Verification

```bash
ruff check .
ruff format --check .
pyright
python -m pytest
```

Updated acceptance matrix: [v2-m2-acceptance.md](v2-m2-acceptance.md).
Migration details: [v2-m2-semantic-migration.md](v2-m2-semantic-migration.md).
