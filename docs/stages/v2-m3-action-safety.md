# V2 M3 — Global Action Safety Gate

Status: **implemented and acceptance-tested**; formal measurements are recorded in
`experiments/results/v2-m3-results.md`. Baseline: M2 correction final `b30b8db`; M1 final
`9b0d861`; V1 frozen at `bd6134b`.

## Goal

Every real tool call — native, searchable, dispatch target, MCP-backed, custom, main-agent, or
worker — passes the same pre-invoke action safety boundary before AnyCode claims idempotency or
invokes the tool.

```text
PROPOSE ACTION
→ VALIDATE INPUT
→ ANYCODE HARD SECURITY
→ ACTION CATALOG / RESOURCE
→ DETERMINISTIC POLICY
→ OPTIONAL SCOPE REVIEWER
→ EXTERNAL POLICY (if configured)
→ REQUIRE_HUMAN? → digest-bound AnyCode approval
→ ANYCODE IDEMPOTENCY CLAIM
→ TOOL EXECUTE
```

The prompt can request an action. Only runtime policy can authorize execution.

## Audit of AnyCode 0.9.0

AnyCode `ToolExecutor.execute` currently performs:

```text
lookup
→ check_tool_access (generic hard allow/deny)
→ optional ApprovalManager tool_call approval (raw input, before validation/idempotency)
→ _invoke:
    → Pydantic validation
    → hard path/shell checks inside tools
    → idempotency claim
    → tool.execute
    → complete idempotency
```

There is no pre-invoke authorization hook. M3 therefore adds one thin wrapper around the existing
`ToolExecutor`:

- it does **not** execute tools;
- it does **not** register tools;
- it does **not** create a second registry, approval engine, or policy engine;
- it validates/canonicalizes the input, calls AnyCode hard security helpers, runs the gate, then
  delegates the actual execution, idempotency, and tracing to AnyCode `ToolExecutor`.

This placement is after hard tool access and before idempotency/tool execution. Path and shell
hard checks are called through AnyCode's own helpers before policy/HITL; AnyCode repeats its
hard checks inside the selected tool.

## Tool call binding

`ActionSafetyGate` is installed by `AtlasToolRuntime(action_policy=...)`. The same gate object is
the inner executor for:

```text
native tools
tool_search / tool.search
dispatch_action target execution
searchable tools
MCP tools in the same registry
custom tools
batch calls (each call re-enters the gate)
main agent and workers
```

`dispatch_action` itself is a control action (ALLOW), but its target is gated again inside
`AtlasToolRuntime.dispatch` immediately before the target tool executes. A wrapper approval alone
cannot authorize the target.

## Cedar-style request

```text
ActionRequest
  principal    ← trusted ExecutionContext.principal
  agent        ← ToolUseContext.agent.name
  action       ← trusted ActionCatalog mapping
  resource     ← deterministic resolver from validated arguments
  context      ← run/task/worker/tenant/classification/trace metadata
  tool_name
  call_id
  canonical_arguments
  digest
```

Principal and agent are never taken from tool input or model text. Unknown catalog entries become
`action = unknown`, `resource = tool:<name>`; no name-based guessing.

## ActionCatalog and resources

The catalog maps canonical tool names to coarse actions and optional expected worker scopes. It
does not own, register, or execute tools. Default mappings include:

```text
tool.search / dispatch_action → control.search / control.dispatch
file_read, grep, list_files → filesystem.read
file_write, file_edit → filesystem.write
bash → shell.execute
web.search → web.search
email.send → communication.send
github.issue.get → github.issue.read
github.issue.create → github.issue.create
database.query → database.read
database.execute → database.write
anything else → unknown
```

Resources are canonical identities only, for example:

```text
filesystem:<path>
email:<recipient>
github:<repo>[:<issue>]
database:<db>/<table>
tool:<unknown-name>
```

No OS path sandbox is reimplemented; AnyCode hard path/shell security remains authoritative.

## Deterministic policy

`ActionPolicy` has:

- mandatory explicit `default` (DENY or REQUIRE_HUMAN; ALLOW is rejected);
- ordered rules;
- first matching rule;
- decisions only ALLOW / DENY / REQUIRE_HUMAN;
- canonical serialization/replay.

