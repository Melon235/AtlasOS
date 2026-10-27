# V2 M2 Acceptance Matrix — Scoped Worker Construction

Status: **PASS**. Formal measurements are in
[`../../experiments/results/v2-m2-workers-results.md`](../../experiments/results/v2-m2-workers-results.md).
Tests: `tests/integration/test_scoped_workers.py` and
`tests/integration/test_worker_action_safety.py`.

## M2-01..M2-25

| ID | Check | Test |
|---|---|---|
| M2-01 | WorkerProfile frozen | `test_profile_and_assignment_are_immutable_and_reject_trusted_fields` |
| M2-02 | unknown profile rejected | same |
| M2-03 | Planner cannot override profile fields | same |
| M2-04 | WorkerAssignment extra fields rejected | same |
| M2-05 | same input produces stable rendered context | `test_renderer_is_stable_and_task_cannot_mutate_profile` |
| M2-06 | task injection cannot mutate trusted profile | same |
| M2-07 | child ExecutionContext distinct from parent | `test_child_context_is_distinct_and_preserves_only_run_metadata` |
| M2-08 | principal preserved | same |
| M2-09 | tenant preserved | same |
| M2-10 | classification preserved | same |
| M2-11 | trace metadata preserved | same |
| M2-12 | worker runtime metadata present | same |
| M2-13 | task text not duplicated into ExecutionContext | same |
| M2-14 | base tools come from trusted profile | `test_base_tools_and_scopes_come_from_trusted_profile` |
| M2-15 | invalid profile tool fails configuration | same |
| M2-16 | can_delegate=false receives no delegate tool | same |
| M2-17 | operating_scope remains coarse | same |
| M2-18 | M2 invokes no PolicyEnforcer | `test_factory_has_no_policy_gate_hitl_or_grant_semantics` |
| M2-19 | M2 invokes no ActionSafetyGate | same |
| M2-20 | M2 invokes no HITL | same |
| M2-21 | M2 creates no capability grant | same; child delegation empty in context test |
| M2-22 | general worker works | `test_general_worker_is_a_limited_fallback` |
| M2-23 | WorkerInstance persists | `test_worker_persists_and_resumes_with_identical_hash` |
| M2-24 | resume hash identical | same |
| M2-25 | parent context remains unchanged | `test_parent_context_is_unchanged_and_renderer_hash_is_stable`; context test |

## M2 × M3 integration — I01..I07

| ID | Check | Test |
|---|---|---|
| I01 | research normal action | `test_i01_research_worker_normal_github_action_passes_gate` |
| I02 | research unrelated communication | `test_i02_research_worker_external_send_requires_human` |
| I03 | coding worker | `test_i03_coding_worker_normal_file_action_passes_gate` |
| I04 | general worker same gate | `test_i04_general_worker_still_uses_same_gate` |
| I05 | main vs worker | `test_i05_main_and_worker_both_pass_the_same_gate` |
| I06 | worker recovery | `test_i06_worker_recovery_preserves_metadata_and_hash` |
| I07 | malicious worker task | `test_i07_malicious_task_cannot_change_trusted_metadata` |

## Migration classification

See [migration](v2-m2-scoped-worker-migration.md). Old capability tests were removed or moved to
M3; no capability runtime tests remain active.

## One-vote audit

| Veto | Result |
|---|---|
| Capability authorization active in M2 | no; package removed |
| RunAuthorityCeiling active state | no |
| GrantIssuer required for workers | no |
| Planner chooses tools/scope/turns/delegation | no; assignment extra=forbid |
| Planner supplies trusted context fields | no |
| LLM generates profile/safety metadata | no; immutable Python catalog |
| Profile loaded from agent-writable path | no file loader |
| Child aliases parent context | no; distinct model copy |
| Task prompt changes metadata | no; JSON untrusted section only |
| operating_scope becomes capability list | no; coarse enum |
| M2 performs HITL/action auth | no |
| M2 duplicates M3 reviewer/policy | no; M3 package untouched semantically |
| M2 creates another ToolRegistry | no |
| M2 changes M1 disclosure | no |
| Worker/GeneralWorker bypass M3 | no; tests use same gate |
| Recursive delegation by default | no; can_delegate=false, no delegate tool |
| Recovery reconstructs scope from prompt | no; stored metadata + deterministic hash |

## Reproduce

```bash
python -m pytest tests/integration/test_scoped_workers.py
python -m pytest tests/integration/test_worker_action_safety.py
python -m pytest tests/integration/test_action_safety.py
python -m pytest
```
