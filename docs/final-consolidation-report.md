# Final Consolidation Report — Final-0 / Final-1

Scope: contribution delta audit (Final-0) and architecture consolidation (Final-1).
Date: 2026-11-12.

---

## 1. Base branch and commit

| Item | Value |
|---|---|
| Base branch | `fix/r1-2h-oracle-sandbox-isolation` |
| Base commit | `facca984849c9821ec3bdc1df2eb6ee20bca757b` |
| Consolidation branch | `refactor/final-runtime-consolidation` |
| Commits on the branch | 5 (audit docs, deletions, entrypoint fix, regression test, final docs) |
| `master` touched | No |
| Force push / history rewrite | No |
| Branch deletion | No |

### Why this base

R1-2H is the last commit that contains the whole coding-runtime stack — tool runtime, action gate,
ledger, sandbox binding, suspension/approval, trajectory integrity, deliverable verification and
isolated oracle — and nothing from the commerce-benchmark track.

The B0 branch adds only business-benchmark code: 28 files, 5,285 insertions, all under
`experiments/business_benchmarks/`, `docs/stage-b0*` and two B0 tests. Nothing in it is a general
defect fix for the coding runtime, so nothing was ported.

Verified before branching:

- `git diff --stat facca98 fix/b0-r3-business-completion-contract` → 28 files, additive only.
- No file outside the B0 track was modified.

## 2. Preserved branches

All nine branches and their remote counterparts are untouched:

| Branch | HEAD |
|---|---|
| `master` | `07e18ec` (local, 22 commits ahead of `origin/master` `3fbf3dd`) |
| `feat/stage-g1-r0-prerequisites` | `7bd0965` |
| `experiment/r0-5-positive-delegation` | `2183983` |
| `analysis/r1-0-failure-attribution` | `a8dde65` |
| `fix/r1-1-worker-ownership-integrity` | `4ad5264` |
| `feat/r1-2-deliverable-verification` | `e90d53c` |
| `fix/r1-2h-oracle-sandbox-isolation` | `facca98` |
| `experiment/b0-commerce-agent-bench` | `a8ce550` |
| `fix/b0-r2-first-native-agent` | `31ef9fc` |
| `fix/b0-r3-business-completion-contract` | `25afded` |

The index is maintained in `docs/research-history.md`.

## 3. Final positioning

AtlasRuntime is a **reliable execution runtime for long-horizon agent workflows**, applied to
autonomous software engineering tasks. It is built on AnyCode 0.9.0 and contributes the
execution-control layer: authorization, durable action state, sandbox lifecycle, suspension,
evidence and isolated grading.

It is not an agent framework, a coding CLI, a serving engine or a business-automation product.

## 4. Responsibility boundary

| Layer | AtlasRuntime | AnyCode 0.9.0 |
|---|---|---|
| Application | CLI, run registry, HTTP projection, coding session composition | — |
| Orchestration | workflow compiler, scoped workers, delegation router (opt-in), handoff (opt-in) | task queue, DAG scheduler, wave execution, assignment policies |
| Execution runtime | tool runtime and disclosure, action gate, action ledger, sandbox binding, shared budget | tool registry, executor, `retry_safe` semantics, idempotency primitives |
| Reliability | suspension, approvals, durable manifest, trajectory, reconciler | turn checkpoints, run store, sweep |
| Evaluation | benchmark, oracle, sandboxed oracle, verification | `eval` scaffolding, quality-gate sensors |

Full evidence: `docs/anycode-delta.md`.

## 5. Core modules (default path)

| Module | Why it is core |
|---|---|
| `app/cli.py`, `app/service.py`, `app/web.py` | the only entry points |
| `app/coding.py` | trusted coding composition (moved out of `experiments/`) |
| `runtime.py`, `workflow.py`, `config.py` | workflow path facade, compiler, configuration contract |
| `integrations/` | AnyCode engine seam + DeepSeek provider |
| `tools/` | one registry, disclosure gate, dispatch |
| `actions/` | `ALLOW/DENY/REQUIRE_HUMAN` decision point |
| `ledger/` | durable action records and `UNCERTAIN` classification |
| `sandbox/` | binding state machine, ownership lock, reattachment, workspace tools |
| `workers/` | scoped worker construction and trusted identity |
| `budget.py` | shared run budget with pre-request admission |
| `trajectory/` | proposed ⟷ observed evidence |
| `suspension/`, `approvals/` | durable suspension and approval (needed by the coding composition) |
| `metrics/` | token, cache, latency, cost accounting |
| `durability/` | session sidecar manifest, deterministic context rebuild |
| `experiments.py` (module) | experiment identity and result index |
| `context/` | logical context + stable rendering used for task prompts |