Default policy is REQUIRE_HUMAN. Known safe control/read actions are explicitly ALLOWed in
`examples/policies/m3-default.json`; unmatched actions are human-gated.

Policy files/sources are trusted-host configuration. The gate receives an immutable policy object;
the agent has no runtime API to modify the active policy or `WorkerProfile` metadata.

## Scope anomaly reviewer

`ActionReviewer` is a stateless semantic task/action consistency checker. It runs only when local
policy is ALLOW and worker `operating_scope` is mismatched/unknown. It cannot run on DENY or
REQUIRE_HUMAN, cannot alter the request, and cannot issue authority.

```text
CONSISTENT → keep original ALLOW
SUSPICIOUS → REQUIRE_HUMAN
UNCERTAIN  → REQUIRE_HUMAN
error/timeout/invalid output → REQUIRE_HUMAN
```

`AnyCodeActionReviewer` uses the same AnyCode `AgentRunner` structured-output seam, an empty tool
registry, no approval/gate reference, and temperature 0. Formal tests use deterministic reviewers.

## Policy / hard security / HITL ordering

1. AnyCode generic hard tool allow/deny.
2. Input validation; malformed input never becomes a valid action.
3. AnyCode path/shell hard checks for known filesystem/shell tools.
4. Deterministic action policy.
5. Scope reviewer only for ALLOW with anomaly.
6. External `PolicyEnforcer` if configured (DENY is final; error fails closed).
7. Human approval only for REQUIRE_HUMAN or reviewer escalation.
8. AnyCode idempotency claim.
9. Actual tool execution.

AnyCode hard security cannot be overridden by reviewer, Atlas policy, or human approval.
Local DENY and external policy DENY are final and occur before HITL.

## HITL binding and modified input

Approval is requested through the existing AnyCode `ApprovalManager` and bound to:

```text
call_id
canonical digest
tool/action/resource
canonical arguments
policy verdict/matched rule
```

The gate verifies the manager history request matches the exact digest/call context; stale or
mismatched approvals fail closed. If the approver returns `modified_input`:

```text
validate modified input
→ rebuild canonical args/request/digest
→ rerun hard checks
→ rerun policy/reviewer/external policy
→ request a new approval if still required
→ execute the approved canonical input
```

The old digest/approval is never reused. There is no durable pending-approval workflow; M6 owns
pause/wake.

## Idempotency

The gate runs before AnyCode's claim. Denied and rejected-HITL calls consume no claim. Approved
side-effecting calls retain AnyCode's existing replay/conflict/in-progress semantics. No Atlas
idempotency system was added.

## Audit

`ActionAudit` records live/structured decision metadata:

```text
call_id, principal, agent, tool_name, action, resource, digest
decision, matched_rule, policy_name
reviewer_verdict, human_outcome, executed, reason_codes
```

It is in-memory and logged through existing logging. M4 owns the durable Action Ledger; M7 owns
full observability.

## M1 / M2 boundaries

M1 native surface, search/disclosure, dispatch state, MCP hidden-by-default, and provider naming
are untouched. M2 capability/authority acquisition is a separate plane and is not used by this
tool execution path. M3 does not enforce tool capabilities against M2 grants; it authorizes the
concrete tool action.

The M3 gate is opt-in at runtime construction (`action_policy=...`), preserving V1/M1/M2 behavior
when not configured. A deployment enabling M3 should install it on the run's tool runtime before
agents start.

## Explicit non-goals

No sandbox, durable HITL, Action Ledger, velocity limiter, sequence policy, OPA/Rego, external
MCP identity forwarding, resource-level policy engine, or full IAM/RBAC/ABAC. No second executor,
registry, policy engine, or approval engine.

## Verification

```bash
ruff check .
ruff format --check .
pyright
python -m pytest
```

M3 focused tests: `tests/integration/test_action_safety.py`; matrix:
[v2-m3-acceptance.md](v2-m3-acceptance.md). Old M2 safety migration:
[v2-m3-migration.md](v2-m3-migration.md).
