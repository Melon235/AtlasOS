# V2 M2 Acceptance Matrix — Semantic Correction

Status: **PASS** for the central-authorization revision. The old parent-authority matrix was
reclassified in [semantic migration](v2-m2-semantic-migration.md).

## Central authorization — A01–A25

| ID | Check | Test |
|---|---|---|
| A01 | run_authority_is_trusted_and_immutable | `tests/integration/test_central_authorization.py::test_run_authority_is_trusted_immutable_and_canonical` |
| A02 | run_authority_scopes_are_canonical | same |
| A03 | main_agent_uses_authorization_runtime | `test_main_and_child_use_same_central_runtime_without_parent_scope` |
| A04 | child_agent_uses_same_authorization_runtime | same |
| A05 | central_authorization_does_not_require_parent_scope | same; `test_explicit_delegation_requires_parent_capability_and_cannot_use_ceiling` |
| A06 | low_privilege_planner_can_request_specialist_capability | same; `test_low_privilege_planner_and_independent_specialists` |
| A07 | child_scope_must_be_within_run_authority | `test_outside_ceiling_is_hard_denied_before_all_decision_components` |
| A08 | outside_run_authority_is_hard_denied | same |
| A09 | outside_run_authority_does_not_call_router | same (route is None/zero calls) |
| A10 | outside_run_authority_does_not_call_reviewer | same (reviewer inputs empty) |
| A11 | outside_run_authority_does_not_call_policy | same (policy requests empty) |
| A12 | outside_run_authority_does_not_call_hitl | same (approval requests empty) |
| A13 | human_cannot_override_run_authority_ceiling | same |
| A14 | policy_cannot_expand_run_authority_ceiling | same (policy never called) |
| A15 | reviewer_cannot_expand_run_authority_ceiling | same; `test_central_grant_uses_principal_and_target_not_spawn_parent` (grant scopes are requested) |
| A16 | central_grant_delegator_is_principal | `test_central_grant_uses_principal_and_target_not_spawn_parent` |
| A17 | central_grant_target_is_requested_agent | same |
| A18 | central_grant_does_not_encode_spawn_parent | same |
| A19 | sibling_agent_authorities_are_independent | `test_sibling_authorities_are_independent_and_planner_does_not_gain_them` |
| A20 | planner_does_not_gain_child_capability | same |
| A21 | child_does_not_inherit_planner_capability_implicitly | same |
| A22 | central_authorization_unmatched_route_adds_hitl | `test_unmatched_route_and_reviewer_escalate_but_policy_deny_is_final` |
| A23 | reviewer_disagreement_adds_hitl | same |
| A24 | policy_deny_remains_final | same |
| A25 | required_policy_and_hitl_both_required | same |

## Explicit delegation — D01–D08

| ID | Check | Test |
|---|---|---|
| D01 | explicit_delegation_requires_parent_capability | `test_explicit_delegation_requires_parent_capability_and_cannot_use_ceiling` |
| D02 | delegated_scopes_must_be_parent_subset | same |
| D03 | explicit_delegation_chain_is_contiguous | `test_explicit_attenuation_chain_cycles_expiry_and_distinct_operation` |
| D04 | delegation_cycle_is_rejected | same |
| D05 | delegated_expiry_cannot_outlive_parent | same |
| D06 | delegation_effective_scopes_are_intersection | same; `tests/unit/test_delegation_core.py::test_effective_scopes_are_intersection` |
| D07 | explicit_delegation_does_not_use_run_ceiling_to_escalate_parent | same/subset test; central email path in the same test |
| D08 | central_authorization_and_explicit_delegation_are_distinct_operations | same (operation fields and delegators) |

## Persistence / recovery — R01–R09

| ID | Check | Test |
|---|---|---|
| R01 | run_authority_is_durable | `test_run_authority_and_per_agent_contexts_survive_resume_without_re_review` |
| R02 | run_authority_hash_matches_after_resume | same |
| R03 | per_agent_authority_is_durable | same |
| R04 | central_grant_survives_resume | `test_crash_after_central_commit_restores_exact_context`; R01 test |
| R05 | explicit_delegation_survives_resume | R01 test (includes issue_reader chain) |
| R06 | committed_grant_is_not_re_reviewed_on_resume | R01 test (reviewer/policy calls zero) |
| R07 | crash_before_commit_creates_no_capability | `test_crash_before_central_commit_creates_no_capability` |
| R08 | crash_after_commit_restores_capability | `test_crash_after_central_commit_restores_exact_context` |
| R09 | spawn_lineage_is_not_used_to_reconstruct_authority | R08 test; central chain has no planner |

## Required E2E

| Scenario | Test |
|---|---|
| Low-privilege planner → GitHubWorker | `test_low_privilege_planner_and_independent_specialists` |
| Independent GitHub/Email/DB specialists | same |
| Unknown route → HITL approve/deny | `test_unmatched_route_and_reviewer_escalate_but_policy_deny_is_final`; `test_unknown_route_and_explicit_attenuation_e2e` |
| Ceiling cannot be overridden | `test_outside_ceiling_is_hard_denied_before_all_decision_components` |
| Explicit attenuation | `test_unknown_route_and_explicit_attenuation_e2e` |
| Recovery | R01/R08 tests |

## Retained M2 pipeline tests

The deterministic Router, Reviewer, Policy/HITL, GrantIssuer internals, and M1 regression remain
covered by:

```text
tests/unit/test_delegation_core.py
tests/integration/test_delegation_runtime.py
tests/integration/test_delegation_reviewer.py
tests/integration/test_m2_acceptance.py
tests/integration/test_central_authorization.py
```

## One-vote audit

| Veto | Result |
|---|---|
| Central still requires parent effective scope | absent; central checks ceiling only |
| Planner must own specialist capabilities | absent; low-privilege E2E |
| Spawn parent treated as authority | absent; central chain is principal→target |
| Ceiling derived from agent scopes | absent; trusted `RunAuthority` |
| Agent/reviewer/policy/HITL can modify ceiling | absent; immutable model, no mutation API |
| Main agent bypasses runtime | absent; same `authorize_agent()` |
| Child uses different authorization | absent; same runtime/pipeline |
| Central grant encodes spawn parent | absent |
| Central/explicit conflated | absent; separate APIs and operations |
| Explicit exceeds parent | absent; validator + D07 |
| Global mutable capability set | absent; per-agent contexts |
| M2 enforces tool capabilities | absent; M1 full suite unchanged |
| Tool names used as capabilities | absent |
| M1 semantics changed | absent |
| Second policy/checkpoint/IAM system | absent |
| Durable pending HITL | absent |
| M3 started | absent |

## Reproduce

```bash
python -m pytest \
  tests/integration/test_central_authorization.py \
  tests/unit/test_delegation_core.py \
  tests/integration/test_delegation_runtime.py \
  tests/integration/test_delegation_reviewer.py \
  tests/integration/test_m2_acceptance.py
```
