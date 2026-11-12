# Final-1.5 — Repository Baseline, Harness Contract and Evaluation Readiness

Date: 2026-11-12. Branch: `chore/final-1-5-repository-baseline`. Worktree: `D:\Work\AtlasRuntime`.
`BASE_SHA` = `16251d05c34cff41d280396a1647214914e51a31` (`refactor/final-runtime-consolidation`).

This stage changed documentation, the harness contract and one package description. It changed no
runtime behaviour, no test, no experiment, no configuration and no CI.

## 1. Executive Summary

Completed: read-only Git and Recovery-Pilot audit; a clean worktree on the consolidated candidate; a
new `AGENTS.md`; a document index and three new active documents (`project-status.md`,
`runtime-entrypoints.md`, `final-roadmap.md`); a rebuilt public `README.md`; seven confirmed
documentation-fact corrections; a fresh Python 3.12 environment with an offline regression record.

Scope compliance: `src/`, `tests/`, `experiments/`, `examples/`, `configs/` and `.github/` are
byte-identical to `BASE_SHA`. No file was deleted, no test was edited, no dependency changed, no
branch was merged, nothing was pushed.

Core behaviour changed: **no**. `pyproject.toml` changed only its `description` string.

Blockers: none for the baseline itself. Two findings block parts of the future evaluation and are
recorded, not fixed — (a) no coding composition writes a checkpoint, so no coding run can be resumed;
(b) the selective router cannot improve a decomposition and has no positive live admission.

## 2. Repository Identity

| Item | Value |
|---|---|
| Project | AtlasRuntime — *a context-aware and reliable agent execution runtime built on AnyCode* |
| GitHub | `https://github.com/Melon235/AtlasRuntime` (renamed from `AtlasOS`; default branch `master`) |
| `origin` | `https://github.com/Melon235/AtlasRuntime.git` (fetch and push verified) |
| Python distribution | `atlasos` (frozen) |
| Python import package | `atlasos` (frozen) |
| CLI | `atlas` → `atlasos.app.cli:app` |
| Environment variables | `ATLASOS_*` (frozen) |
| Runtime base | `anycode-py[openai]==0.9.0` (installed and verified: `0.9.0`) |
| Original workspace | `D:\Work\AtlasOS` (primary research worktree, branch `experiment/final-2-recovery-pilot`) |
| New worktree | `D:\Work\AtlasRuntime` (this branch) |

## 3. Git State

| Item | Value |
|---|---|
| `CONSOLIDATED_SHA` (Final-0/1) | `16251d05c34cff41d280396a1647214914e51a31` — 8 commits, base `facca984849c9821ec3bdc1df2eb6ee20bca757b` |
| `facca98` ancestor of `CONSOLIDATED_SHA` | yes (`git merge-base --is-ancestor` exit 0) |
| `PILOT_SHA` | `c5fce5cc5d3d96251968b264e8113685711ffc8e` |
| Pilot base | `16251d0` (`git merge-base` = `CONSOLIDATED_SHA`; 3 commits ahead) |
| Pilot working tree at audit time | clean (0 untracked, 0 modified) |
| `BASE_SHA` (this stage) | `16251d05c34cff41d280396a1647214914e51a31` |
| Current branch | `chore/final-1-5-repository-baseline` (created from `BASE_SHA`; local only) |
| Local `master` | `07e18ec` — 22 commits ahead of `origin/master`, not modified by this stage |
| Remote `master` | `3fbf3ddb7867b45c1cab40e8ff094c7d94d795af` (default branch confirmed by `ls-remote --symref`) |
| Remote branches | `analysis/r1-0-failure-attribution`, `experiment/b0-commerce-agent-bench`, `experiment/r0-5-positive-delegation`, `feat/r1-2-deliverable-verification`, `feat/stage-g1-r0-prerequisites`, `fix/b0-r2-first-native-agent`, `fix/b0-r3-business-completion-contract`, `fix/r1-1-worker-ownership-integrity`, `fix/r1-2h-oracle-sandbox-isolation` + `master` |
| Local-only branches | `refactor/final-runtime-consolidation`, `experiment/final-2-recovery-pilot`, `chore/final-1-5-repository-baseline` |
| Worktrees | `D:\Work\AtlasOS` (pilot), `D:\Work\AtlasRuntime` (this stage) |
| Pushed / merged / rewritten by this stage | none |

