> **HISTORICAL:** capability authorization results only; superseded by [v2-m2-workers-results.md](v2-m2-workers-results.md).

# V2 M2 Semantic Correction —Formal Results

## Provenance

```text
baseline before correction: b8d2f32
implementation commit:      bee0cee
git_dirty:                  false
timestamp:                  2026-10-27T12:16:31Z
record:                     experiments/runs/v2-m2-correction-eval.jsonl
```

Pre-correction M2 records are historical: [v2-m2-results.md](v2-m2-results.md).

## Quality gates

```text
ruff check .             All checks passed
ruff format --check .    136 files already formatted
pyright                  0 errors, 0 warnings, 0 informations
pytest                   203 passed (full suite, run twice from clean tree)
M2 focused tests         52 passed
```

The M2 focused set is:

```text
tests/unit/test_delegation_core.py
tests/integration/test_delegation_runtime.py
tests/integration/test_delegation_reviewer.py
tests/integration/test_m2_acceptance.py
tests/integration/test_central_authorization.py
```

## Formal measurements

| Metric | Result |
|---|---|
| Router deterministic replay rate | **1.000** (11/11) |
| Router rule coverage | **1.000** (11/11) |
| Reviewer AGREE | 10 |
| Reviewer DISAGREE | 1 |
| Reviewer UNCERTAIN | 1 |
| Reviewer failure | 1 |
| Policy decisions | 1 |
| HITL escalations | 9 |
| Central E2E scenarios passed | **9/9** |
| Low-privilege specialist authorization | **pass** |
| Outside-ceiling violations | **0** |
| HITL ceiling overrides | **0** |
| Policy ceiling overrides | **0** |
| Reviewer ceiling overrides | **0** |
| Implicit parent-inheritance violations | **0** |
| Explicit-delegation escalation violations | **0** |
| Per-agent authority isolation violations | **0** |
| Run-authority recovery hash equality | **true** |
| Per-agent context recovery hash equality | **true** |
| M1 regression status | **pass** |

## Scenarios asserted by the evaluator

| Scenario | Result |
|---|---|
| A —low-privilege planner + GitHubWorker | planner has only `agent.orchestrate`; worker gets `github.read` centrally |
| B —independent GitHub/Email/DB specialists | each worker has only its own scope; planner gains none |
| C —unmatched route | HITL; approve issues, deny does not |
| D —outside ceiling | hard `outside_run_authority`; no Router/Reviewer/Policy/HITL/Issuer call |
| E —explicit attenuation | worker read+write →helper read only; email escalation denied |
| F —reviewer failure | UNCERTAIN + HITL; no gate removed |
| H —reviewer disagreement | POLICY/gates preserved + HITL |
| G —recovery | run-authority hash and per-agent context hashes equal; no re-review |

## Required semantic proof

```text
Run ceiling:
{agent.orchestrate, github.read, email.send}

Planner central grant:
{agent.orchestrate}

GitHubWorker central request:
research + github.read

Result:
Planner remains {agent.orchestrate}
GitHubWorker becomes {github.read}
Central chain: user:wang -> agent:github_worker
```

The planner never owns `github.read`; no child inherits it from a spawn parent. Central grant
chains contain `principal →target`, not `principal →planner →target`.

Explicit attenuation:

```text
GitHubWorker:
user:wang -> agent:github_worker (github.read, github.write)

IssueReader:
user:wang -> agent:github_worker -> agent:issue_reader (github.read)

Explicit email.send request from GitHubWorker:
DENY scope_escalation
```

## Old-test classification

```text
UNCHANGED:            15
SEMANTICALLY UPDATED: 22
REPLACED:              2
NEW central tests:    13
```

Old M2 focused tests total 39; corrected M2 focused tests total 52.

Full per-item classification is in
[v2-m2-semantic-migration.md](../docs/stages/v2-m2-semantic-migration.md).

## Caveats

- Formal scenarios use deterministic static reviewer/policy/approval adapters. The real
  `AnyCodeDecisionReviewer` is separately tested with a fake structured-output adapter, including
  no operational tools, disagreement, invalid output, and timeout.
- No external policy server, live HITL, or live LLM authorization run was performed.
- The run ceiling is trusted host input, persisted locally. This is not an authentication system
  and does not protect against a compromised host filesystem.
- M2 still does not enforce tool→capability mapping or resource-level authorization (M3).
- Durable pending HITL pause/wake is intentionally absent (M6).
- No distributed identity database or spawn graph was introduced.

## Reproduction

```bash
uv run python examples/m2_delegation_demo.py

# Formal record requires clean worktree.
uv run python examples/m2_delegation_eval.py

python -m pytest \
  tests/integration/test_central_authorization.py \
  tests/unit/test_delegation_core.py \
  tests/integration/test_delegation_runtime.py \
  tests/integration/test_delegation_reviewer.py \
  tests/integration/test_m2_acceptance.py

ruff check .
ruff format --check .
pyright
python -m pytest
```
