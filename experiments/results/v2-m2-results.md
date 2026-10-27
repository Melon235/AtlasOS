# V2 M2 Formal Results — Delegated Capability Runtime

## Provenance

```text
implementation commit: 156479b
evaluation commit:     75516c7
git_dirty:             false
timestamp:             2026-10-27T11:07:49Z
record:                experiments/runs/v2-m2-eval.jsonl
```

Baseline: M1 final `9b0d861`. V1 remains frozen at `bd6134b`.

## Quality gates

```text
ruff check .             All checks passed
ruff format --check .    133 files already formatted
pyright                  0 errors, 0 warnings, 0 informations
pytest                   190 passed (full suite, run twice from clean tree)
M2 focused tests         39 passed
```

M2 focused test files:

```text
tests/unit/test_delegation_core.py
tests/integration/test_delegation_runtime.py
tests/integration/test_delegation_reviewer.py
tests/integration/test_m2_acceptance.py
```

## Formal measurements

| Metric | Result |
|---|---|
| Router deterministic replay rate | **1.000** (11/11 cases repeated twice) |
| Router rule coverage | **1.000** (11/11 table rules exercised) |
| Reviewer AGREE | 5 |
| Reviewer DISAGREE | 1 |
| Reviewer UNCERTAIN | 1 |
| Reviewer failure | 1 |
| Policy decisions | 4 |
| HITL escalations | 4 |
| E2E scenarios passed | **9/9** |
| Privilege-escalation violations | **0** |
| Policy-DENY overrides | **0** |
| Unauthorized grant issuances | **0** |
| Recovery context hash equality | **true** |
| M1 regression status | **pass** |

The record is serialized as `EvalRecord` in `examples/m2_delegation_eval.py`. The aggregate counts
are recorded from deterministic runtime stats; the scenario outcomes are separately asserted in
the acceptance tests.

## Scenarios

| Scenario | Result |
|---|---|
| A — AUTO research + github.read | grant issued, effective `{github.read}`, gates `{}` |
| B — HITL external_communication + email.send | approval requested, then grant |
| C — POLICY modify_data + github.write | ALLOW grants; DENY denies |
| D — POLICY + HITL | both satisfied → grant; policy deny + human approve → deny |
| E — reviewer DISAGREE on {POLICY} | final `{POLICY,HITL}`, policy preserved |
| F — hard escalation | `database.write` with only `github.read` → `scope_escalation`; zero reviewer/policy/HITL calls |
| G — recovery | same context and hash after resume; no re-review |
| H — reviewer failure | uncertainty adds HITL; approval succeeds; no gate removal |

## Scenario detail

### A — AUTO

```text
user:wang -> agent:planner (github.read, email.send)
request: agent:github_worker / research / github.read
validator: PASS
router: ()
reviewer: AGREE
policy/HITL: not called
grant: agent:planner -> agent:github_worker, scopes (github.read,)
effective scopes: {github.read}
```

### B — HITL

```text
user:wang -> agent:planner (email.send)
request: agent:email_worker / external_communication / email.send
router: (HITL,)
reviewer: AGREE
approval: approved
grant issued
```

Before approval the child context is absent from memory and
`run_execution_context.json`; the pending-approval test asserts this.

### C — POLICY

```text
request: agent:github_worker / modify_data / github.write
router: (POLICY,)
policy: one PolicyRequest, ALLOW -> grant
policy: DENY -> no grant, no HITL call
```

### D — POLICY + HITL

```text
request: agent:credential_worker / manage_credentials / credentials.read
router: (POLICY, HITL)
policy ALLOW then human APPROVE -> grant
policy DENY + human approve -> deny (human cannot override policy)
policy ALLOW + human deny -> deny
```

### E — Reviewer disagreement

```text
router baseline: (POLICY,)
reviewer: DISAGREE
final gates: (POLICY, HITL)
policy still called; human gate added; grant only after both pass
```

### F — Hard escalation

```text
parent effective: {github.read}
requested: database.write
hard validator: scope_escalation
reviewer calls: 0
policy decisions: 0
approval requests: 0
grant: none
```

### G — Recovery

```text
grant committed -> sidecar written -> new runtime from same run directory
context equality: true
snapshot hash equality: true
reviewer/policy calls on resume: 0
```

Also tested:

```text
crash BEFORE grant commit -> resumed context has no new capability
crash AFTER grant commit  -> resumed context contains the grant
```

### H — Reviewer failure

```text
reviewer raises -> UNCERTAIN
gates: {HITL}
approval: approve
grant issued; POLICY/HITL never removed
```

## M1 regression

Full M1 suite remains green. The explicit boundary test verifies:

- core native tool surface is unchanged;
- `tool.search` still discovers a searchable tool;
- `dispatch_action` still reaches the AnyCode executor;
- the native list remains unchanged after search;
- M2 capability context does not block a normal tool call.

```text
allowed_tools != dispatchable tools != delegated capabilities
```

## Caveats

- These are deterministic synthetic scenarios using static reviewer/policy/approval adapters for
  measurement, plus AnyCode structured-output reviewer tests with a fake adapter. No production
  security claim follows from this set.
- The formal reviewer counts use `StaticReviewer`/`ReviewFailure`; the real
  `AnyCodeDecisionReviewer` is covered separately for structured output, no operational tools,
  disagreement, invalid output, and timeout.
- Policy decisions are a test adapter; Atlas reuses AnyCode `PolicyEnforcer`, but no external
  policy server was deployed.
- HITL is synchronous. Durable pending-approval pause/wake is M6 and intentionally absent.
- No live LLM issue/approval path was evaluated in M2; the M2 control plane is deterministic.
- `run_execution_context.json` is local trusted-host persistence, not an authentication boundary.
- M2 does not enforce tool capabilities, tool→capability mapping, RBAC/ABAC, resource scopes,
  wildcards, credentials, or identity forwarding. Those are M3+.

## Reproduction

```bash
uv run python examples/m2_delegation_demo.py

# Formal record requires a clean worktree.
uv run python examples/m2_delegation_eval.py

python -m pytest \
  tests/unit/test_delegation_core.py \
  tests/integration/test_delegation_runtime.py \
  tests/integration/test_delegation_reviewer.py \
  tests/integration/test_m2_acceptance.py

ruff check .
ruff format --check .
pyright
python -m pytest
```
