# Final Evaluation Protocol — Track B (Integrated Runtime)

Design only. No run is executed by this document.

Question Track B answers: **when AtlasRuntime's stable mechanisms are composed, can it still complete
real software-engineering tasks, and what does that composition cost in model resources and time
relative to unmodified AnyCode 0.9.0?**

It does not answer "is Atlas a better agent", and it does not measure ordinary coding crash recovery
(out of scope, `final-2-evaluation-design-report.md` §11).

---

## 1. Arms

| Arm | Runtime | Composition |
|---|---|---|
| **N0** | `anycode-py==0.9.0`, `AnyCode(OrchestratorConfig())` | native agent loop, native tool executor, native 6-tool surface, no Atlas gate/ledger/context/sandbox |
| **R0** | AtlasRuntime stable commit | progressive tools + action gate + durable ledger + sandbox binding + trajectory + (optional) shared budget; single worker |

Both arms: same model, same temperature, same `max_tokens`, same task text, same fixture, same
per-task resource ceiling, same oracle, same acceptance protocol.

Differences that are **allowed** (they are the product):
tool presentation (full exposure vs progressive disclosure), isolation (host vs sandbox), the
existence of an authorization gate, the ledger, trajectory recording, and the context plane when it is
wired.

Differences that are **forbidden**: disabling an AnyCode native capability (checkpoint config,
idempotency, HITL hook, native tools) to make Atlas look better; giving one arm a smaller tool set
than the other needs; giving one arm more turns or a larger token budget.

---

## 2. Environment equality (the hard part)

Today the two arms are **not** environment-equivalent:

| | N0 (current baseline implementation) | R0 (evaluation runner) |
|---|---|---|
| Execution location | host copy of the fixture (`tempfile` workspace) | OpenSandbox container, `network="none"` |
| Shell | Windows `cmd` (`dir`, `type`) | POSIX shell in `/workspace/project` |
| Tool surface | 6 built-ins, full exposure | 8 native (`+ tool_search`, `dispatch_action`) |
| Isolation mechanism | none | container + binding + probes |

Three options, in preference order:

**Option A (recommended): a thin neutral environment adapter.** `experiments/evaluation/neutral_env.py`
(design only) provides the six native tools *executed inside the same sandbox image* through
`SandboxProvider`, plus workspace materialisation from the same fixture digest. It injects no gate, no
ledger, no trajectory, no context plane, no budget — only *where* the tool runs and *which* fixture it
starts from. N0 would then be `AnyCode(OrchestratorConfig())` with those six tool implementations.

Constraint: if binding the native tools to the sandbox requires modifying AnyCode behaviour (rather
than substituting tool implementations), Option A is rejected — the point is to keep the loop
untouched.

**Option B: run both arms on the host.** Rejected: it deletes the isolation the Atlas arm contributes,
so the comparison would no longer describe the product.

**Option C (fallback): keep the current asymmetry and report it.** Then every Track B table must carry
the caption "arms differ in execution location and shell", and no claim may attribute a difference to
a runtime mechanism when the environment also differs. Latency and setup cost are explicitly
**not comparable** under Option C.

Decision is the owner's; this protocol is executable under A or C, and only A supports a cost claim.

---

## 3. Frozen composition (R0)

| Setting | Value | Notes |
|---|---|---|
| `context.policy` / `renderer` / `transform` | `upstream_default` for the primary matrix; `task_scoped` + `stable_deterministic` + `identity` only in the integration sub-study | the plane is not wired into the coding path today (`runtime-integration-contract.md`) |
| tool mode | progressive, `keyword` backend | semantic backend is a separate module study (A2-04) |
| worker count | 1 | multi-worker is Track C |
| `handoff_mode` | `legacy` | dual is measured in Track A only |
| `budget_mode` | `guarded`, `token_budget` fixed per task | must be identical for every arm that has a budget |
| gate policy | `_coding_policy()` (the shipped policy) | no per-task policy tuning |
| ledger | SQLite, per-run directory | |
| sandbox | OpenSandbox 1.2.0, image digest frozen, `network="none"` | |
| suspension / approval | stores configured (suspension possible) | no artificial suspension in the normal matrix |
| trajectory | on | |
| verification | `observe` | never blocks |
| oracle | `sandbox` mode | |
| router | `off` | Track C only |