## 6. Optional modules

| Module | Default | Config entry | Why not default |
|---|---|---|---|
| `ownership/` | off | `ownership_mode` | enforce arm 1/16 vs 7/16 oracle (R1-1) |
| `workers/routing.py` | direct | `delegation_policy_version` | never produced a live admission in a benchmark; +40 %/+269 % cost when forced |
| `handoff.py` | legacy | `handoff_mode` | no success benefit at n=6; one offload sample |
| `history_compaction.py` | off | `compaction_mode` | prompt +9.3 %, cost +43 % (G2b-1) |
| `tool_result_offload.py` | off | `tool_result_mode` | prompt +3.7 %, cost noise (G3-2) |
| `context/transform.py` dedup | identity | `context.transform.name` | 0/72 dedup hits in E2; cache miss worse at scale |
| `placement/` | bypass | `placement.policy` | forced to `bypass` in the app; no token effect |
| `verification/` | eval only | — | 0 false positives but 23/23 recall; report, not gate |
| `trusted_recovery.py` | operator harness | — | requires M5/M6 authority; CLI refuses to reattach |

## 7. Deleted files (66)

| Group | Count | Files | Reason |
|---|---|---|---|
| Empty / dead `src` packages | 5 | `src/atlasos/scheduler/` (empty), `src/atlasos/experimental/batch.py`, `experimental/tool_search_eval.py`, `experimental/__init__.py`, `src/atlasos/stress.py` | Zero importers outside tests and already-deleted V1 examples |
| V1-era examples | 19 | `stage2_ablation`, `stage3_contextpilot`, `stage4_formal`, `stage4_live`, `stage4_placement`, `stage5_recovery`, `stage6_live`, `stage6_offline`, `stage6_report`, `stage6_workloads`, `baseline_team`, `live_baseline`, `m1_surface_eval`, `m1_tool_search_eval`, `m1_m2_m3_control_plane_eval`, `m1_m4_failure_injection_eval`, `m2_scoped_worker_eval`, `m4_sqlite_process_eval`, `m6_durable_approval_eval` | Superseded by `experiments/evaluation` or by the five retained `m*` demos; none imported by tests |
| Obsolete configs | 9 | `stage2-*.yaml` (3), `stage3-*.yaml` (2), `stage4-*.yaml` (3), `stage-d-hitl.yaml` | Their only consumers were the deleted examples/rounds |
| One-off experiment drivers | 7 | `eval_local_ownership`, `eval_os_process_hitl_resume`, `eval_real_sandbox`, `eval_v2_integrated`, `eval_verified_reattachment`, `stage_d_process`, `uat_v2` | V2 measurement drivers whose reports are frozen; `uat_cli` is now `src/atlasos/app/coding.py` |
| V1 benchmarks | 2 | `benchmarks/reorder_cpu.py`, `benchmarks/_reorder_worker.py` | ContextPilot CPU reorder benchmark for a withdrawn claim |
| Stage analysis scripts | 22 | `stage_e1_analysis`, `stage_e2_analysis`, `stage_e2_cli`, `stage_e2_rounds`, `stage_e2_tasks`, `stage_e_cli`, `stage_e_evidence`, `stage_e_rounds`, `stage_g1_analysis`, `stage_g2a_analysis`, `stage_g2b1_analysis`, `stage_g2b_diagnosis`, `stage_g3_1_analysis`, `stage_g3_2_analysis`, `stage_g3_3_analysis`, `stage_g3_4a_analysis`, `stage_g3_failure_audit`, `stage_r0_analysis`, `stage_r05_rounds`, `stage_r1_1_rounds`, `stage_r1_2_rounds`, `stage_r1_2h_consistency` | One-off post-analysis of a single historical round; outputs are frozen in `experiments/results/` and `experiments/evidence/`. Kept modules are the ones imported by `runner.py` or by a test that protects a retained mechanism |
| Tests of deleted code | 2 | `tests/integration/test_tool_search_semantic.py`, `tests/unit/test_stage_r05_rounds.py` | Only exercised the deleted helpers |

Deletion rule applied: a file was removed only after (a) an AST reverse-dependency scan over
`src/`, `tests/`, `examples/`, `experiments/`, (b) a check for dynamic imports / config strings /
`__main__` entry points, and (c) a green suite afterwards. Nothing was deleted for being large or
unreferenced-by-grep alone.

## 8. Archive sources

