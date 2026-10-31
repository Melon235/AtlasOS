# V2 M6 — Durable Approval Interrupt

Status: **PASS / FROZEN** (see [`v2-m6-results.md`](../../experiments/results/v2-m6-results.md) for
the formal clean-tree provenance).

```text
M3 = MAY THIS ACTION HAPPEN?
M6 = IF A HUMAN MUST DECIDE, CAN THIS EXACT EXECUTION POINT SURVIVE TIME AND RESTART?
M4 = HAS THIS ACTION ALREADY EXECUTED?
```

M6 adds two separate concepts:

- **ExecutionSuspension** — Atlas Runtime state that says exactly where the run stopped.
- **ApprovalChallenge / ApprovalDecision / ApprovalReceipt** — durable human-decision state.

Neither replaces M3, M4, or M5. No second Runtime, ToolRegistry, ToolExecutor, Policy Engine,
Idempotency Engine, or Workflow Engine was added.

## 1. Problem

Before M6, M3's `REQUIRE_HUMAN` path used AnyCode's current-run `ApprovalManager`. A pending
approval was not durable: a process restart lost the pause, and a later resume could not recover
the exact pending action without asking the model again.

M6 makes the pause durable before any ToolCall in the model response can enter M4. The process may
exit while the human decision is pending. Resume loads the exact pending invocation from the
suspension and revalidates current authority before executing.

## 2. Architecture

```text
LLM response ──► all ToolUseBlocks in ordinal order
                         │
                         ▼
             M3 whole-response preflight
             (evaluate only; zero claims / executions)
                         │
              ┌──────────┴──────────┐
              │                     │
     no REQUIRE_HUMAN          any REQUIRE_HUMAN
              │                     │
              ▼                     ▼
      existing M4 path      ExecutionSuspension durable
      (unchanged)                   │
                                    ▼
                            ApprovalChallenge(s)
                                    │
                              human decision
                                    │
                                    ▼
                            Runtime resume
                       M1 / M2 / M3 / M5 revalidate
                                    │
                                    ▼
                                  M4
                                    │
                              ToolExecutor
                                    │
                                    ▼
                             ToolResult batch
                                    │
                     durable settlement (results)
                                    │
                     durable model-visible continuation
                                    │
                          clear suspension
                                    │
                                next turn
```

The AnyCode seam is `AgentRunner._execute_tool_blocks`; M6 subclasses it in
`AtlasAgentRunner` and performs the barrier immediately before the upstream concurrent execution.
The `AtlasRuntime` facade exposes `active_suspensions()` and `resume_suspension()`, and normal
checkpoint resume refuses to run while an active suspension exists.

## 3. Authority separation

```text
Atlas Runtime / ExecutionSuspension
  where execution stopped, exact pending calls, continuation facts

M6 ApprovalStore
  durable human decision truth only

M3 ActionSafetyGate
  current authorization authority

M4 DurableActionBridge / ActionLedger + AnyCode idempotency
  execution durability, replay, conflict, uncertainty

M5 ProjectSandboxManager
  project/sandbox isolation and resource continuity
```

`APPROVED` does not mean "executed". M6 has no `EXECUTED` or `CONSUMED` state. The same receipt
may be revalidated repeatedly; execution dedupe belongs only to M4.

## 4. M3 preflight barrier

`ActionSafetyGate.evaluate()` performs validation, AnyCode hard security, deterministic policy,
the optional scope reviewer, and external policy. It does **not**:

- request an approval,
- create an M4 ledger record,
- claim AnyCode idempotency,
- execute a tool,
- touch a sandbox or external provider.

`RuntimeSuspensionCoordinator.preflight_batch()` evaluates every call in the response. For a
`dispatch_action` call it resolves the disclosed target and evaluates the **effective target
action** (including M1 disclosure/availability). A whole response is suspended before any call
can reach M4:

```text
IF ANY TOOL CALL IN A MODEL RESPONSE REQUIRES HUMAN APPROVAL,
NO TOOL CALL FROM THAT RESPONSE MAY ENTER M4.
```

If no call requires human, the original AnyCode concurrent execution path runs unchanged.

## 5. ExecutionSuspension

Schema version 1. Stored in a SQLite database under the Atlas run directory; the approval store is
a different database and contains no execution payload.

```text
schema_version / protocol_version / suspension_id
kind = approval
resume_point = before_tool_execution

run_id / task_id / worker_id / profile_id / agent_name
response_identity
assistant_response_digest
held_assistant_message            (exact model-visible continuation)
pending_invocations[]             (ordinal, call_id, tool ids, canonical_input,
                                   input/tool/action/decision-basis digests,
                                   execution key, preflight outcome, approval_id)
trusted_identity_digest
sandbox_binding_digest / sandbox_required
wake_reference
state / revision / created_at / updated_at
settlement
```

