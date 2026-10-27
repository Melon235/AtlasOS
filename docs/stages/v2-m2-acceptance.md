# V2 M2 Acceptance Matrix

Status: **PASS** (focused tests and full suite). Formal clean-tree measurement is recorded in
`experiments/results/v2-m2-results.md`.

## T01–T50

| ID | Check | Test |
|---|---|---|
| T01 | root_grant_bootstrap_is_runtime_only | `tests/unit/test_delegation_core.py::test_root_grant_bootstrap_is_runtime_only_and_request_rejects_identity` |
| T02 | agent_cannot_supply_principal_or_delegator | same test |
| T03 | chain_root_matches_principal | `test_chain_root_must_match_principal` |
| T04 | chain_is_contiguous | `test_chain_must_be_contiguous` |
| T05 | chain_tail_matches_current_agent | `test_actor_must_be_chain_tail` |
| T06 | delegation_cycle_rejected | `test_delegation_cycle_is_rejected` |
| T07 | expired_ancestor_rejected | `test_expired_ancestor_is_rejected` |
| T08 | empty_delegation_has_zero_capabilities | `test_empty_delegation_has_zero_capabilities` |
| T09 | scope_format_and_canonical_order | `test_scope_format_and_canonical_order` |
| T10 | requested_scopes_must_be_parent_subset | `test_requested_scopes_must_be_parent_subset` |
| T11 | effective_scopes_are_intersection | `test_effective_scopes_are_intersection` |
| T12 | child_expiry_cannot_outlive_ancestor | `test_child_expiry_cannot_outlive_ancestor` |
| T13 | research_read_requires_no_gate | `test_intent_routes_and_multiple_scope_union` |
| T14 | write_route_requires_policy | same |
| T15 | external_send_requires_hitl | same |
| T16 | unmatched_route_requires_hitl | same |
| T17 | multiple_scopes_union_required_gates | same |
| T18 | routing_is_deterministic | `test_routing_is_deterministic` |
| T19 | reviewer_agree_preserves_gates | `tests/integration/test_delegation_runtime.py::test_reviewer_agree_preserves_gates_disagree_and_uncertain_add_hitl` |
| T20 | reviewer_disagree_adds_hitl | same |
| T21 | reviewer_uncertain_adds_hitl | same |
| T22 | reviewer_error_adds_hitl | `test_reviewer_provider_error_adds_hitl`; `tests/integration/test_delegation_reviewer.py::test_reviewer_timeout_is_uncertain` |
| T23 | reviewer_cannot_remove_policy | `test_reviewer_cannot_remove_policy_gate` |
| T24 | reviewer_has_no_tools_or_grant_access | `test_reviewer_receives_only_reviewer_input_and_has_no_grant_access`; `test_reviewer_uses_anycode_structured_runner_without_normal_tools` |
| T25 | auto_agree_can_issue | `test_auto_agree_can_issue` |
| T26 | policy_allow_can_issue | `test_policy_allow_deny_and_human_cannot_override_deny` |
| T27 | policy_deny_blocks_issue | same |
| T28 | human_approval_cannot_override_policy_deny | same |
| T29 | policy_plus_hitl_requires_both | `test_policy_and_hitl_require_both_and_hitl_deny_blocks` |
| T30 | hitl_deny_blocks_issue | same |
| T31 | missing_required_policy_fails_closed | `test_missing_required_gate_fails_closed` |
| T32 | missing_required_approval_fails_closed | same |
| T33 | pending_approval_has_no_capability | `test_pending_approval_has_no_capability` |
| T34 | grant_issuer_is_runtime_internal | `test_issuer_is_internal_parent_immutable_and_appends_one_grant`; `test_m1_regression...` |
| T35 | parent_context_is_immutable | `test_issuer_is_internal_parent_immutable_and_appends_one_grant` |
| T36 | child_context_appends_one_grant | same |
| T37 | child_context_is_revalidated_before_commit | `test_child_context_is_revalidated_before_commit` |
| T38 | child_agent_receives_derived_context | `test_child_config_siblings_and_non_delegation_fields`; `tests/integration/test_m2_acceptance.py::test_scenario_g_recovery_and_child_config` |
| T39 | sibling_delegations_are_isolated | `test_child_config_siblings_and_non_delegation_fields` |
| T40 | non_delegation_execution_context_fields_preserved | same |
| T41 | execution_context_snapshot_is_durable | `test_durable_snapshot_resume_and_no_re_review` |
| T42 | resume_restores_identical_context | same |
| T43 | crash_before_grant_commit_has_no_new_capability | `test_crash_before_grant_commit_has_no_new_capability` |
| T44 | crash_after_grant_commit_restores_capability | `test_crash_after_grant_commit_restores_capability` |
| T45 | committed_grant_is_not_re_reviewed_on_resume | `test_durable_snapshot_resume_and_no_re_review` |
| T46 | m1_tool_search_behavior_unchanged | `tests/integration/test_m2_acceptance.py::test_m1_regression_tool_runtime_unchanged_and_no_capability_enforcement`; full M1 suite |
| T47 | m1_native_tool_surface_unchanged | same |
| T48 | m1_dispatchable_semantics_unchanged | same; full M1 suite |
| T49 | m2_does_not_enforce_tool_capabilities | same |
| T50 | final_end_to_end_delegation_scenarios | `test_scenario_a_auto` … `test_scenario_g_recovery_and_child_config` |