Every deleted file is recoverable at:

| Deleted group | Branch / commit |
|---|---|
| `src/atlasos/{scheduler,experimental,stress.py}` | `facca98` (base) |
| V1 examples, V1 configs, `benchmarks/reorder_*` | `facca98` (base) |
| Stage analysis scripts | `facca98`; the earlier ones also appear from `7bd0965` onward |
| V2 drivers (`eval_*`, `uat_v2`, `stage_d_process`) | `master` (`07e18ec`) and `7bd0965` |
| `experiments/uat_cli.py` | moved to `src/atlasos/app/coding.py` (not deleted) |

## 9. Replaced implementations

| Replaced | Replacement |
|---|---|
| `experiments/uat_cli.py` subprocess invocation from the app | `src/atlasos/app/coding.py::run_coding_session` called in-process |
| `src → tests` import of `FakeSandboxProvider` | injected `provider_factory` on `CodingRunOptions` |
| `stage_r0_analysis` / per-stage reports as the failure-attribution path | `stage_r1_0_analysis.py` (kept, tested) |
| `stage_e*_rounds` | `experiments/evaluation/rounds.py` + `cli.py` (the generic pipeline) |

## 10. Dependency changes

None. `pyproject.toml` is unchanged: `anycode-py[openai]==0.9.0`, `pydantic`, `python-dotenv`,
`pyyaml`, with the optional extras `contextpilot`, `tools`, `sandbox`, `evaluation`, `app`, `dev`.

Two consequences of the deletions:

- The `tools` extra is now used only by semantic search (`tools/search.py`); the deleted
  `experimental.tool_search_eval` was its other consumer.
- `experiments/evaluation/inspect_adapter.py` remains the only `inspect_ai` usage.

## 11. Size before / after

| Metric | Before (`facca98`) | After | Delta |
|---|---|---|---|
| Tracked files | 456 | 396 | −60 |
| Tracked Python files | 297 | 240 | −57 |
| `src/` Python files / LOC | 118 / 23,116 | 114 / 23,587 | −4 files, +471 LOC |
| `tests/` Python files / LOC | 92 / 22,173 | 90 / 22,090 | −2 files |
| `experiments/` Python files / LOC | 63 / 25,423 | 31 / 12,295 | **−32 files, −51.7 % LOC** |
| `examples/` Python files / LOC | 24 / 6,160 | 5 / 1,623 | −19 files, −73.7 % LOC |
| Test cases | 905 passed | 895 passed, 1 skipped | −10 (all belonging to deleted code) |

`src/` grew because the 703-line coding composition moved in from `experiments/`; that is the point
of the consolidation, not a regression.

Line count was not an optimization target. Two files kept their size because their tests protect
retained mechanisms (`stage_r1_0_analysis.py` 1,588 lines, `stage_r05_tasks.py` 849 lines).

## 12. Regression results

Environment: Windows 11, `.venv` (Python 3.12.15), `pip install -e .` with all extras installed.

| Check | Result |
|---|---|
| `python -m pytest` | **895 passed, 1 skipped, 1 deselected**, 3 m 51 s |
| `python -m ruff check .` | All checks passed |
| `python -m ruff format --check .` | 341 files already formatted |
| `pyright` | **0 errors, 0 warnings** |
| `atlas --help` | 12 commands listed |
| `import atlasos.app.coding` | OK, `REPO_ROOT` resolves to the repo root, default config found |
| `python examples/m1_demo.py` | deterministic tool-runtime demo runs offline |
| `python -m experiments.evaluation.cli list` | 30 tasks listed |
| Source scan for credentials | no key/private-key patterns in the tracked tree |

Mechanism-level coverage retained (all green): action gate and policy, ledger and idempotency,
sandbox binding/ownership/reattachment, suspension/approval/resume, trajectory and reconciler,
shared budget, worker contracts, verification engine, oracle sandbox, R1-0 failure attribution,
R0.5 routing and task corpus.

Not exercised in this pass (needs a provider key and a live sandbox): a live coding run, a live
benchmark sample, and the environment-gated isolation test. No new real-model evaluation was run,
per scope.

## 13. Minimal run path

```
python -m pytest                                  # offline proof of behaviour
atlas run "<task>" --mode workflow                # workflow path, in-process AtlasRuntime
atlas run "<task>" --mode coding                  # trusted sandbox path, in-process coding session
atlas approvals / approve / reject / status       # durable approval surface
python -m experiments.evaluation.cli run --sample <id> --repetitions 1
```

## 14. Technical debt