`canonical_input` is the validated/canonicalized exact execution payload and is protected Runtime
state. It is not reconstructed from the conversation. `held_assistant_message` is the separate
model-visible truth.

`suspension_id` is deterministic:

```text
hash(protocol_version, run_id, response_identity, assistant_response_digest)
```

`approval_id` is deterministic:

```text
hash(protocol_version, suspension_id, call_id, action_digest, decision_basis_digest)
```

## 6. Tool binding identity

Resume does not trust tool names alone. Every invocation carries a binding digest over:

```text
logical tool id
trusted source identity (built-in / Atlas control / sandbox / MCP server / host)
provider-facing name
input JSON schema
description
side-effecting flag
idempotency key field
execution implementation identity
```

A changed source, schema, description, or tool identity produces `STALE_SUSPENSION`; the old
approval cannot be reused.

## 7. ApprovalStore and reviewer identity

`SQLiteApprovalStore` supports:

```text
create_if_absent
get
list_pending / list_for_suspension
resolve(expected_version, decision, reviewer, reason)
supersede(expected_version, reviewer, reason)
```

Resolution is compare-and-swap on `(state=PENDING, version=N)`. Two reviewers racing produce
exactly one terminal decision; the loser observes `already_resolved`.

Reviewer identity comes only from a trusted host/CLI/console boundary
(`ReviewerIdentity`). No `approval.approve` or `approval.reject` tool is registered, and
agent/model namespaces are rejected by the validator. The model cannot approve itself.

The approval store keeps a redacted, trusted review projection (tool/action/resource, coarse
recipient/path/amount/classification fields, and digests). Bodies, file contents, shell strings,
and arbitrary payloads are represented by digests, never copied wholesale. The exact input stays
in protected Runtime suspension state.

## 8. Durable write order and reconciliation

No 2PC. Deterministic identities plus reconciliation:

```text
1. build suspension
2. persist suspension (Runtime state)
3. derive approval ids
4. create_if_absent approval challenges
5. mark run/task PAUSED
6. approval becomes externally visible
```

No approval is externally visible before its suspension is durable.

Crash reconciliation:

```text
suspension exists, approval missing -> derive deterministic id, create_if_absent
approval exists, suspension missing -> orphan/non-executable audit, never reconstruct execution
```

## 9. Resume validation order

`resume_execution_suspension()` takes the exact suspension and a trusted current
`ToolUseContext`:

1. schema/integrity of the suspension and held message;
2. terminal run/task state (terminal state wins over late approval);
3. M2 trusted identity digest, run/task/worker/profile;
4. M1 disclosure and tool availability (effective dispatch target included);
5. M5 sandbox continuity when the task was sandbox-bound;
6. canonical input and action digest unchanged;
7. current tool binding digest unchanged;
8. full M3 revalidation (security, policy, reviewer, external policy);
9. approval receipt binding to current `action_digest` + `decision_basis_digest`;
10. only then M4 execution/replay.

Human approval can satisfy `REQUIRE_HUMAN`; it can never override a current `DENY`. If the
current decision is `REQUIRE_HUMAN` and the receipt no longer matches the current basis, the
resume fails closed (the old pending challenge is superseded when possible).

## 10. Batch settlement and ToolResult durability

All calls in the response are held until every required human decision is terminal. Settlement
then processes calls in original ordinal order:

```text
ALLOW / approved  -> M4 execute or replay
DENY              -> synthetic denied ToolResult, no M4
REJECT            -> synthetic "Rejected by human reviewer." ToolResult, no M4
M4 uncertain      -> terminal uncertain ToolResult, no blind retry
```

The complete settlement is written to the Runtime suspension record before the suspension is
cleared. The model-visible continuation is then committed to the task conversation sidecar. The
tool result is durable before the suspension is cleared. A crash after settlement but before the
clear re-applies the continuation idempotently and clears without re-execution.

## 11. M4 replay

If M4 completed and the process died before the ToolResult was durable, resume re-enters M4 with
the same execution key. AnyCode's idempotency store returns the original result; M4 records a
replay. The provider side effect is not repeated. M4 conflicts and uncertainties remain M4
outcomes; M6 treats them as terminal results and never blind-retries.

## 12. M5 interaction

If a suspension is bound to a sandbox task, resume requires the same verified binding. A lost,
closed, or unverifiable sandbox fails closed. M6 never asks M5 to create a replacement sandbox:

```text
APPROVED + M5 LOST = FAIL CLOSED
```

## 13. Crash matrix

Focused tests cover:

