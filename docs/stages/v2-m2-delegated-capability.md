# V2 M2 — Delegated Capability Runtime

Status: **implemented and acceptance-tested**, pending the formal result record.
Baseline: M1 final commit `9b0d861`; V1 remains frozen at `bd6134b`.

## What M2 answers

M2 gives the runtime a trustworthy answer to:

- who is the principal;
- which agent is acting;
- which delegation chain produced its authority;
- what its effective capabilities are;
- whether the runtime safely issued a child delegation;
- whether that committed authority survives crash/resume.

It does **not** decide whether a specific tool call is authorized. `github.issue.create` does
not require `github.write` in this stage; that belongs to M3.

```text
MODEL INTENT != RUNTIME AUTHORITY
REQUEST != ROUTE != REVIEW != APPROVAL != GRANT
```

Only a runtime-committed `DelegationGrant` creates delegated authority.

## Reused AnyCode 0.9.0 seams

| Concern | AnyCode seam | Atlas use |
|---|---|---|
| identity / chain | `anycode.identity.context.ExecutionContext` | copied, never replaced |
| capabilities | `DelegationGrant.scopes` | the only capability store |
| actor | `ToolUseContext.agent.name` | runtime canonicalizes to `agent:<name>` |
| delegation policy | `PolicyRequest`, `PolicyEnforcer` | one request per delegation |
| human approval | `ApprovalManager` / `ApprovalGate` | existing HITL seam |
| reviewer model | `AgentRunner` + structured output | one-shot, empty tool registry |
| child context | `AgentConfig.execution_context` | set only from committed child context |

No `AtlasExecutionContext`, `AtlasDelegationGrant`, `AtlasPolicyDecision`, or second approval
engine exists.

## Modules

```text
src/atlasos/delegation/
  bootstrap.py    trusted host -> root ExecutionContext
  capabilities.py atomic scopes, canonicalization, intersection, expiry, hashing
  models.py       DelegationRequest, RouteDecision, ReviewerResult, DelegationOutcome
  validator.py    hard validation before routing
  router.py       deterministic intent × capability route table
  reviewer.py     stateless AnyCode structured-output reviewer
  gates.py        delegation-only Policy/HITL adapters
  issuer.py       the only grant-appending code path
  runtime.py      validate → route → review → gates → issue → revalidate → persist
  store.py        run_execution_context.json sidecar
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

## Trusted root bootstrap

`TrustedRootIdentity(principal, main_agent, scopes, expiry, ...)` is host/application input.
`bootstrap_root_context()` creates:

```text
principal -> agent:<main_agent>
```

The LLM/Agent cannot create or change this root. `DelegationRequest` uses `extra="forbid"` and
has no `principal`, `delegator`, `run_id`, `tenant`, or `current_scopes` fields. Runtime actor
identity comes from the trusted `ToolUseContext` / stored context, never from the request.

## Hard validation

`DelegationValidator` runs before Router/Reviewer/Policy/HITL and checks:

- parent chain exists;
- first grant delegator == context principal;
- chain is contiguous;
- current actor == chain tail;
- no expired grant;
- delegatee != actor;
- delegatee not already in the ancestor path;
- non-empty, syntactically valid scopes;
- requested scopes ⊆ parent effective scopes.

Failure returns immediately; Router, Reviewer, Policy, HITL, and GrantIssuer are not called.

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

The Reviewer can never remove POLICY or HITL and cannot issue a grant. Default
`review_mode="always"`; `protected_only` and `off` are explicit non-default extensions.

## Policy and HITL gates

One delegation request produces exactly one `PolicyRequest`:

```text
action   = "delegate"
boundary = "a2a"
resource = "agent:<delegatee>"
input    = intent, requested_scopes, task_summary, current_actor
context  = trusted parent ExecutionContext
```

POLICY runs before HITL. Policy DENY is final; the human gate is never asked and cannot
override it. Missing/unconfigured/erroring policy fails closed. Missing/disabled/erroring
approval fails closed. The approval display includes principal, actor, delegatee, task, intent,
scopes, router gates/reasons, and reviewer verdict/reasons. No pending approval is persisted;
while approval is pending the child capability does not exist.

## Issuance and sub-agent seam

`GrantIssuer` is runtime-internal and is not registered as a tool. It appends exactly one:

```python
DelegationGrant(
    delegator=current_actor,
    delegatee=request.delegatee,
    scopes=request.requested_scopes,
    expires_at=min(non-null ancestor expiries),
)
```

The child context is a `model_copy` of the parent with only `delegation` replaced; principal,
subject, workload identity, tenant, classification, regions, credential references, trace id,
and attributes are preserved. The parent is frozen/immutable. The child is structurally
revalidated before the sidecar write.

`DelegationRuntime.build_child_agent_config(base, outcome)` attaches only a committed child
context to an `AgentConfig`. A denied outcome raises `DelegationDenied`; raw parent context is
never used as child authority. Sibling children each have their own appended chain and store key.

## Durability

`run_execution_context.json` is a sidecar under the run directory and M2 authority recovery
truth:

```json
{"schema_version": 1, "run_id": "...", "contexts": {"agent:planner": {...}}}
```

It stores all committed actor contexts, not conversation. Recovery validates the chain
structurally and never re-runs Router/Reviewer/Policy/HITL. Crash before grant commit leaves no
new context; crash after commit restores it exactly. This is not a second checkpoint engine and
not a durable pending-approval workflow (M6).

## M1 boundary

M2 adds a separate package and does not modify the M1 tool runtime, core/searchable split,
`tool.search`, Top-3 disclosure, dispatchable set, `dispatch_action`, MCP hidden-by-default,
provider-safe naming, or M1 recovery. Capability authority is independent of tool visibility:
`allowed_tools != dispatchable tools != delegated capabilities`.

## Verification

```bash
ruff check .
ruff format --check .
pyright
python -m pytest
```

M2 focused tests: [acceptance matrix](v2-m2-acceptance.md). M1 regression: full suite.