1. `experiments/evaluation/runner.py` re-implements the tool-runtime composition instead of reusing
   `app/coding.py`. Unifying them would need a parameter object for ten pinning flags; deferred to
   avoid a god-object refactor in a consolidation stage.
2. `stage_*` module names remain in `experiments/evaluation/` for the five load-bearing modules
   (`stage_r05_tasks`, `stage_r05_routing`, `stage_r0_routing`, `stage_r1_1_ownership`,
   `stage_r1_1_trace`, `stage_r1_2_contracts`, `stage_r1_2_verify`, `stage_r1_2_business`,
   `stage_g3_4a_integrity`, `stage_g3_worker_contracts`). Renaming them touches dozens of tests and
   is a naming-migration task.
3. `docs/stage-*.md` and `docs/stages/**` are frozen history kept in place and indexed in
   `docs/research-history.md` rather than moved, to avoid breaking external links.
4. `atlas resume` still refuses to reattach without the operator harness; recovery remains a
   two-step operator action.
5. `RunBudgetState` is process-local; a run directory must have a single writer.
6. The coding mode requires a UAT-shaped workspace (`csv_parser.py`).
7. `experiments/evidence/stage-b0-r2/runs/` remains in the B0-R2 git history (see §15).

## 15. Raw artifact handling (Phase 5)

Audit of `fix/b0-r2-first-native-agent`:

- 239 raw run files were tracked under `experiments/evidence/stage-b0-r2/runs/`: `ledger.db`,
  `transcript.jsonl`, `run.json`, `tool_calls.json`, `mock_state/*`, `workspace/tmp/*` (pid files and
  service logs).
- A pattern scan of those blobs found **no** API keys, private keys or access tokens.
- They are nevertheless raw execution records and should not have been published.
- `fix/b0-r3-business-completion-contract` (`1f8125e`) removes them from tracking and adds ignore
  rules.

Actions taken on the candidate branch:

- `.gitignore` now ignores `_debug_run/`, `experiments/runs/` and `experiments/evidence/**/runs/`,
  covering every stage.
- The candidate branch contains **no** raw run directories (verified: 0 tracked files under
  `experiments/runs/`, 0 under any `evidence/**/runs/`).
- `_debug_run/` is ignored; it is local scratch (handoff JSON, oracle debug output), not evidence.

Not done, deliberately: no force push, no history rewrite. Removing the blobs from B0-R2 history
requires a rewrite of a branch that is still referenced from the previous stage reports; that is an
operator decision with a separate disclosure.

**Disclosure.** If `fix/b0-r2-first-native-agent` is ever published, its history exposes raw agent
transcripts, tool payloads and sandbox state from one model run. They contain no credentials.

## 16. GitHub and CI status

| Item | State |
|---|---|
| Repository | `Melon235/AtlasOS` (unchanged) |
| `master` | local `07e18ec`, 22 commits ahead of `origin/master` `3fbf3dd`; **not pushed by this stage** |
| Pull requests | PR #1 (R0.5), PR #3 (R1-0) exist historically; R1-1, R1-2A and R1-2H never had a PR |
| Consolidation branch push | not performed (no approval requested in this stage) |
| CI workflow | `.github/workflows/ci.yml`, `windows-latest`, offline: pytest → ruff check → ruff format --check → pyright |
| CI on the candidate branch | **unknown** — the branch is local; the same four commands pass locally after the change |
| Live tests in CI | never run (`live` marker deselected, `ATLASOS_RUN_LIVE_*` forced to 0) |

## 17. Proposed main experiment

### Candidate comparison

| Direction | Contribution independence | Implementation maturity | Existing evidence | Cost | Demo value |
|---|---|---|---|---|---|
| A. Reliable execution (ledger + durable suspension + recovery) | **High** — no upstream equivalent for durable suspension, ledger state or uncertain-outcome handling | High (M3/M4/M6 shipped, deterministic tests) | **Weak for live recovery** (never triggered in a real run) | Medium | **High** |
| B. Execution integrity (trajectory completeness) | **High** — 38 % gap vs the upstream result object is a project finding | High (recorder + reconciler shipped) | **Strong** (R1-0/R1-1) | **Low** (reuses existing runs) | Medium |
| C. Multi-worker resource control (shared budget) | High (upstream budget is per agent) | High | Mechanism only; no quality benefit and multi-worker loses overall | High | Medium |
| D. Verifiable delivery (verification + oracle isolation) | High | High | **Strong** (0 FP; 28/28) | **Low** (0 model tokens) | High |

### Selected direction: A, with B as a free secondary metric

