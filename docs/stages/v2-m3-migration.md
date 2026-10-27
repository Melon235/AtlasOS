# V2 M3 Safety Migration — Capability Gates → Global Action Gate

## Starting point

M2 correction final `b30b8db`; M1 final `9b0d861`; V1 frozen at `bd6134b`.

M2 solved capability acquisition (who may acquire an authority context). M3 solves a different
question: **may this concrete tool invocation execute now?** M3 does not reuse M2 capability
grants as the tool authorization source.

## Reused / rewritten / deleted

| M2/M1 element | M3 decision |
|---|---|
| M1 `ToolRegistry`, `ToolExecutor`, idempotency, tracing | reused unchanged |
| M1 `tool.search`, dispatch disclosure | reused unchanged; control actions explicitly allowed, target re-gated |
| M2 `Router` (intent × capability) | not used for tool actions; M3 has deterministic `ActionPolicy` |
| M2 `Reviewer` structured-output skeleton | pattern reused for `ActionReviewer`; no capability authority |
| M2 policy/HITL delegation gates | not reused as capability gates; M3 uses policy + existing AnyCode `ApprovalManager` at action level |
| M2 `GrantIssuer` / RunAuthority / delegation scopes | removed by the final M2 migration; no active code or M3 dependency |
| AnyCode hard security / approval / policy enforcer | reused; hard security remains highest authority |
| new Atlas executor/registry/approval engine | not created |

## Exact enforcement seam

Audited AnyCode 0.9.0 `ToolExecutor`:

```text
execute: lookup → check_tool_access → optional ApprovalManager
_invoke: Pydantic validate → hard path/shell inside tool → idempotency claim → tool.execute
```

No pre-invoke hook exists. M3 adds one thin wrapper, `ActionSafetyGate`, that:

1. performs AnyCode generic hard access;
2. validates and canonicalizes input;
3. performs AnyCode path/shell hard checks;
4. evaluates the action policy / optional reviewer / external policy;
5. obtains digest-bound HITL if required;
6. delegates to the original AnyCode `ToolExecutor` for its revalidation, hard checks,
   idempotency, tracing, and actual `tool.execute`.

The gate does not execute tools, register tools, or replace the executor.

## Old M2 capability semantics

M2's old capability runtime was removed during the final M2 migration to Scoped Worker
Construction. M3 does not enforce `github.issue.get requires github.read` or any similar
capability mapping; that was intentionally outside M2 and remains outside M3 V1.

This distinction is explicit in the code:

```text
M1: tool discovery / dispatch
M2: scoped worker construction
M3: concrete action safety
```

## Reviewer migration

The old capability reviewer's structured-output, empty-tool-registry, failure-handling pattern is
reused in `AnyCodeActionReviewer`. Its role is now task/action consistency:

```text
CONSISTENT → preserve existing ALLOW
SUSPICIOUS / UNCERTAIN / error → REQUIRE_HUMAN
```

It is invoked only for policy-ALLOW + scope anomaly and cannot change DENY/REQUIRE_HUMAN.

## Ordering migration

Old M2 ordering (validate → route → review → policy/HITL → issue) remains appropriate for
delegation. M3's ordering is explicitly:

```text
hard security
→ input validation
→ action mapping/resource
→ deterministic policy
→ scope reviewer (ALLOW anomalies only)
→ external policy (if configured)
→ HITL if required
→ idempotency claim
→ tool.execute
```

Local/external DENY is before HITL and cannot be overridden.

## Remaining historical code

Historical capability documentation remains for provenance only. The capability module itself was
removed by the final M2 migration; M3 has no dependency on `RunAuthority`, `requested_scopes`,
`GrantIssuer`, or capability accumulation.
