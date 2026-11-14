# Final-4 — Design-to-Execution Coverage Matrix

Audit target: `chore/final-1-5-repository-baseline`, HEAD `b555043` (= `37eba87` + the Phase-1 docs
commit). Frozen runtime SHA: `93cbd98f9407e7358e997878238520dcac15827a` (`src/` is byte-identical to
it: `git diff 93cbd98..HEAD -- src` is empty).

Purpose: answer one question per mechanism, for each of the three real entry points —

> **Design Contract → what the code implements → what actually executed.**

This is *not* a re-run of the 59-module acceptance table (`docs/final-3-module-acceptance-report.md`).
It covers only the mechanisms named by `docs/architecture-design-contract.md` §2–§4, and it
distinguishes "wired" from "triggered", because that distinction is exactly what the last pilot lost.

## 0. Vocabulary

| Status | Meaning |
|---|---|
| `IMPLEMENTED_AND_ACTIVE` | Code exists and runs in that entry point with the shipped defaults |
| `IMPLEMENTED_OPT_IN` | Code exists and runs **only** when that entry point's configuration selects it |
| `IMPLEMENTED_BUT_NOT_WIRED` | Code exists, but this entry point has no call site for it |
| `WIRED_BUT_NOT_TRIGGERED` | A call site exists and the code path is reached only under a condition that was never met in the recorded runs |
| `NOT_APPLICABLE` | The entry point has no such concept (e.g. a gate with no tools) |
| `DISABLED_BY_DESIGN` | Deliberately not constructed in that composition, with a recorded reason |
| `UNVERIFIED` | Present and wired, no live evidence at all |
| `NOT_IMPLEMENTED` | No code |

## 1. Matrix

### A. Workflow / library runtime (`atlas run --mode workflow`, `AtlasRuntime`)

| Mechanism | Status | Real evidence |
|---|---|---|
| ContextBlock / ContextRef / ContextStore | `IMPLEMENTED_AND_ACTIVE` | `app/service.py:267-274` sets `context.policy="task_scoped"`; `runtime.py:868 _configure_context_mode`; `context/task_prompt.py:176 _project` writes `task-output:<id>` blocks |
| TaskScopedSelector | `IMPLEMENTED_AND_ACTIVE` | `runtime.py::_create_prompt_builder` → `TaskScopedSelector`; selection boundary = declared direct dependencies |
| StablePromptRenderer | `IMPLEMENTED_AND_ACTIVE` | `context/task_prompt.py:139`; layout/prompt hashes recorded per task in `_render_info` |
| Identity transform | `IMPLEMENTED_AND_ACTIVE` | default `context.transform.name="identity"` |
| ContextPilot dedup transform | `IMPLEMENTED_OPT_IN` | `transform="contextpilot_dedup"` + `contextpilot` extra; measured, no cache-miss win |
| Context-locality placement | `IMPLEMENTED_OPT_IN` | `runtime.py::_place` → `placement.policy`; `bypass` in the app service (`service.py:274`) |
| Context-aware recovery | `IMPLEMENTED_AND_ACTIVE` (library API) | `runtime.py:385 resume` → `verify_compatible` → `rebuild_context_store`; equivalence asserted by `tests/integration/test_resume.py:288-313` (layout hash **and** full prompt hash equal). No CLI command reaches it |
| Tool engineering | `NOT_APPLICABLE` | `tools.mode` defaults to `disabled`; `compile_workflow` gives agents `tools=[]` |
| Scoped worker | `NOT_APPLICABLE` | upstream agent construction; `ScopedWorkerFactory` is not used |
| ActionSafetyGate | `NOT_APPLICABLE` | no tool runtime is constructed |
| Durable ledger | `NOT_APPLICABLE` | same |
| Sandbox binding | `NOT_APPLICABLE` | no sandbox |
| Approval / durable suspension | `NOT_APPLICABLE` | no gate, so no `REQUIRE_HUMAN` |
| Shared run budget | `IMPLEMENTED_BUT_NOT_WIRED` | `AtlasRuntime` never builds a `RunBudgetState` |
| Adaptive Delegation (router) | `NOT_APPLICABLE` | no `src/` module imports `workers/routing.py` |
| Trajectory | `IMPLEMENTED_OPT_IN` | `trajectory.enabled` defaults to `False` |
| Oracle / verification | `NOT_APPLICABLE` | evaluation layer only |

### B. Coding app (`atlas run --mode coding`, `app/coding.py`)