Rationale: A is the only direction where AtlasRuntime has a mechanism that AnyCode lacks entirely
*and* the current evidence is deterministic-only. Closing that gap is worth more than reproducing a
result that already exists. B comes for free because the same runs produce the trace. D is already
evidenced and does not need another run.

C is rejected for this round: the multi-worker arm loses on correctness, so a cost-control result
would be measured on an arm that already fails.

### Design

| Item | Value |
|---|---|
| Question | Does the runtime convert interrupted long-horizon coding runs into completed deliverables? |
| Arms | A0 native AnyCode 0.9.0 (no Atlas mechanisms) · A1 AtlasRuntime stable · A2 AtlasRuntime + one injected interruption (SIGTERM between tool dispatch and observation), then resume |
| Task set | the existing 30-task benchmark (`atlasos-agentbench-v1`), single repetition, one failure-injection rep for A2 |
| Model | one model family, T=0, identical budgets, identical sandbox image, identical oracle |
| Sample size | 30 (A0) + 30 (A1) + 30 (A2) = 90 runs |
| Primary metric | effective completion = oracle pass after resume / tasks started |
| Secondary | trace completeness (`input_digest_coverage`, actions recorded vs actions executed), actions missing from the task result object |
| Guard metrics | tokens, cost, p50/p95 latency per run — A1 must be non-inferior to A0 within 10 % |
| Fairness rules | A0 keeps every AnyCode default reliability mechanism (checkpoint, idempotency, HITL hook); no mechanism is disabled to manufacture an advantage |
| Acceptance | A2 completion ≥ A1 − 5 pp **and** ≥ A0 + 20 pp; trace completeness ≥ 0.99 in A1/A2; cost non-inferiority holds |
| Rejection criterion | if A1 is not non-inferior on cost, the runtime is reported as a reliability feature with a measured cost, not as an improvement |

Deliverable of this stage: this design and its acceptance conditions only. No live evaluation was
run.

## 18. Demo outline (next stage, not built here)

1. `atlas run "..." --mode coding` against the UAT fixture with a live sandbox; show the event stream.
2. `atlas approvals` while a destructive action is pending; show `REQUIRE_HUMAN`, then `approve`.
3. Kill the process mid-tool-call; show `RECOVERY_REQUIRED`, run the operator harness, show the
   resumed run and the reconciled trace.
4. `python -m experiments.evaluation.cli run --sample bugfix-001-slugify` and show the verification
   findings plus the isolated oracle verdict.

Each step already exists; the demo work is packaging, not new capability.

---

## 19. Acceptance

| Question | Verdict | Basis |
|---|---|---|
| Can AtlasRuntime be explained in one sentence? | **PASS** | README §0, `docs/architecture.md` §1 |
| Is the AnyCode / AtlasRuntime split explicit? | **PASS** | `docs/anycode-delta.md` (25 capabilities, 20-row delta table, upstream-gap list) |
| Can the real main path be drawn with every core module located? | **PASS** | `docs/architecture.md` §3–§9, verified against `runtime.py`, `anycode_engine.py`, `conversation.py`, `tools/runtime.py`, `gate.py` |
| Does every core module have a stated engineering problem? | **PASS** | README §3, `docs/anycode-delta.md` §3 |
| Is unrelated business code, duplicated prototype and obsolete experiment code out of the mainline? | **PASS** | 66 files removed; no `business_benchmarks` or `commerce_agent_bench` tracked; `experiments/` LOC −51.7 % |
| Does a real coding agent still run? | **PARTIAL** | The composition (`src/atlasos/app/coding.py`) is in the runtime package, wired in-process and covered by a regression test; **no live run was executed in this stage** (needs a provider key and a running OpenSandbox), and it requires a UAT-shaped workspace |
| Can the existing benchmark and oracle be reused? | **PASS** | `experiments/evaluation/cli.py list` returns 30 tasks; oracle, sandboxed oracle and verification suites pass |
| Do the safety, recovery, trace and budget mechanisms still have test evidence? | **PASS** | 895 passed / 1 skipped; action gate, ledger, suspension, trajectory, budget suites all green |
| Is the technical narrative complete for an AI-agent engineering role? | **PARTIAL** | README + 6 docs are complete; the branch is not pushed, so there is no CI run or public link, and no recorded demo yet |
| Is one core metric selected instead of more business tasks? | **PASS** | §17 selects interruption recovery with declared acceptance and rejection criteria; no business track remains |

Remaining work after this stage: push the branch and let CI run, execute the §17 experiment, and
record the §18 demo.