The commit graph is linear from `facca98` → consolidation (8 commits) → pilot (3 commits). No merge
commit exists; nothing was rebased or force-pushed.

## 4. Recovery Pilot Review

Pilot scope: 3 commits, 13 touched files, +1 071 / −50 lines. No new runtime module, planner,
scheduler, tool executor or business adapter — no P6 and no P7 change was found.

| Change | Classification | Evidence | Recommendation |
|---|---|---|---|
| `18e523c` — `src/atlasos/app/coding.py` (`_classify_agent_outcome`, +49 lines) | P1 GENERAL_RUNTIME_FIX | S1-A run: `status=failed`, `agent_success=False`, `failure_class=null` while the exported deliverable passed 10 local tests. The fix adds `MODEL_UNFINISHED` and records `stop_reason`, `terminal_phase`, `model_turns`, `prompt_tokens`, `completion_tokens` | NEEDS_OWNER_REVIEW |
| `18e523c` — `tests/unit/test_coding_attribution.py` (3 cases) | P1 (regression test for the above) | Added in the same commit | NEEDS_OWNER_REVIEW |
| `17a19ba` — `experiments/final2/fault_injection.py` (308 lines) + `tests/unit/test_final2_fault_injection.py` | P2 EVALUATION_INFRASTRUCTURE | Kill-at-first-durable-effect harness: never edits a prompt, task or oracle; kills with `taskkill /T /F`; records the boundary it used in `fault.json` | CANDIDATE_FOR_LATER_PORT |
| `17a19ba` — `experiments/final2/probe.py`, `experiments/final2/ledger_dump.py` | P2 EVALUATION_INFRASTRUCTURE | Read-only post-mortem readers (SQLite row counts, ledger rows, checkpoint presence, transcript sizes); no mutation | CANDIDATE_FOR_LATER_PORT |
| `c5fce5c` — `docs/final-2-recovery-pilot.md` (361 lines) | P4 DOCUMENTATION | Full pilot report: frozen candidate, S1/S2 smoke, 3-composition comparison, native capability audit, 4-arm kill experiment, fairness audit, limitations | KEEP_AS_HISTORICAL_EVIDENCE |
| `c5fce5c` — wording corrections in `README.md`, `docs/limitations.md`, `docs/evaluation-summary.md`, `docs/anycode-delta.md`, `docs/final-consolidation-report.md` | P4 DOCUMENTATION | "survivable" → "accountable"; barrier ≠ atomicity; verification bounded by detection not recall; recovery evidence split into deterministic vs real-model columns | KEEP_AS_HISTORICAL_EVIDENCE (the same factual corrections are re-applied to this branch's documents by §11) |
| `experiments/runs/final2/{atlas,native,native-normal}` and `experiments/runs/app-*` | P5 GENERATED_ARTIFACT | Raw run directories; ignored by `.gitignore:22` (`experiments/runs/`); 0 tracked files; stay in `D:\Work\AtlasOS` | KEEP_AS_HISTORICAL_EVIDENCE |

What this means:

- One general fix exists and is *not* ported. It changes an application-path result field
  (`failure_class`), which is why it needs an owner decision rather than an automatic merge.
- The harness and the post-mortem readers are reusable and contain no pilot-specific task text.
- The pilot's documents are evidence. They stay on the pilot branch and are cited by branch + SHA
  (see §9), so the stable branch does not carry a report describing code it does not contain.
- Nothing in `src/` other than `app/coding.py` was touched by the pilot.

## 5. Harness Development Baseline

Problems in the previous `AGENTS.md` (272 lines): it described a superseded mission (a cache-aware
context-runtime research programme with V1 contributions), carried stage-specific instructions that
no longer authorise anything, pointed at `docs/architecture.md` as the only architecture reference,
and kept no rule that a historical stage document is not a work order. Its safety rules (reuse
before reimplementation, cache is not source-of-truth, stable prefix before dynamic context, baselines
before optimised policies, ablation, metrics contract, licence discipline) were still valid and were
kept.

New `AGENTS.md` (~150 lines): Project Identity, Current Development Objective, Architecture
Boundaries, Source of Truth (active vs archived), Development Rules, Evaluation Rules (Tracks A/B/C +
freeze and holdout rules), Git Rules, Verification (the commands actually validated in §10), Read
Next. Backed by `docs/README.md`, which classifies every document as ACTIVE, EVIDENCE or ARCHIVED
and states that archived documents carry no current implementation instruction.

Closed directions recorded in `docs/project-status.md` §7: commerce/business benchmark expansion,
multi-industry business adaptation, a generic business completion gate, unbounded multi-worker
optimisation, evidence-free Context Pack rewrites, and recovery-as-the-only-main-experiment.

## 6. Runtime Entry Points

Full detail and the per-mechanism matrix: `docs/runtime-entrypoints.md`. Summary:

| Path | Entry | Context plane | Tools / gate / ledger | Sandbox | Checkpoint | Oracle / verification |
|---|---|---|---|---|---|---|
| Workflow | `atlas run "…" --mode workflow` | selector + stable renderer (+ optional dedup) | none (agents compiled with `tools=[]`) | none | **on** (library resume only) | none |
| App coding | `atlas run "…" --mode coding` | not wired | progressive tools, gate, ledger, suspension, approvals | **on** (real provider required) | **off** | none |
| Evaluation | `python -m experiments.evaluation.cli run …` | not wired | progressive tools, gate, ledger, trajectory | **on** | **off** | **on** (sandboxed by default) |
| Recovery | `AtlasRuntime.resume`, `resume_suspension`, `atlas approve/reject`, `trusted_recovery.py` | rebuild on resume (workflow) | ledger classification | reattachment is manual | workflow only | — |
| Router | `workers/routing.py::route_delegation` | — | — | — | — | evaluation-only, default `off` |

Router answers: input = `DelegationProposal` + `CapabilityCatalog` + `RoutingPolicy`; `DIRECT`
collapses the run to the A0 single-worker plan; `DELEGATE_PARALLEL` runs the candidate DAG unchanged;
it cannot create workers or tasks and never rewrites the plan; it depends on a pre-built
decomposition (the benchmark's frozen `WorkerStep` list); it is imported by nothing under `src/`;
default `off`; tests `tests/unit/test_delegation_routing.py` (31) and
`tests/unit/test_stage_r05_routing.py` (15); the binding limitation is that it can only accept or
reject a plan it was handed.

## 7. Core Capability Preservation

| Pillar | Preserved | Evidence in this branch |
|---|---|---|
| Context Engineering | yes | `context/` 8 modules, `placement/` 4, `durability/` 4 unchanged (`src` diff empty) |
| Tool Engineering | yes | `tools/` 8 modules, catalog/search/state/dispatch/MCP unchanged |
| Multi-Agent Coordination | yes | `workers/` 7 modules incl. `routing.py`, `workflow.py`, `handoff.py`, `budget.py` unchanged |
| Reliable Execution | yes | `actions/` 7, `ledger/` 6, `sandbox/` 13, `suspension/` 6, `approvals/` 6, `trusted_recovery.py` unchanged |
| Observability & Evaluation | yes | `trajectory/` 9, `metrics/` 4, `verification/` 9, `experiments/evaluation/` 28 files incl. `sandboxed_oracle.py` unchanged |

No module was removed, disabled or deprecated by this stage. Business-benchmark adapter code
(`experiments/business_benchmarks/`, `commerce_agent_bench`) remains absent from this branch — it was
removed by Final-0/1 and stays on `experiment/b0-commerce-agent-bench` and
`fix/b0-r2-first-native-agent` / `fix/b0-r3-business-completion-contract`.

## 8. Naming

Public prose now says AtlasRuntime everywhere it identifies the project: `README.md`, `AGENTS.md`,
`docs/{README,project-status,runtime-entrypoints,final-roadmap,architecture,limitations}.md`, and the
package `description` in `pyproject.toml`.

Unchanged by design, and verified unchanged: package name `atlasos`, import path `atlasos`, console
script `atlas`, `ATLASOS_*` environment variables, module docstrings, schema names, historical run ids
(`atlasos-*`, `atlasos-agentbench-v1`), stage documents and Git history.

`docs/limitations.md` item 22 was the only active document that still claimed the repository name was
frozen as `Melon235/AtlasOS`; it now states the current repository name and keeps the internal
freeze. No global find-and-replace was performed (47 `AtlasOS` occurrences remain in active documents,
all of them legitimate references to identifiers, stage names or historical runs).

## 9. Evidence and Security

- Tracked artifacts: `git ls-files` contains no `_debug_run/`, `experiments/runs/`, `.venv/`, `.env`,
  `__pycache__/`, `.pytest_cache/`, `.ruff_cache/`, `*.db`, `*.sqlite` or raw `*.jsonl` run record.
- Ignore rules verified with `git check-ignore -v`: `experiments/runs/final2` → `.gitignore:22`;
  `.venv/…` → `.gitignore:7`. `experiments/evidence/` is **not** ignored as a whole (only
  `experiments/evidence/**/runs/`), so sanitized aggregates stay publishable; `tests/fixtures/` is not
  ignored anywhere.
- No `.gitignore` change was necessary. The one untracked file this stage produced (a local pytest
  log) was deleted before committing.
- Recovery-Pilot raw data: `D:\Work\AtlasOS\experiments\runs\final2\{atlas,native,native-normal}`
  plus the `app-*` and `evaluation` run directories — local, git-ignored, 0 tracked files, not copied
  here. Recorded content types: `result.json`/`session.json`, `trajectory/`, `ledger.db`,
  `idempotency.db`, `sandbox_bindings.db`, `fault.json`, workspace exports. No payload is reproduced
  in this report.
- B0 history: raw business runs were committed on `fix/b0-r2-first-native-agent` and removed from the
  index by `fix/b0-r3-business-completion-contract` (`1f8125e chore(bench): stop publishing raw
  business run artifacts`). They remain in Git history and in `experiments/evidence/stage-b0/runs/`
  locally. This branch must not claim they were "never public" or that history was purged.
- Secret scan over tracked files (`sk-…`, `ghp_…`, `BEGIN … PRIVATE KEY`): 0 hits. `.env.example`
  contains empty placeholders only. No `SECURITY_BLOCKER`.

## 10. Installation and Regression

Environment: `D:\Work\AtlasRuntime`, `.venv` created from CPython **3.12.15** (the machine default is
3.11.7, which does not satisfy `requires-python`), `pip install -e ".[app,dev,evaluation,sandbox]"`.

| Check | Command | Result |
|---|---|---|
| Import path | `python -c "import atlasos; print(atlasos.__file__)"` | `D:\Work\AtlasRuntime\src\atlasos\__init__.py` (this workspace, not `D:\Work\AtlasOS`) |
| AnyCode version | `version('anycode-py')` | `0.9.0` |
| Package version | `version('atlasos')` | `0.0.1` (editable, this workspace) |
| Tests | `python -m pytest` (`ATLASOS_RUN_LIVE_TESTS=0`, `ATLASOS_RUN_LIVE_SANDBOX=0`) | **exit 0 — 887 passed, 7 skipped, 1 deselected in 246.97 s** |
| Lint | `python -m ruff check .` | exit 0, "All checks passed!" |
| Format | `python -m ruff format --check .` | exit 0, "341 files already formatted" |
| Types | `python -m pyright` | exit 0, 0 errors / 0 warnings / 0 informations |
| CLI | `atlas.exe --help` | exit 0, 12 commands listed |
| Benchmark inventory | `python -m experiments.evaluation.cli list` | exit 0, 30 tasks (10 bugfix, 5 feature, 5 tool_discovery, 5 multi_worker, 5 long_context) |
| Offline demo | `python examples\m1_demo.py` | exit 0, stable 8-tool native surface, `tool_search` + `dispatch_action` executed |
| Live model run | — | **not executed** (forbidden by this stage) |
| Live sandbox task | — | **not executed** (forbidden by this stage) |

Notes: `experiments.evaluation.cli` imports the Inspect AI adapter at module scope, so the
`evaluation` extra is required even for `list`; this is now stated in the README. The previously
recorded "895 passed" is not reproducible verbatim under pytest 9.2.0 and is replaced by the measured
numbers above. The 7 skips are environment-gated (optional extras, live sandbox), and 1 test is
deselected by the `live` marker.

## 11. Change Inventory

| File | Change | In allowlist | Runtime behaviour |
|---|---|---|---|
| `AGENTS.md` | rewritten (272 → ~150 lines) as the current development contract | yes | no |
| `README.md` | restructured into 8 sections covering all five pillars | yes | no |
| `docs/README.md` | new ACTIVE / EVIDENCE / ARCHIVED index | yes | no |
| `docs/project-status.md` | new: identity, baseline, per-pillar status, mechanism status, evidence, closed directions | yes | no |
| `docs/runtime-entrypoints.md` | new: call chains for 5 paths + capability matrix | yes | no |
| `docs/final-roadmap.md` | new: Track A/B/C, holdout principle, stop conditions | yes | no |
| `docs/final-1-5-repository-baseline.md` | new: this report | yes | no |
| `docs/architecture.md` | recovery path corrected (no CLI checkpoint resume; coding compositions have no checkpoint); switchboard defaults aligned with `config.py`; CLI invocation syntax; context-plane scope note | yes | no |
| `docs/limitations.md` | no coding run can be resumed; oracle isolation is not a security proof; repository name corrected | yes | no |
| `docs/evaluation-summary.md` | dedup cache-miss reading corrected (sign convention); recovery evidence split into deterministic vs real-model; verification claim bounded by detection; "no automatic resume" added to unsupported claims | yes | no |
| `docs/anycode-delta.md` | pre-execution barrier is not transactional atomicity | yes | no |
| `docs/final-consolidation-report.md` | §17 marked SUPERSEDED BY FINAL EVALUATION ROADMAP; repository-name row annotated with the later rename | yes | no |
| `pyproject.toml` | `description` only | yes | no |

Not changed: `src/` (115 files), `tests/` (90), `experiments/` (113), `examples/` (6), `configs/`,
`.github/workflows/ci.yml`, `uv.lock`, `.gitignore`, `.gitattributes`, `THIRD_PARTY.md`, `docs/stages/`,
`docs/stage-*.md`, `docs/decisions/`, `docs/research-history.md`.

Document lengths: `AGENTS.md` 149 lines (target 100–160), `docs/project-status.md` 225 lines
(target ≤200 — over by 25 because each of the five pillars carries its own modules, default state,
evidence and limitation, and the mechanism table has 24 rows), `docs/runtime-entrypoints.md` 218,
`docs/final-roadmap.md` 128, `docs/README.md` 56, `README.md` 312.

## 12. Findings and Blockers

**P0 — blocks the stable baseline**: none. The branch installs, imports from its own worktree and
passes the offline suite.

**P1 — blocks parts of the final evaluation**

1. *No coding composition writes a checkpoint.* `src/atlasos/app/coding.py:646`,
   `experiments/evaluation/runner.py:1003`, `experiments/evaluation/baseline.py:143` all construct a
   bare `OrchestratorConfig()` (`checkpoint=None`), so `run_tasks(resume_from=…)` is unreachable and
   no coding run can be resumed by either arm. Impact: any completion-rate recovery metric, and the
   `atlas resume` operator story, are impossible without a product change. Suggested stage: decide in
   Final-2 whether checkpoint wiring is in scope; implement (if approved) as its own small change with
   tests, not as an experiment setup detail.
2. *The selective router cannot improve a decomposition.* `worker/routing.py` accepts or rejects the
   plan it is handed; the plan comes from the benchmark's frozen `WorkerStep` list
   (`stage_r0_routing.py::build_proposal`). Impact: Track C as currently wired measures a
   decomposition produced by the benchmark author, not by the runtime. Suggested stage: Final-2 must
   fix what the router's input is before Final-4 measures it.
3. *The Recovery-Pilot general fix is not on this branch.* Coding sessions can still report
   `status=failed` with `failure_class=null` when the agent loop ends without success. Impact:
   attribution noise in Tracks A/B, and the pilot's S1-A defect remains reproducible here. Suggested
   action: owner decision, then a single-file port of `18e523c` including its regression test.

**P2 — known technical debt**

4. `workspace.patch` reports `patch_changed_files=1` for a two-file diff
   (`sandbox/export.py` counts `git status --porcelain` lines). Metric label only; not fixed.
5. `input_digest_coverage` is emitted by the R1-1 evaluation path but not by the application coding
   path, so the two paths are not comparable on that metric.
6. An abrupt kill leaks a live sandbox pair; reaping is manual and a coding run has no sweeper.
7. The application coding mode only accepts a UAT-shaped workspace containing `csv_parser.py`.
8. `experiments.evaluation.cli` requires the `evaluation` extra for every subcommand, including
   `list`, because the Inspect adapter is imported at module scope (now documented, not changed).
9. `docs/` holds 63 files; the archive was indexed, not physically reorganised, deliberately.
10. Verification recall remains thin (23 TP / 23 FN); it is an observation, not a gate.

**P3 — documentation/engineering hygiene (fixed here)**

11. `docs/architecture.md` recorded `atlas resume <run_id> → AtlasRuntime.resume(...)`, which the
    code does not do; and its switchboard defaults (`task_scoped`, `progressive`, `trajectory=true`)
    contradicted `config.py`.
12. `docs/evaluation-summary.md` claimed the warm cache-miss rate "got worse" at the 44 % dedup
    point; the source table shows an 11.1 % *reduction* there (negative values belong to the
    small/medium rows).
13. `README.md` described the project as a recovery runtime only, repeated the same cache claim, and
    advertised "895 tests" that this environment does not reproduce.
14. `docs/limitations.md` still froze the repository name as `Melon235/AtlasOS`.
15. `pyproject.toml` still described the project as a cache-aware context runtime.
16. There was no document index, so a new session could not tell active from archived material.

## 13. Final Readiness

| Dimension | Verdict | Basis |
|---|---|---|
| Git Baseline | **PASS** | Branch from the confirmed Final-0/1 SHA, ancestor check passed, no rewrite, no push, pilot untouched |
| Harness Readiness | **PASS** | New `AGENTS.md` + document index; a new session can start from `docs/README.md` and `docs/project-status.md` alone |
| Architecture Clarity | **PASS** | Five pillars with per-path wiring, plus the capability matrix that names every `NOT_WIRED`/`NOT_VALIDATED` cell |
| Core Preservation | **PASS** | `src`/`tests`/`experiments` diff empty; all five pillars present |
| Offline Regression | **PASS** | 887 passed / 7 skipped / 1 deselected; ruff, format, pyright clean; CLI and 30-task inventory verified |
| Runtime Live Readiness | **NOT_VALIDATED** | No live provider or sandbox run was permitted in this stage; the last real execution evidence is the pilot at `c5fce5c` |
| Final Evaluation Readiness | **PARTIAL** | Protocol and holdout not designed; recovery metric blocked by P1-1; router input blocked by P1-2 |
| Public Release Readiness | **PARTIAL** | Branch is local; no CI run, no demo recording, no release notes |

## 14. Proposed Next Action

1. Project owner reviews this baseline and the P1 findings.
2. Decide whether to port the Recovery-Pilot general fix (`18e523c`) — the only `src/` change on the
   pilot branch.
3. If approved, integrate it as its own minimal change with its regression test, then re-run the
   offline suite.
4. Re-run the regression on the resulting candidate.
5. Push safely and check CI.
6. Start Final-2 (evaluation protocol and holdout design).

This stage stops here. Final-2 and everything after it require a separate instruction.
