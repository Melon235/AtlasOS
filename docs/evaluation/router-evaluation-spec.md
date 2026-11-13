# Router Evaluation Spec — Track C (Adaptive Delegation)

Design only. No run, no threshold change, no code change.

Track C asks one question: **given a candidate decomposition that already exists, does admitting or
rejecting it improve outcomes relative to always-single and always-multi?**

It does not ask whether the runtime can invent a decomposition (no planner exists, and D6 forbids
adding one in this round).

---

## 1. Current router semantics (verified, read-only)

| Item | Fact | Evidence |
|---|---|---|
| Input | `DelegationProposal` (candidate tasks + provenance-tagged evidence) + `CapabilityCatalog` + `RoutingPolicy` | `workers/routing.py::route_delegation` |
| Nature | pure function: no I/O, no clock, no randomness; same inputs ⇒ same `decision_digest` | module docstring + `decision_digest` |
| `DIRECT` | the runner replaces the executed plan with `a0_steps(task)` (single worker) and forces single-worker grading (`distinct_workers=1`) | `runner.py:847-859` |
| `DELEGATE_PARALLEL` | the candidate DAG runs unchanged; `executed_worker_count = len(task.workers)` | `runner.py:860-862` |
| `DELEGATE_ONE` | exists in the decision vocabulary; reachable only through the single-read-only-task gate; the runner branches on `is_direct` only, so a `DELEGATE_ONE` decision **executes as the candidate plan** — i.e. it has no distinct execution semantics today | `routing.py:722-750`, `runner.py:847` |
| Candidate plan source | the benchmark's frozen `WorkerStep` list, converted by `build_proposal` | `experiments/evaluation/stage_r0_routing.py:191` |
| Data beyond the public task | none: task text, declared `depends_on`, path declarations, static analysis; planner claims are down-ranked | `SourceKind`, `_trusted_items` |
| Oracle data | never read | no oracle import in `routing.py` |
| Default | `delegation_route_mode="off"` | `RunnerSettings` |
| App coding | not wired; nothing under `src/` imports the router | `select_*` grep in Final-1.5 §5 |
| Tests | `tests/unit/test_delegation_routing.py` (34), `tests/unit/test_stage_r05_routing.py` (19), `tests/integration/test_stage_r0_routing.py` (4) | measured |

Consequence for evaluation: a `DELEGATE_ONE` decision must be reported as such in the analysis, but it
executes like `DELEGATE_PARALLEL` with one worker. The protocol therefore scores by **executed worker
count**, not by the enum alone.

---

## 2. Arms

| Arm | Definition | Executed plan |
|---|---|---|
| **C0** | always single | `a0_steps(task)` |
| **C1** | always multi | the frozen candidate DAG, unchanged |
| **C2** | selective router | `route_delegation` decides per task; `DIRECT` ⇒ C0 plan, otherwise C1 plan |

Rules:

- C1 and C2 use the **same** candidate plan object (`candidate_plan_digest`).
- C0 receives the same public prompt, the same fixture and the same total budget. It is not given the
  decomposition, but it is not given less task information either.
- C0 may reuse Track B's R0 samples only if commit, tasks, model, budget and environment are identical;
  otherwise C0 is run inside Track C.
- No arm gets extra implementation hints, extra turns, or a larger budget.
- Router thresholds and the candidate plans are frozen before the run.

---

## 3. Candidate plan source

Externally provided, as described in `holdout-design.md` §7:

- authored from the public prompt, initial tree, declared write surface and public tests only;
- authored before any run of that task;
- no access to hidden oracle tests, no access to another arm's results;
- recorded with authoring provenance, version and digest;
- never edited after a formal failure.

`build_proposal` currently consumes `WorkerStep`-shaped data. The evaluation layer must therefore
convert the authored plan into that shape inside `experiments/` (a data adapter, not a runtime
change). This is evaluation-only code and is not part of the runtime.

---

## 4. Fair resource limits