## One-vote rejection audit

| Veto | Status | Evidence |
|---|---|---|
| second ExecutionContext / identity model | not present | reuses AnyCode `ExecutionContext` |
| new capability field | not present | only `DelegationGrant.scopes` |
| Agent sets principal/delegator | not possible | `DelegationRequest` extra=forbid; runtime identity from `ToolUseContext` |
| Agent creates/appends grant | not possible | `GrantIssuer` is runtime-internal, not a tool |
| Agent mutates trusted context | not possible | frozen AnyCode model + only issuer copies |
| child scopes exceed parent | prevented | validator subset + issuer revalidation |
| cycles accepted | prevented | ancestor-path and chain duplicate checks |
| child outlives ancestor | prevented | `min(non-null ancestor expiry)` |
| empty delegation gets authority | prevented | effective scopes = empty; propose requires chain |
| LLM/embedding router | absent | deterministic table |
| unmatched defaults AUTO | absent | unmatched adds HITL |
| multiple scopes discard gates | absent | union |
| Reviewer removes gate / issues grant | absent | monotonic union; no runtime reference |
| Reviewer has operational tools | absent | empty registry, only `_structured_output` |
| Policy/HITL mutate context | absent | gates return booleans/decisions only |
| human overrides policy DENY | absent | policy first and final |
| missing required gate becomes AUTO | absent | fail closed |
| issuer exposed as model tool | absent | no `ToolDefinition`; M1 surface unchanged |
| child receives raw parent authority | prevented | `build_child_agent_config` accepts only committed outcome |
| global mutable capability state | absent | per-run store/context map |
| identity recovery from conversation | absent | `run_execution_context.json` only |
| durable pending approval | absent | not implemented |
| tool capability enforcement | absent | M1 tool tests unaffected |
| allowed_tools/tool names as capabilities | absent | scopes are separate strings |
| resource scopes/wildcards | absent | atomic syntax |
| new Policy Engine / Action Ledger | absent | AnyCode policy/approval only |
| M1 regressions | absent | full suite + T46–T49 |

## Invariant

```text
PROPOSE
→ VALIDATE
→ ROUTE
→ REVIEW
→ SATISFY REQUIRED GATES
→ ISSUE
→ REVALIDATE
→ PERSIST
```

Only the committed `DelegationGrant` produces delegated authority.

## Reproduce

```bash
python -m pytest tests/unit/test_delegation_core.py \
  tests/integration/test_delegation_runtime.py \
  tests/integration/test_delegation_reviewer.py \
  tests/integration/test_m2_acceptance.py
```
