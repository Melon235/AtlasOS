# AtlasOS M1–M3 Integrated Control-Plane Reliability Test

Status: formal control-plane E2E. Scope is limited to M1 + M2 + M3; no M4–M8 behavior is
implemented.

## Scenario

A deterministic checkout-service incident:

```text
checkout-service failure rate rises after v2.4
Planner → ResearchWorker → safe issue/log/code inspection
Planner → CodingWorker when analysis requires code work
No automatic external email
No access outside the workspace
```

All tools, approvals, failures, and crash points are fake/local. No network, GitHub, MCP,
email, or LLM is used.

## Fake tools

Native/core: AnyCode built-ins plus `tool_search`, `dispatch_action`.

Searchable:

```text
github.issue.read
logs.search
docs.search
code.read
code.edit
tests.run
email.send
filesystem.read_external
vendor.magic_action
```

`vendor.magic_action` deliberately has no ActionCatalog entry. `filesystem.read_external` is a
trusted catalog filesystem action with hard path prechecks.

## M1 semantics

`REGISTERED != VISIBLE != DISCLOSED != EXECUTABLE BY GUESS`.

- Searchable tools are registered but hidden from the native surface.
- A hidden target dispatch is rejected with `tool_not_disclosed` and never executes.
- `tool_search` discloses only results; disclosure is monotonic for the run.
- `dispatch_action` target re-enters M3 before execution.
- Current Atlas M1 disclosure is **run-shared**, not worker-private. The test explicitly asserts
  run-shared disclosure within one run and no sharing across a separate run/state sidecar.
- Recovery restores the run disclosure sidecar and native surface remains core-only.

## M2 semantics

Research/coding/general workers are constructed from trusted profiles. Assignment is limited to
profile/task/context; task injection cannot change profile, scope, tools, can_delegate, or
ExecutionContext. Worker contexts are distinct and preserve trusted run metadata. Recovery
rerenders deterministically and verifies the hash.

M2 does not create grants or perform action authorization.

## M3 semantics

Every fake real tool action runs through the frozen `ActionSafetyGate`:

- safe reads ALLOW and scope-aligned;
- email external communication REQUIRE_HUMAN;
- reject → no execute/no claim; approve → one execute;
- stale/mismatched approval → DENY;
- modified input → revalidate/reroute/new digest/new approval;
- outside-workspace path → AnyCode hard deny before policy/HITL;
- unknown action → REQUIRE_HUMAN, never guessed;
- no approver → fail closed;
- main and worker use the same gate;
- batch calls are authorized independently.

## Determinism

Fixed worker/task IDs are used. The test checks deterministic worker rendering/hash, M1 search
state/result stability, Action digest/resource/rule stability, and recovery hash equality.
Timestamps are not part of the assertions.

## Required security counters

```text
unauthorized_executions      = 0
M3_bypasses                  = 0
hard_deny_overrides          = 0
stale_approvals_accepted     = 0
modified_input_old_approval_reuse = 0
trusted_field_mutations      = 0
ExecutionContext_alias_violations = 0
recovery_hash_match          = true
```

## Implementation

```text
tests/integration/test_control_plane_e2e.py
examples/m1_m2_m3_control_plane_eval.py
```

The formal evaluator uses the same deterministic fixture helpers and writes metrics to an
ignored JSONL run artifact. It does not introduce production runtime code.

## Limitations

- M1 disclosure is run-shared by design; worker-private disclosure is not implemented.
- HITL is synchronous/current-run; no durable pending-approval recovery.
- No Action Ledger, sandbox, velocity limiter, OPA/Rego, or external identity forwarding.
- The test does not exercise real MCP or provider behavior.
- M3 remains opt-in at tool-runtime construction.