| Limit | Rule |
|---|---|
| Total tokens per task | one run-level budget for every arm; a 3-worker arm does **not** get 3× |
| Model requests | run-level cap recorded per run |
| Per-worker turns | same cap for every worker in every arm |
| Concurrency | same max parallel workers (`RoutingPolicy.max_parallel_workers`, frozen) |
| Wall time | same timeout, and a timeout counts as a failure for all arms |
| Retries | only pre-declared infrastructure classes, at most once, recorded |

Native AnyCode has no shared budget; if a native arm is included for context it uses an external
neutral limit (a wrapper that stops the process at the same total token count), and the difference in
*mechanism* is disclosed.

---

## 5. Metrics per task (and per arm)

| Metric | Definition |
|---|---|
| `oracle_pass` | per run, per arm; primary quality metric |
| `pass_rate` | passes / runs, per arm per task |
| `overall_cost` | sum of cost per arm |
| `cost_per_pass` | arm cost / passes (null when 0 passes) |
| `wall_time` | end-to-end per run; median and max |
| `delegation_rate` | share of C2 runs where the decision was not `DIRECT` |
| `actual_multi_execution_rate` | share of runs where **executed worker count > 1** |
| `harmful_delegation` | C2 executed multi **and** (C0 passed that task while C2 failed, **or** quality equal and C2 cost > 1.5 × C0 cost) |
| `beneficial_delegation` | C2 executed multi **and** quality ≥ C0 **and** cost < C0 cost (or quality strictly better) |
| `avoided_delegation` | C2 chose `DIRECT` where C1 failed or cost more than C0 |
| `routing_regret` | quality regret = 1 when C2 fails where either C0 or C1 passes; cost regret = `cost(C2) − min(cost(C0), cost(C1))` when quality is equal |
| `both_fail` / `inconclusive` | both arms fail; or repetitions disagree ⇒ `INCONCLUSIVE` |

The 1.5× cost-harm threshold is declared now, before any data, and is not tuned afterwards.

`routing_regret` is computed **after** the run from observed outcomes. It is never a router input.

---

## 6. Success criteria (declared before the run)

Track C is a **positive** result only if all of the following hold on the holdout:

1. C2 avoids at least one harmful delegation that C1 would have executed.
2. C2 actually executes multi on ≥1 task (not 100 % `DIRECT`).
3. On at least one delegated task, `beneficial_delegation` is observed with a measurable benefit.
4. C2 `oracle_pass` is not materially worse than C0 (declared tolerance: ≤1 task).
5. The aggregate resource trade-off is explainable from the per-task table.

Otherwise the result is one of:

- **Degenerate**: C2 = 100 % `DIRECT` ⇒ "always single with extra steps"; **not** adaptive delegation.
- **Negative**: delegated tasks lose quality or cost without a compensating win.
- **Inconclusive**: repetitions disagree, or both arms fail on the tasks where delegation fired.

None of these outcomes authorises a Router V2 in this round.

---

## 7. Analysis rules

1. Report at task level; an arm-level average hides the interesting distribution.
2. Report `INCONCLUSIVE` when the two repetitions disagree; do not take the better one.
3. Never generalise from one task that "P tasks suit multi"; report the observed cell.
4. Show the router's decision object (`gates`, `reason_codes`, `decision_digest`) for every delegated
   and every refused task, so the refusals are auditable.
5. If a task fails in every arm, it is a task-quality finding, not a routing finding.

---

## 8. Stop conditions

- If the holdout shows no benefit and no harmful delegation avoided: record it, stop. No new rules, no
  new tasks, no threshold search.
- If the router admits everything (C2 ≈ C1): the policy is not discriminating; report it as a coverage
  failure of the admission logic, not as a success.
- If the router refuses everything (C2 ≈ C0): report "degenerate".
- If infrastructure blocks a cell (sandbox, quota, provider), mark the cell `BLOCKED`, keep the rest,
  and never quietly drop the task.
- One bounded policy adjustment is allowed **only** on the development/validation set, before the
  freeze; it is recorded as a new policy version with a new digest.