| Mechanism | Status | Real evidence |
|---|---|---|
| ContextBlock / Ref / Store | `IMPLEMENTED_OPT_IN` | `coding.py:499 _build_context_plane`, called at `coding.py:815`; returns `None` unless `config.context.policy == "task_scoped"` |
| TaskScopedSelector | `IMPLEMENTED_OPT_IN` | same call site, `TaskScopedSelector()` |
| StablePromptRenderer | `IMPLEMENTED_OPT_IN` | `StablePromptRenderer(preserve_input_order=True)` in that builder |
| Identity / dedup transform | `IMPLEMENTED_OPT_IN` | same builder |
| Context-locality placement | `IMPLEMENTED_BUT_NOT_WIRED` | one task, one worker; `placement.policy` is not consulted |
| Context-aware recovery | `NOT_IMPLEMENTED` | `coding.py:822` builds a bare `OrchestratorConfig()`, so `checkpoint=None`; no resume entry exists |
| Tool engineering | `IMPLEMENTED_AND_ACTIVE` | `AtlasToolRuntime` + 8 core tools + `tool_search`/`dispatch_action` |
| Scoped worker | `IMPLEMENTED_AND_ACTIVE` | `ScopedWorkerFactory.create`, profile `coding_worker`, `workers.json` |
| ActionSafetyGate | `IMPLEMENTED_AND_ACTIVE` | `action_policy=_coding_policy()` |
| Durable ledger | `IMPLEMENTED_AND_ACTIVE` | `SQLiteActionLedgerStore(state/ledger.db)` |
| Sandbox binding | `IMPLEMENTED_AND_ACTIVE` | `ProjectSandboxManager` + `sandbox_bindings.db` |
| Approval / durable suspension | `IMPLEMENTED_AND_ACTIVE` | `coding.py:797-798` passes both stores |
| Shared run budget | `IMPLEMENTED_OPT_IN` | `coding.py:572 _build_run_budget` from `ATLASOS_CODING_TOKEN_BUDGET` |
| Adaptive Delegation | `NOT_APPLICABLE` | single-task composition |
| Trajectory | `IMPLEMENTED_AND_ACTIVE` | always on |
| Oracle / verification | `NOT_APPLICABLE` | evaluation layer only |

### C. Evaluation runner (`python -m experiments.evaluation.cli run`, `runner.run_sample`)

| Mechanism | Status | Real evidence |
|---|---|---|
| ContextBlock / Ref / Store | `IMPLEMENTED_OPT_IN` | `runner.py:350 _context_prompt_builder`; called at `runner.py:1090`; returns `None` unless `config.context.policy == "task_scoped"` |
| TaskScopedSelector | `WIRED_BUT_NOT_TRIGGERED` | the call site exists; the three Final-4 pilot runs loaded `eval-benchmark.yaml`, which has no `context:` block, so the resolved policy was `upstream_default` and the builder was `None` |
| StablePromptRenderer | `WIRED_BUT_NOT_TRIGGERED` | same |
| Identity / dedup transform | `WIRED_BUT_NOT_TRIGGERED` | same |
| Context-locality placement | `IMPLEMENTED_BUT_NOT_WIRED` | `runner.py` constructs one worker per frozen `WorkerStep` (`runner.py:1005-1016`); no planner call |
| Context-aware recovery | `NOT_IMPLEMENTED` | `runner.py:1102` uses `OrchestratorConfig(on_progress=…)`, `checkpoint=None` |
| Tool engineering | `IMPLEMENTED_AND_ACTIVE` | `AtlasToolRuntime` (or `AllToolsAtlasRuntime` for the full-exposure ablation) |
| Scoped worker | `IMPLEMENTED_AND_ACTIVE` | `ScopedWorkerFactory`, `worker_contract_mode` controls the task turn only |
| ActionSafetyGate | `IMPLEMENTED_AND_ACTIVE` | `_coding_policy()`, default `DENY` |
| Durable ledger | `IMPLEMENTED_AND_ACTIVE` | `state/ledger.db` |
| Sandbox binding | `IMPLEMENTED_AND_ACTIVE` | `ProjectSandboxManager`, real provider required (`runner.py:792`) |
| Approval / durable suspension | `IMPLEMENTED_BUT_NOT_WIRED` | neither store is passed and `_coding_policy()` contains no `require_human` rule, so `REQUIRE_HUMAN` is unreachable |
| Shared run budget | `IMPLEMENTED_OPT_IN` | `runner.py:774` when `budget_mode="guarded"` (the pilot used it) |
| Adaptive Delegation (router) | `IMPLEMENTED_OPT_IN` | `runner.py:895-924`, `delegation_route_mode`; `off` by default. Live: `DELEGATE_PARALLEL` on `validation-parallel-R2` (`eval-ffc739e09fa6`, 3 workers), `DIRECT` on the other two cells |
| Trajectory | `IMPLEMENTED_AND_ACTIVE` | `TrajectoryRecorder` |
| Oracle | `IMPLEMENTED_AND_ACTIVE` | `evaluate_oracle_isolated`, sandboxed hidden tests |
| Deliverable verification | `IMPLEMENTED_OPT_IN` | `verification_mode="observe"`; observation only |

