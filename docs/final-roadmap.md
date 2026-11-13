# Final Roadmap — Evaluation Tracks

Forward-looking only. Nothing in this document has been executed. This stage (Final-1.5) stops at
the repository baseline; Final-2 (protocol and holdout design) starts only on the project owner's
instruction.

## 0. Principles

- Three tracks, one frozen runtime: module evidence (A), integrated runtime evidence (B), delegation
  evidence (C).
- A module does not have to win on every workload. Functional correctness, measured benefit,
  overhead, applicability conditions and generalisation are reported separately.
- Negative and neutral results are published with the same weight as positive ones.
- Nothing is tuned after a formal run starts.

## 1. Track A — Complete Module Evaluation

Re-validate every retained module on the final candidate. One row per module, with these fields:

| Field | Meaning |
|---|---|
| Functional status | `PASS` / `PARTIAL` / `FAIL` / `NOT_VALIDATED` — does the mechanism do what it claims, under test |
| Benefit status | `POSITIVE` / `CONDITIONAL` / `NEUTRAL` / `NEGATIVE` / `INSUFFICIENT_EVIDENCE` |
| Baseline | the comparison the benefit is measured against (upstream default, previous stage, or no-mechanism arm) |
| Historical evidence | the frozen number and the commit/task set it came from |
| New independent workload | tasks not used to develop the mechanism |
| Primary metric | one metric per module, declared before the run |
| Overhead | tokens, latency and cost added by the mechanism |
| Known limitations | what the result does not cover |

Module list (the full set, not a subset):

1. Context plane — `TaskScopedSelector`, `StablePromptRenderer`, `ContextPilotDedupTransform`,
   context-locality placement, durable context rebuild.
2. Tool surface — registry, keyword search, semantic search, progressive disclosure, `RunToolState`,
   MCP discovery.
3. Coordination — scoped worker, dual handoff, shared budget.
4. Reliability — action gate, durable ledger, sandbox binding/reattachment, durable suspension and
   approvals, uncertain-side-effect handling, checkpoint resume.
5. Observability — trajectory integrity, input digests, metrics, verification, isolated oracle.

Test positive, negative and boundary conditions. A module that only ever runs on its own development
tasks is reported as `INSUFFICIENT_EVIDENCE`, not `POSITIVE`.

Recovery belongs here as one reliability module. The Final-2 pilot outcome (accountability after a
kill, no resumable coding run) is its starting point, not a separate programme.

## 2. Track B — Integrated Runtime Evaluation

| Arm | Definition |
|---|---|
| B0 | native AnyCode 0.9.0, unmodified |
| B1 | AtlasRuntime stable candidate |

Same model, same task set, same tool capability, same sandbox image and limits, same oracle, same
time/token budget, same repetitions.

Rules:

- Do not disable a legitimate AnyCode capability (checkpoint, idempotency, HITL hook, native tools)
  to create an advantage.
- Report oracle pass and runtime success separately. A runtime failure with a correct artifact is
  still a runtime failure; an oracle failure with a completed run is still a failure.
- Report cost, cache hit/miss, latency and delegation rate alongside success.
- If B1 is not non-inferior on cost, say so and report Atlas as a reliability feature with a
  measured cost.

## 3. Track C — Adaptive Delegation Evaluation

| Arm | Definition |
|---|---|
| C0 | always single worker |
| C1 | always multi worker (the candidate DAG runs as decomposed) |
| C2 | selective router (`route_delegation`) |

All three arms use the same AtlasRuntime core, sandbox, tools, budget and oracle. Task families:

- serial dependency tasks (no real parallelism available),
- naturally parallel tasks (independent deliverables),
- strongly coupled tasks (shared write surface, unstable interface).

Router constraints:

- The decision uses only information available **before** execution: task text, declared
  dependencies, worker steps, static path declarations.
- Oracle results, hidden tests and post-hoc failure data are never router inputs.
- The router may not be edited between arms.

Metrics: oracle pass, cost, latency, delegation rate, harmful delegation (delegated where single
would have passed), routing regret (cost/latency of the wrong choice), and measured wall-clock
parallelism.

A C2 result equal to C0 is reported as "the router chose correctly to stay single", **not** as
evidence that multi-agent routing adds value.

## 4. Holdout principle

- The historical 30-task benchmark, the E1/R0/R0.5 task families and every task that was debugged
  against a result are **development evidence**.
- Formal evaluation needs an additional, disjoint task set (or a disjoint workload of the same
  family) that has never been inspected while tuning.
- Freeze the holdout before the first formal run: runtime commit, model, tasks, tools, sandbox,
  oracle, budget, router policy.
- Do not modify a task after a formal failure. A broken task is reported as a broken task.

This stage creates no test set, no benchmark runner and no evaluation config.

## 5. Stop conditions

- If the current router shows no benefit on independent tasks: record the result, do not start a
  "Router V2", do not widen the scope.
- If a mechanism is beneficial only under specific conditions: publish the conditions, not a general
  claim.
- If a module fails functionally: report `FAIL`; do not repair it inside the evaluation stage.
- If infrastructure blocks a track (no sandbox, no budget, no provider quota): mark the track
  `BLOCKED` with the evidence, and continue the tracks that are not blocked.

## 6. Stage order

| Stage | Content | Gate |
|---|---|---|
| Final-2 | Evaluation protocol: module matrix, Track B/C specs, integration contract, metrics contract, holdout design, environment protocol | **designed** — see `docs/final-2-evaluation-design-report.md` and `docs/evaluation/`; awaiting owner approval |
| Final-3 | Minimal integration + required coverage + Track A module acceptance | **done** (`93cbd98` / `97fa2d4`) — see `docs/final-3-*-report.md`; native parity blocked, live track blocked |
| Final-4 | Integrated runtime and adaptive delegation pilot (Tracks B, C) on the validation set | **partially done** (28428e8): offline supplement + neutral adapter; live pilot NOT_RUN — no provider or sandbox credential |
| Final-5 | Frozen final evaluation on the holdout | Runtime, tasks, candidate plans, model, tools, budget, oracle and router frozen |
| Final-6 | Demo, release, packaging | Final-5 results published |

Do not start Final-5 from this stage. It requires an explicit instruction, a provider credential and a decision on native environment parity. Previous gate:
on `docs/final-2-evaluation-design-report.md` §11.

### 6.1 Frozen protocol documents

The evaluation protocol is normative and lives in `docs/evaluation/`:

- `module-evaluation-matrix.md` — 59 modules, functional + benefit checks, readiness.
- `final-evaluation-protocol.md` — Track B arms, environment equality, thresholds.
- `router-evaluation-spec.md` — Track C arms, regret, success and stop conditions.
- `runtime-integration-contract.md` — minimal wiring design, allowlist, acceptance gates.
- `metrics-contract.md` — one definition per metric and the missing-value policy.
- `holdout-design.md` — data classes, task structures, manifest schema, candidate slate.
- `environment-protocol.md` — E1/E2/E3 and fault-injection safety.

Editing these documents during a formal run invalidates it.
