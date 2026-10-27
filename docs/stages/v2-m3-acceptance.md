# V2 M3 Acceptance Matrix — Global Action Safety Gate

Status: **PASS**. Formal measurements are in
[`../../experiments/results/v2-m3-results.md`](../../experiments/results/v2-m3-results.md).
Focused implementation:
[`tests/integration/test_action_safety.py`](../../tests/integration/test_action_safety.py).

## Policy core — M3-01..M3-10

| ID | Check | Test |
|---|---|---|
| M3-01 | policy requires explicit default | `test_policy_requires_default_and_first_match_is_deterministic` |
| M3-02 | ordered first-match deterministic | same |
| M3-03 | explicit ALLOW | same |
| M3-04 | explicit DENY | same |
| M3-05 | explicit REQUIRE_HUMAN | same |
| M3-06 | unmatched defaults REQUIRE_HUMAN | same |
| M3-07 | policy load failure fails closed | `test_policy_load_failure_and_immutability` |
| M3-08 | agent cannot modify active policy | same (frozen policy, no mutation API) |
| M3-09 | unknown tool semantics never guessed | `test_unknown_tool_is_unknown_and_requires_human` |
| M3-10 | unknown tool requires human | same |

## ActionRequest — M3-11..M3-19

| ID | Check | Test |
|---|---|---|
| M3-11 | principal from trusted ExecutionContext | `test_request_identity_canonical_args_and_digest` |
| M3-12 | agent from ToolUseContext | same |
| M3-13 | model cannot spoof principal | same (raw spoof fields ignored for identity) |
| M3-14 | model cannot spoof agent | same |
| M3-15 | canonical args deterministic | same |
| M3-16 | same request yields same digest | same |
| M3-17 | changed args change digest | same |
| M3-18 | resource resolver deterministic | same |
| M3-19 | malformed input never reaches policy | `test_malformed_input_never_reaches_policy_or_execution` |

## Enforcement — M3-20..M3-29

| ID | Check | Test |
|---|---|---|
| M3-20 | native tool passes global gate | `test_atlas_native_searchable_dispatch_and_batch_are_gated` |
| M3-21 | searchable tool passes global gate | same |
| M3-22 | MCP tool passes local global gate | `test_main_agent_and_known_mcp_action_use_the_same_gate` |
| M3-23 | dispatch_action target passes global gate | `test_atlas_native_searchable_dispatch_and_batch_are_gated` |
| M3-24 | main agent passes global gate | `test_main_agent_and_known_mcp_action_use_the_same_gate` |
| M3-25 | worker passes global gate | native test and MCP test |
| M3-26 | deny never calls tool.execute | `test_deny_and_rejected_hitl_never_execute` |
| M3-27 | rejected HITL never calls tool.execute | same |
| M3-28 | no approver fails closed | same |
| M3-29 | batch actions independently authorized | native test (allowed web + denied unknown) |

## AnyCode hard security — M3-30..M3-34

| ID | Check | Test |
|---|---|---|
| M3-30 | hard tool deny cannot be overridden | `test_hard_security_precedes_policy_and_cannot_be_overridden` |
| M3-31 | workspace path denial cannot be overridden | same |
| M3-32 | shell hard deny cannot be overridden | same |
| M3-33 | human approval cannot override hard security | same (zero approval calls) |
| M3-34 | reviewer cannot override hard security | same (zero reviewer calls) |

## Scope / reviewer — M3-35..M3-42

| ID | Check | Test |
|---|---|---|
| M3-35 | aligned allow does not call Reviewer | `test_scope_reviewer_only_runs_on_policy_allow_and_can_only_tighten` |
| M3-36 | mismatch allow invokes Reviewer | same |
| M3-37 | CONSISTENT preserves original ALLOW | same |
| M3-38 | SUSPICIOUS escalates to human | same |
| M3-39 | UNCERTAIN escalates to human | same |
| M3-40 | reviewer error escalates to human | same (raising reviewer) |
| M3-41 | reviewer cannot change policy DENY | same |
| M3-42 | reviewer cannot change REQUIRE_HUMAN to ALLOW | same |

## HITL binding — M3-43..M3-48

| ID | Check | Test |
|---|---|---|
| M3-43 | approval bound to call_id | `test_approval_digest_binding_modified_input_and_stale_denial` |
| M3-44 | approval bound to digest | same |
| M3-45 | stale approval rejected | same (mismatched manager history) |
| M3-46 | modified input reruns authorization | same (two approvals/new digest) |
| M3-47 | changed input cannot reuse old approval | same |
| M3-48 | approved canonical input equals executed input | same |

## Idempotency — M3-49..M3-53

| ID | Check | Test |
|---|---|---|
| M3-49 | denied action consumes no execution claim | `test_idempotency_ordering_replay_and_conflict` |
| M3-50 | rejected HITL consumes no claim | same |
| M3-51 | approved side effect retains AnyCode idempotency | same |
| M3-52 | replay behavior remains correct | same |
| M3-53 | conflicting idempotency input remains rejected | same |

## Required E2E

| Scenario | Test |
|---|---|
| Safe research `github.issue.read` | `test_e2e_safe_research_scope_anomaly_external_and_unknown` |
| Coding scope anomaly `web.search`, reviewer CONSISTENT | same |
| External `email.send` requires human | same |
| Hard filesystem deny | `test_hard_security_precedes_policy_and_cannot_be_overridden` |
| Unknown MCP-style `vendor.magic_action` → human | same E2E / `test_unknown_tool_is_unknown_and_requires_human` |

## One-vote audit

| Veto | Result |
|---|---|
| Main Agent bypasses M3 | no; same gate for main/worker |
| native/MCP/searchable/dispatch target bypass | no; single gate around AnyCode inner executor |
| dispatch only authorizes wrapper | no; target re-enters gate before execution |
| unknown guessed safe | no; unknown action + REQUIRE_HUMAN default |
| implicit/ALLOW default | no; explicit default required, ALLOW rejected |
| reviewer overrides DENY | no; reviewer runs only on ALLOW |
| human overrides hard security | no; hard checks run before policy/HITL |
| approval not bound to call | no; call_id + digest + canonical args |
| modified input old approval | no; revalidation/reauthorization/new digest |
| policy/WorkerProfile writable by agent | no API; trusted host objects |
| second executor/registry/approval/policy engine | no; thin wrapper delegates to AnyCode |
| OPA/Rego | absent |
| durable HITL / ledger / sandbox / velocity | absent |
| old M2 capability runtime on tool path | absent; M3 path does not import/use it |

## Reproduce

```bash
python -m pytest tests/integration/test_action_safety.py
python -m pytest
```
