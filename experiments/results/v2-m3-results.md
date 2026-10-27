# V2 M3 Formal Results —Global Action Safety Gate

## Provenance

```text
starting commit:          b30b8db (M2 correction final)
implementation commit:     c42f72b
verification commit:       697711b\nfinal docs commit:         ec4dc5e
git_dirty:                  false
timestamp:                  2026-10-27T14:30:14Z
git status:                 clean
full pytest:                217 passed (full suite, run twice)
M3 focused tests:           14 passed
```

Formal record: `experiments/runs/v2-m3-eval.jsonl` (ignored run artifact).
The final repository commit (`ec4dc5e`) includes docs/results only; code/tests are `697711b`. Formal evaluation was run against clean `697711b` before the docs commit.

## Quality gates

```text
ruff check .             All checks passed
ruff format --check .    149 files already formatted
pyright                  0 errors, 0 warnings, 0 informations
pytest                   217 passed
```

## Dependency / reuse decision

Local audit:

```text
toolcall-authz: not installed
agent-guard:    not installed
anycode-py:     0.9.0
```

`toolcall-authz` was not added as a dependency and no source was copied. Its published semantics
(explicit default, ordered first-match rules, ALLOW/DENY/REQUIRE_HUMAN, fail closed, exact-call
approval, human/LLM can only tighten) were adapted in Atlas code. No exact package version or
commit is pinned/referenced because no package source is vendored or imported. M3 does not wrap
AnyCode with synchronous `Guard.wrap`.

AnyCode 0.9.0 has no pre-invoke authorization hook; the audit is in
[`v2-m3-migration.md`](../docs/stages/v2-m3-migration.md). `ActionSafetyGate` is a thin delegating
wrapper around the existing `ToolExecutor`, not a replacement executor.

## Formal metrics

| Metric | Result |
|---|---|
| Total action requests | 9 |
| ALLOW | 1 |
| DENY | 1 |
| REQUIRE_HUMAN | 6 |
| Reviewer invocations | 1 |
| Reviewer CONSISTENT / SUSPICIOUS / UNCERTAIN / error | 0 / 1 / 0 / 0 |
| HITL approvals | 4 |
| HITL rejections | 1 |
| Unknown action count | 1 |
| Hard-security denials | 1 |
| Unauthorized executions | **0** |
| Hard-deny overrides | **0** |
| Stale approvals accepted | **0** |
| Modified-input old-approval reuse | **0** |
| Policy replay determinism | **1.000** |
| M1 regression | **pass** |
| M2 regression | **pass** |

The formal scenario set includes safe ALLOW, explicit DENY, HITL approve/reject, unknown action,
scope anomaly reviewer, hard-security deny, and modified-input reauthorization. Counts are from
`ActionAudit`; the hard-deny request is counted separately because it stops before an ActionRequest
audit.

## E2E behavior verified

```text
ResearchWorker github.issue.read
  →hard security pass
  →policy ALLOW
  →scope aligned
  →no reviewer/HITL
  →execute

CodingWorker web.search
  →policy ALLOW
  →scope mismatch
  →reviewer CONSISTENT
  →execute

ResearchWorker email.send
  →policy REQUIRE_HUMAN
  →digest-bound approval
  →execute only after approval

Outside workspace file read
  →AnyCode hard deny
  →no reviewer/HITL
  →no execute

vendor.magic_action with no catalog entry
  →UNKNOWN
  →REQUIRE_HUMAN
```

## M1/M2 regression

The full 217-test suite includes M1 tool discovery/search/dispatch/MCP and M2 central
authorization/explicit delegation/recovery. The M3 gate is disabled when `action_policy` is not
provided, preserving old runtime behavior. M3 does not import M2 grants into the action path and
does not enforce capability-to-tool mapping.

## Known limitations

- M3 is opt-in for a tool runtime; deployments must install `ActionSafetyGate` before agents start.
- Policy and WorkerProfile safety files must be provided by the trusted host outside agent-writable
  workspaces. M3 does not implement authentication or an OS sandbox.
- HITL is synchronous/current-run only; durable pending approval is not implemented.
- No durable Action Ledger (M4), no velocity/sequence policy, no OPA/Rego, no MCP identity
  forwarding, no resource-level authorization.
- Formal reviewer/policy scenarios use deterministic fakes; no live LLM reviewer or remote policy
  server was evaluated.
- The ActionCatalog is deliberately small; unknown MCP/custom tools require human approval.
- AnyCode hard security remains authoritative; M3 explicitly does not reimplement path/shell
  sandboxing.
- Historical M2 capability code remains as an isolated module for prior-stage provenance and is not
  used by the M3 action path.

## Reproduction

```powershell
cd D:\Work\AtlasOS

.venv\Scripts\ruff.exe check .
.venv\Scripts\ruff.exe format --check .
.venv\Scripts\pyright.exe
.venv\Scripts\python.exe -m pytest

.venv\Scripts\python.exe -m pytest tests\integration\test_action_safety.py

# Formal record requires clean worktree.
.venv\Scripts\python.exe examples\m3_action_safety_eval.py
```