## 2. Two cores, one extension — summary

| Core | Implemented | Active by default somewhere | Actually executed on a real model run |
|---|---|---|---|
| **Context Engineering** — end to end through a provider request | yes | workflow path only | **yes, in evaluation** — 7 corrected runs; `eval-db81850a884c` is verified at request-content level (`REQUEST_CONTENT_VERIFIED`, dependency present once in 6 of 6 consumer requests) |
| **Reliable Execution** — gate, ledger, sandbox, suspension, budget | yes | coding + evaluation | **partially** — gate/ledger/sandbox/budget ran; suspension never triggered (no `REQUIRE_HUMAN` produced) |
| **Adaptive Delegation** — router | yes | no (evaluation-only, off by default) | **yes** — `validation-parallel-R2` (`eval-ffc739e09fa6`) took `DELEGATE_PARALLEL` and ran 3 workers; the other cells took `DIRECT` |

The Context Engineering row was the most important line in this table and is now closed for the
evaluation path (see `docs/final-4-evidence-closure-report.md` §C). The remaining rows that are still
open are placement (`IMPLEMENTED_BUT_NOT_WIRED`, approval requested) and the native neutral baseline
(`BASELINE_PARITY_BLOCKED`, no lifecycle layer).

## 3. Functional gaps found by this audit

| ID | Gap | Where |
|---|---|---|
| G-1 | A pilot manifest declared `context_policy="task_scoped + stable_deterministic + identity"` while the runner loaded a YAML with no `context:` section. Nothing in the runner compared declared vs resolved configuration, so the mismatch was silent. | `experiments/results/final4/pilot_manifest.json` vs `experiments/configs/eval-benchmark.yaml`, `runner.py:1090` |
| G-2 | The three validation tasks gave a worker one sentence (`"implement clean_rows"`), not the public requirement, the public interface contract or the public tests. Multi-worker arms therefore had strictly less information than the single-worker arm. | `final4/validation_tasks.py`, `final4/runner_entry.py::_worker_steps` |
| G-3 | `tool_error_mode="legacy"` turns any non-zero-exit tool result into `retry_safe=False`, so the runner stops the worker with `side_effect_unknown`. In the R1 run both workers died on a failed **read-only** `git` command and produced no artifact. | `sandbox/tools.py:26-35`; observed in run `r0-eval-c16d3b21a134` |
| G-4 | `placement.policy` has a consumer only in `AtlasRuntime._place`; neither coding nor evaluation consults it. The design mechanism is real but unreachable from the measured paths. | `runtime.py::_place`, `runner.py:1005` |
| G-5 | Resume-time context equivalence is proven by test, not asserted at runtime: `AtlasRuntime.resume` checks policy/renderer/transform compatibility and rebuilds the store, but does not compare the new render against the pre-crash render hash. | `durability/manifest.py::verify_compatible`, `tests/integration/test_resume.py:288` |
| G-6 | The evaluation path's `config_digest` differs between R0/R1 and R2 by construction (`delegation_route_mode` is part of `RunnerSettings.digest()`), so "same core" cannot be checked by digest equality alone. | `runner.py:214-243` |

G-1..G-3 are corrected. G-4 is analysed and left for owner approval
(`docs/final-4-placement-core-integration-approval.md`). G-5 is recorded, not fixed.

G-6, corrected: `config_digest` is a *unit identity*, not a comparability marker. Measured on the
executed runs it does **not** include `mode`, so R0 and R1 share `6e588edd24cb` while running different
topologies, and it changes across metadata-only revisions (`2a2af55` → `97a7d35`). Comparability has to
be established from `(git_commit, config_path, dataset_version, treatment fields, src SHA)`, which is
what `collect_v2` classifies on and what the report's §A/§B evidence supports. No digest was rewritten.

## 4. Revision-3 update

`BenchmarkTask.required_artifacts` was `()` in revisions 1–2, so `GATE_0` blocked every candidate with
`UNCOVERED_GOAL` and the delegation mechanism never reached gate 1. Revision 3 declares the public
deliverables; the router, its gates and its thresholds are unchanged. See
`docs/final-4-evidence-closure-report.md` §B for the digest table and the verdict-neutrality proof of
the one oracle check that the field feeds.