```text
F0  before suspension durable                 no suspension/approval/M4
F1  suspension durable, approval missing      reconcile then resume
F2  approval PENDING, run not yet PAUSED      resume still valid
F3  crash while PAUSED                        restart loads exact state
F4  approval APPROVED before wake             scan finds it
F5  before M3 revalidation                    settlement lease recovers
F6  M3 approved before M4                     settlement lease recovers
F7  M4 claimed before tool execution          M4 in-progress/uncertain result
F8  side effect before M4 complete            M4 uncertainty preserved
F9  M4 complete before ToolResult             replay, one effect, one result
F10 ToolResult durable before clear           settlement re-applied, no re-execution
F11 clear before next LLM turn                next engine continues from continuation
F12 approval store unavailable                fail closed; reconcile later
F13 runtime store unavailable                 fail closed
F14 changed tool binding                      STALE_SUSPENSION
F15 M1 tool unavailable/hidden                fail closed
F16 M2 identity mismatch                      fail closed
F17 M5 sandbox LOST                           fail closed, no recreation
F18 run cancelled while paused                terminal state wins
F19 two runtime resumes                       CAS, one continuation
F20 two reviewers                             CAS, one terminal decision
```

## 14. Acceptance matrix

Focused files:

```text
tests/integration/test_m6_runtime_suspension.py   M6-01..M6-33, B01..B07
tests/integration/test_m6_agent_runner.py         real AnyCode agent-loop suspension/resume
tests/integration/test_m6_failure_matrix.py       F0..F13
tests/integration/test_m6_sandbox_resume.py       M6-11 / M5 interaction
tests/integration/test_m6_sqlite_process.py       P01..P04 independent processes
```

Mapping highlights:

```text
M6-01/02/03/04  test_m6_01, test_m6_03, test_m6_agent_runner
M6-05/06/07/08  current deny, changed basis, changed input, changed binding
M6-09/10/11     M1 hidden, M2 mismatch, M5 lost
M6-12/13        restart tests + cross-process P01/P04
M6-14/15/16     idempotent creation, deterministic suspension/approval ids
M6-17/18        reviewer race, resume race
M6-19/20        approval creates no M4; reject creates no M4
M6-21..25       M4 replay/conflict/uncertainty; result before clear
M6-26/27/29     wake loss, terminal late approval, no timeout auto-approve
M6-28           no approval tools / model reviewer identity rejected
M6-30/31        no raw secret in approval DB; exact input in Runtime state
M6-32           unsupported schema fails closed
M6-33           normal non-HITL path unchanged
M6-34..38       full M1-M5 regression suite
```

## 15. Formal metrics

`examples/m6_durable_approval_eval.py` records the clean-tree commit, Python/AnyCode/SQLite
versions, all counters from the task specification, and scenario booleans. Hard-zero assertions:

```text
tool_calls_entered_m4_before_required_approval = 0
llm_reexecutions_during_suspension_resume = 0
duplicate_provider_side_effects = 0
tool_results_committed_twice = 0
suspensions_cleared_before_result_commit = 0
late_approval_resumes = 0
model_self_approval_attempts = 0
m1_resume_violations = 0
m2_identity_violations = 0
m5_resume_violations = 0
unauthorized_executions = 0
timeout_auto_approvals = 0
```

`late_approval_attempts` is an observation of refused terminal-run attempts; it is not a
successful resume.

## 16. Known limitations

- M6 v1 supports `kind=approval` and `resume_point=before_tool_execution` only. No payment/job/
  dependency/scheduled/general workflow signals.
- No approval UI, Slack/email notification, RBAC/IAM, delegation, or multi-level approval chains.
- No timeout auto-approval. Pending may remain pending indefinitely; `EXPIRED != APPROVED`.
- `MODIFY` is not implemented as a durable direct mutation. Rejection with feedback is the v1
  scope cut; a modified action would require a new action identity and a new approval.
- The approval store is SQLite on the host filesystem, appropriate for single-node AtlasOS. It is
  not a distributed approval service.
- A sandbox binding that cannot be verified after restart fails closed; M6 does not add M5
  snapshot/recovery or sandbox recreation.
- M6 is opt-in: an `AtlasToolRuntime` must be constructed with both a suspension store and an
  approval store (plus M3 policy and M4 ledger). Run-times without M6 retain M3 current-run HITL
  behavior.
- The formal eval uses deterministic fake tools/providers. No claim of exactly-once execution is
  made; M4 provides durable dedupe/replay and downstream provider idempotency remains the
  provider's responsibility.

## 17. Non-goals

```text
new Runtime / workflow engine / distributed scheduler / event bus
new ToolRegistry / ToolExecutor / Policy Engine / idempotency engine
approval Web UI / Slack / email notifications
generic RBAC / IAM / organization hierarchy / approval chains
timeout auto-approve / approval delegation
automatic sandbox recreation / workflow compensation / 2PC
arbitrary Python continuation serialization / pickle
```
