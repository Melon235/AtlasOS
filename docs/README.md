# Documentation Index

Three classes of document live here. They are not interchangeable.

## ACTIVE — current architecture and development constraints

Read these before changing code. They describe what exists today in this branch.

| Document | Purpose |
|---|---|
| [`project-status.md`](project-status.md) | Current baseline commit, architecture status per pillar, mechanism status, current work, closed directions |
| [`architecture.md`](architecture.md) | Layers, normal execution path, persisted state, recovery/verification paths, mechanism switchboard |
| [`anycode-delta.md`](anycode-delta.md) | What AnyCode 0.9.0 provides vs what AtlasRuntime adds (provenance basis for every claim) |
| [`runtime-entrypoints.md`](runtime-entrypoints.md) | Real call chains for the workflow, application-coding, evaluation, recovery and router paths, plus the capability matrix |
| [`design-decisions.md`](design-decisions.md) | Trade-offs, rejected designs, why defaults are what they are |
| [`limitations.md`](limitations.md) | Existing constraints; nothing here is a backlog promise |
| [`final-roadmap.md`](final-roadmap.md) | The only forward plan: Track A/B/C, holdout principle, stop conditions |
| [`evaluation/`](evaluation/) | The evaluation protocol (Final-2): module matrix, Track B/C specs, integration contract, metrics contract, holdout design, environment protocol |
| [`decisions/`](decisions/) | ADRs 0001–0004 (AnyCode base, ContextPilot backend, cache metrics, no LangGraph) |

The `evaluation/` bundle is normative for any measurement. Read
[`evaluation/module-evaluation-matrix.md`](evaluation/module-evaluation-matrix.md) before claiming a
module works, [`evaluation/metrics-contract.md`](evaluation/metrics-contract.md) before reporting a
number, and [`evaluation/environment-protocol.md`](evaluation/environment-protocol.md) before running
anything live.

## EVIDENCE — measured results

Real runs. Numbers are frozen at the commit and task set named in each document; they are not
results of the current branch unless the document says so.

| Document | Content |
|---|---|
| [`evaluation-summary.md`](evaluation-summary.md) | Consolidated evidence inventory with comparability warnings |
| [`../../experiments/results/`](../../experiments/results/) | Per-stage reports and published figures |
| [`../../experiments/evidence/`](../../experiments/evidence/) | Sanitized public aggregates only (no raw payloads) |
| [`final-consolidation-report.md`](final-consolidation-report.md) | Final-0/1 repository consolidation audit, 66 removed files, regression record |
| [`final-1-5-repository-baseline.md`](final-1-5-repository-baseline.md) | Final-1.5 baseline: Git state, Recovery-Pilot review, regression record, readiness |
| [`final-1-6-evaluation-baseline.md`](final-1-6-evaluation-baseline.md) | Final-1.6: the ported attribution fix, dependency inventory, measured regression, readiness matrix |
| [`final-2-evaluation-design-report.md`](final-2-evaluation-design-report.md) | Final-2: the evaluation design itself — coverage, integration, workloads, budgets, required changes |
| [`final-3-integration-report.md`](final-3-integration-report.md) | Final-3: what was integrated (R-4/R-5/R-6/R-7), with the real call chain and the parity blocker |
| [`final-3-module-acceptance-report.md`](final-3-module-acceptance-report.md) | Final-3: 59-module acceptance status, measured metrics, negative and blocked results |
| [\
| [final-4-module-evidence-supplement.md](final-4-module-evidence-supplement.md) | Final-4: evidence levels, D0-D3 dedup diagnosis, 64/128-tool retrieval, topology study |
| [\
| [final-4-integration-and-router-pilot.md](final-4-integration-and-router-pilot.md) | Final-4: neutral backend, frozen configs, and the honest NOT_RUN live matrix |
| [`research-history.md`](research-history.md) | Which branch/SHA holds which historical stage |

The Final-2 Recovery Pilot report (`docs/final-2-recovery-pilot.md`) lives on
`experiment/final-2-recovery-pilot` at `c5fce5cc5d3d96251968b264e8113685711ffc8e`. It is cited by
branch and SHA and deliberately not copied into this branch; no pilot experiment code entered the
stable baseline.

## ARCHIVED — historical stage specifications and old plans

Historical references, not current implementation instructions. Nothing here authorises new work.

| Path | Content |
|---|---|
| [`stages/`](stages/) | V1 Stage 0–6 and V2 M1–M7 stage notes, acceptance records, migration notes |
| `stage-*.md` (this directory) | Stage G/R report set (G1, G2a, G2b-1, G3-x, R0, R0.5, R1-x) |
| [`verification/`](verification/) | Historical system-verification write-up |

ARCHIVED documents are historical references, not current implementation instructions. Their
recorded measurements are preserved verbatim; do not edit them to match a newer result.

## Repository entry points

- `README.md` — public project overview.
- `AGENTS.md` — the harness development contract.
- `THIRD_PARTY.md` — third-party acknowledgements and licences.