A module that is not wired in the coding path is **not** marked ACTIVE in any table. `NOT_APPLICABLE`
is used for single-worker-only optimisations.

---

## 4. Metrics

Primary: `oracle_pass` rate per arm (`metrics-contract.md` §2).

Secondary: `task_quality` components, `total_cost`, `end_to_end_wall_time_ms` (with per-phase split),
`prompt_tokens`, `cache_miss_tokens`, `completion_tokens`, `model_requests`, `tool_calls`,
`resource_violations`, `execution_integrity` (pairing, digest coverage, ledger consistency),
`cleanup_outcome`, `runtime_status`, `failure_class`.

### 4.1 Pre-declared acceptance (must be confirmed by the owner before the run)

| Criterion | Threshold | Rationale |
|---|---|---|
| Quality non-inferiority | R0 oracle passes ≥ N0 − 1 task over the 12-task matrix | one task of slack for a mechanism that does not target quality |
| Cost ceiling | R0 `total_cost` ≤ 2.0 × N0 | the control plane is expected to add prompt tokens and turns, not to be free |
| Latency | reported, not targeted | setup dominates and is environment-dependent |
| Reliability floor | 0 unauthorised side effects; 0 host mutations | absolute |
| Attribution | every failed run has a non-null `failure_class` | absolute |

If a threshold is missed, the result is reported as-is; the thresholds are not revised after seeing
data.

---

## 5. Scale

| Phase | Matrix | Runs |
|---|---|---|
| Pilot (validation set) | 3 tasks (S/P/C) × 1 rep × {N0, R0} | 6 |
| Formal (holdout) | 6 tasks × 2 reps × {N0, R0} | 24 |

Track C adds 24 more (see `router-evaluation-spec.md`). Total formal: 48 task runs (+ pilot). Track A
module studies are separate and mostly model-free.

Small-sample rule: 6 tasks cannot support a statistical equivalence claim. The report must say
"descriptive, n=6 per arm, no equivalence inferred".

---

## 6. Reliability sub-study (deterministic, pre-defined)

Normal completion cannot demonstrate reliability, so these scenarios are scripted and counted
separately. They use a scripted adapter and/or a fake sandbox; no model is involved.

| Scenario | Expected observation |
|---|---|
| Forbidden tool request (policy deny) | decision DENY, ledger row `DENIED`, no execution |
| `REQUIRE_HUMAN` on a 3-call response | suspension created before any of the three executes |
| Argument changed between approval and resume | revalidation refuses, no execution |
| Replay of a completed idempotency key | replay returns the recorded result, no second effect |
| Kill after execute, before complete | `UNCERTAIN` row, no automatic retry, operator decision recorded |
| Sandbox binding conflict | second binding refused, first preserved |
| Budget exhaustion | run stops with `stop_reason=budget_exceeded`, `safe_stop=true` |
| Truncated trajectory (dropped tail) | reconciler reports a gap, does not invent events |

Each scenario is `PASS` only if the documented state transition is observed in the ledger/trajectory.
"Safe stop" is never counted as task completion.

---

## 7. Procedure

1. Freeze runtime commit, environment digest, model, tasks, oracle, budgets, thresholds.
2. Materialise a fresh workspace per run from the fixture digest (never reuse a mutated one).
3. Run N0 and R0 in interleaved order to spread provider-cache and time-of-day effects; record the
   order.
4. Capture `result.json`, `inference.jsonl`, `provider_calls.jsonl`, `trajectory/`, `ledger.db`,
   oracle output.
5. Classify every run: success / model failure / infrastructure failure / cleanup failure. Retry only
   pre-declared infrastructure classes, at most once, and record both attempts.
6. Report per-arm tables with n, denominators and the gaps.

---

## 8. Reporting rules

- No number from the historical 30-task benchmark may be presented as a Track B result.
- Every table states: commit, dataset digest, model (requested and resolved), oracle mode,
  environment option (A/B/C), n.
- If Option C is used, latency and setup cost are marked `NOT_COMPARABLE`.
- Failures are listed, not summarised away; a task that fails in one arm and passes in the other is
  always shown by id.
