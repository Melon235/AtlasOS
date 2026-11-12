# Project Status — AtlasRuntime

Entry point for a new harness session. Everything below was verified against this branch's source,
tests and Git state; nothing is copied from a historical claim.

## 1. Current Identity

| Item | Value |
|---|---|
| Public name | AtlasRuntime — *a context-aware and reliable agent execution runtime built on AnyCode* |
| GitHub | `https://github.com/Melon235/AtlasRuntime` (repository renamed from `AtlasOS`; default branch `master`) |
| Git remote | `origin` → `https://github.com/Melon235/AtlasRuntime.git` |
| Python distribution | `atlasos` (frozen compatibility identifier) |
| Python import package | `atlasos` (frozen) |
| CLI | `atlas` (`atlasos.app.cli:app`) |
| Environment variables | `ATLASOS_*` (frozen) |
| Runtime base | `anycode-py[openai]==0.9.0` |
| Python | `>=3.12` (validated on CPython 3.12.15, Windows 11) |
| Domain | software engineering workflows |
| Current scope | final integration and evaluation (no unbounded feature work) |

Naming is intentionally split: **public prose says AtlasRuntime**, while the package, CLI,
environment variables, historical run ids and Git history keep `atlasos` / `atlas` /
`ATLASOS_*` / `atlasos-*`. Do not "unify" them without a dedicated migration stage.

## 2. Current Baseline

| Item | Value |
|---|---|
| Final-0/1 consolidated candidate | `refactor/final-runtime-consolidation` = `16251d05c34cff41d280396a1647214914e51a31` |
| Final-0/1 base | `fix/r1-2h-oracle-sandbox-isolation` = `facca984849c9821ec3bdc1df2eb6ee20bca757b` (confirmed ancestor) |
| Current stage branch | `chore/final-1-5-repository-baseline` (this worktree) |
| Final-1.5 baseline (`BASE_SHA` of that stage) | `16251d05c34cff41d280396a1647214914e51a31` |
| Final-1.6 ported fix | `1b4f6be` = cherry-pick of `18e523c` (coding-session outcome attribution, 2 files) |
| Worktree | `D:\Work\AtlasRuntime` (primary research worktree remains `D:\Work\AtlasOS`) |
| Recovery Pilot branch | `experiment/final-2-recovery-pilot` = `c5fce5cc5d3d96251968b264e8113685711ffc8e`, 3 commits on top of `16251d0` |
| Recovery Pilot in the stable baseline? | **One commit only** (`18e523c`, as `1b4f6be`). The rest is audited only — see `docs/final-1-5-repository-baseline.md` §4 and `docs/final-1-6-evaluation-baseline.md` §3 |
| Local `master` | `07e18ec` (22 commits ahead of `origin/master`) |
| Remote `master` | `3fbf3ddb7867b45c1cab40e8ff094c7d94d795af` (default branch, unchanged) |
| Pushed branches | 9 historical branches on `origin` besides `master`; `refactor/final-runtime-consolidation` and `experiment/final-2-recovery-pilot` are **local only** |

A run's own `result.json` / `session.json` records `git_commit` and `git_dirty`; use it rather than
memory when citing which commit produced a number.

## 3. Current Architecture

Five pillars. "Default state" always names the entry point, because the entry points are not
identical (`docs/runtime-entrypoints.md`).

### 3.1 Context Engineering

- Modules: `context/` (`models.py`, `store.py`, `selectors.py`, `renderer.py`, `transform.py`,
  `requirements.py`, `fingerprints.py`), `placement/`, `durability/context_rebuild.py`.
- Responsibility: represent what a task logically needs (`ContextBlock` / `ContextRef`), select it
  (`TaskScopedSelector`), order it deterministically (`StablePromptRenderer`), optionally dedupe it
  (`ContextPilotDedupTransform`), place tasks by context locality (`placement/`), and rebuild the
  store deterministically on checkpoint resume (`durability/`).
- Source: `runtime.py::_configure_context_mode` / `_build_atlas_prompt`; `context/renderer.py`.
- Default state: **active** on the workflow path (`app/service.py` sets
  `policy="task_scoped"`, `renderer="stable_deterministic"`); **not wired** on the application
  coding path and the evaluation path, which render prompts upstream (see §4).
- Evidence: stable layout removes completion-order dependence (S6 Table A); dedup reaches a 44.0 %
  prompt-token cut at ~50 % exact duplicates (S6 Table B) but **no matching cache-miss win**;
  context-locality placement cuts logical handoff 50–80 % (S6 Table C); E2 C1 == C0 on prompt
  tokens (+0.8 %).
- Limitation: the cache claim is withdrawn — a shorter prompt is not a cheaper prompt; dedup and
  placement are optional and off in the app.

### 3.2 Tool Engineering

- Modules: `tools/` (`runtime.py`, `catalog.py`, `search.py`, `state.py`, `dispatch.py`,
  `registry.py`, `conversation.py`).
- Responsibility: one registry for every executable tool; a stable native surface (core tools
  only); `tool_search` (keyword or semantic) + `dispatch_action`; per-run `RunToolState` of
  disclosed tools; MCP servers registered but hidden until disclosed.
- Source: `tools/runtime.py::AtlasToolRuntime`, `integrations/anycode_engine.py::build_agent`.
- Default state: **active** on the application coding path and the evaluation path;
  **not wired** on the workflow path (`config.tools.mode` defaults to `disabled`).
- Evidence: semantic retrieval Recall@1 0.875 / Recall@3 1.000 / MRR 0.938 on a fixed 24-tool
  catalog and 24-query set; native schema 8 558 → 4 207 chars; tool-surface prompt 2 599 → 1 399
  tokens (−46.2 %) in a **schema microbenchmark**, not an end-to-end coding measurement.
- Limitation: the 46.2 % figure describes tool schemas only; the shorter surface did **not** raise
  cache-hit ratio in that microbenchmark.

### 3.3 Multi-Agent Coordination

- Modules: `workers/` (`factory.py`, `catalog.py`, `models.py`, `renderer.py`, `store.py`,
  `routing.py`), `workflow.py`, `handoff.py`, `budget.py`.
- Responsibility: compile an Atlas workflow to AnyCode task specs; construct scoped workers with a
  trusted `ExecutionContext`; dual-channel handoff between workers; one run-scoped shared budget;
  evidence-based selective delegation.
- Source: `workers/factory.py::ScopedWorkerFactory.create`; `workflow.py::compile_workflow`;
  `workers/routing.py::route_delegation`.
- Default state: single worker. Multi-worker **optional**; dual handoff **optional**; shared budget
  **optional** (`budget_mode="guarded"` in the evaluation runner only); router **evaluation only**.
- Evidence: E1 28/36 single vs 3/36 static multi, cost 4.12×, p50 latency 1.94×; G3-4a 6/6 vs 2/6;
  R0 5/6 vs 2/6; R0.5 forced-positive admission 6/6 admitted, 0 false admissions, still +40 % cost.
  Real parallelism exists (20/20 runs overlapped), the net benefit does not.
- Limitation: selective delegation has never produced a live `DELEGATE_PARALLEL` admission in a
  benchmark; its generalisation benefit is unproven.

### 3.4 Reliable Execution

- Modules: `actions/` (`gate.py`, `policy.py`, `catalog.py`, `binding.py`, `reviewer.py`),
  `ledger/` (`store.py`, `bridge.py`, `recovery.py`, `identity.py`), `sandbox/` (`binding.py`,
  `store.py`, `reattachment.py`, `ownership.py`, `provision.py`, `tools.py`), `suspension/`,
  `approvals/`, `trusted_recovery.py`, `durability/`.
- Responsibility: authorize every project action before execution; record it before and after;
  classify an interrupted action as `UNCERTAIN` instead of replaying it; bind a run to one sandbox
  and verify reattachment; suspend a whole model response before any of its tools run; persist
  approvals; rebuild context deterministically on resume.
- Source: `tools/conversation.py` (agent seam) → `actions/gate.py` → `ledger/bridge.py`;
  `runtime.py::resume`; `approvals/resume.py`.
- Default state: gate + ledger + suspension + sandbox **active** on the application coding path;
  gate + ledger active and suspension **off** on the evaluation path; checkpoint + resume active
  **only** on the workflow path, where the gate and ledger are **off**.
- Evidence: G3-1 `side_effect_unknown` 2 → 0, oracle 2/4 → 4/4, 3 `UNCERTAIN` records, 0 replays;
  deterministic control plane 24 requests → 15 ALLOW / 1 DENY / 8 REQUIRE_HUMAN, 0 bypasses;
  30-task benchmark 0/90 unauthorized side effects.
- Limitation: **no coding composition enables AnyCode checkpointing**, so no coding run can be
  resumed — Atlas's measured advantage after an identical kill is post-crash accountability
  (1 in-flight ledger row + binding + idempotency claim + 4 trajectory events vs nothing), not
  completion rate.

### 3.5 Observability and Evaluation

- Modules: `trajectory/`, `metrics/`, `identity` digests in `ledger/`, `verification/`,
  `experiments/evaluation/` (`runner.py`, `baseline.py`, `sandboxed_oracle.py`, `oracle.py`,
  `tasks.py`, `analysis.py`, `cli.py`).
- Responsibility: record `action_proposed` before execution and the observation after; reconcile
  outcomes; capture tokens / cache hit / cache miss / latency / cost per call; verify deliverables
  deterministically; grade hidden tests inside a disposable network-disabled sandbox.
- Source: `trajectory/recorder.py`; `metrics/collector.py`; `verification/engine.py`;
  `experiments/evaluation/sandboxed_oracle.py`.
- Default state: trajectory **active** on the coding and evaluation paths, **opt-in** on the
  workflow path (`trajectory.enabled` defaults to `False`); verification **evaluation-only,
  observation-only**; oracle **evaluation-only**, sandbox mode by default.
- Evidence: `result.json` captured 365 of 591 actions (38 % missing); after the fix,
  `input_digest_coverage = 1.00` over 32 runs and 111 recovered actions; verification 0 false
  positives on 66 deliverables; oracle re-grading 28/28 identical at ~6 s and 0 model tokens.
- Limitation: verification recall is thin (23 TP / 23 FN) and it never blocks; 28/28 is a statement
  about those historical workspaces, not a security proof of the isolated oracle. The coding session's
  `prompt_tokens` / `completion_tokens` come from `TokenUsage`, i.e. **cache-miss** input tokens —
  they are not comparable with `result.json` or `inference.jsonl` `prompt_tokens`, which include
  cache hits.

## 4. Mechanism Status

`DEFAULT_ACTIVE` = on in the named entry point without configuration. `OPTIONAL` = exists and is
switchable. `EVALUATION_ONLY` = wired only in `experiments/`. `RETAINED_RESEARCH` = kept despite a
neutral/negative result. `NOT_WIRED_IN_ALL_PATHS` = real, but absent from at least one path that
users would expect. `NOT_VALIDATED` = implemented, no live evidence.

| Mechanism | Status | Where it is actually wired |
|---|---|---|
| Context selector + stable renderer | DEFAULT_ACTIVE / NOT_WIRED_IN_ALL_PATHS | workflow path only |
| ContextPilot dedup transform | OPTIONAL / RETAINED_RESEARCH | workflow path, requires `contextpilot` extra |
| Context-locality placement | OPTIONAL / RETAINED_RESEARCH | workflow path, `placement.policy` |
| Tool search + progressive disclosure | DEFAULT_ACTIVE / NOT_WIRED_IN_ALL_PATHS | coding + evaluation paths |
| Scoped worker + trusted identity | DEFAULT_ACTIVE / NOT_WIRED_IN_ALL_PATHS | coding + evaluation paths |
| Dual-channel handoff | OPTIONAL / RETAINED_RESEARCH | evaluation path (`handoff_mode="dual"`) |
| Run-scoped shared budget | OPTIONAL | evaluation path (`budget_mode="guarded"`) |
| Selective delegation router | EVALUATION_ONLY | `experiments/evaluation/runner.py` + `stage_r0_routing.py` |
| Worker ownership enforcement | OPTIONAL, default off | evaluation path (`ownership_mode`) |
| Action safety gate | DEFAULT_ACTIVE / NOT_WIRED_IN_ALL_PATHS | coding + evaluation; **off** in workflow path |
| Durable action ledger | DEFAULT_ACTIVE / NOT_WIRED_IN_ALL_PATHS | coding + evaluation; **off** in workflow path |
| Sandbox binding + workspace tools | DEFAULT_ACTIVE / NOT_WIRED_IN_ALL_PATHS | coding + evaluation (real provider required) |
| Durable suspension + approvals | DEFAULT_ACTIVE / NOT_WIRED_IN_ALL_PATHS | application coding; not in evaluation |
| Checkpoint resume (`AtlasRuntime.resume`) | OPTIONAL, library API only | workflow path; unreachable from the CLI |
| Uncertain-side-effect classification | DEFAULT_ACTIVE (coding/evaluation) | `ledger/recovery.py` |
| Trajectory recorder | DEFAULT_ACTIVE / OPTIONAL | coding + evaluation always; workflow opt-in |
| History compaction | OPTIONAL / RETAINED_RESEARCH | evaluation path only |
| Tool-result offload (`history.read`) | OPTIONAL / RETAINED_RESEARCH | evaluation path only |
| Coding-session failure attribution (`MODEL_UNFINISHED`) | DEFAULT_ACTIVE | application coding (`app/coding.py`) |
| Deliverable verification | EVALUATION_ONLY (observation only) | evaluation path |
| Isolated oracle | EVALUATION_ONLY, default `sandbox` | evaluation path |
| Coding benchmark (30 tasks) | EVALUATION_ONLY | `experiments/evaluation/tasks.py` |
| Coding-run crash recovery | NOT_VALIDATED | no composition enables checkpointing |
| Suspension resume inside a live model run | NOT_VALIDATED | never triggered |

## 5. Historical Evidence

Each row is frozen at its own commit and task set; the numbers are not re-measured on this branch.

**Tool Engineering** — semantic Recall@1 0.875, Recall@3 1.000, MRR 0.938 (24-tool fixed catalog);
native tool schema 8 558 → 4 207 chars; tool-surface microbenchmark 2 599 → 1 399 prompt tokens.

**Context Engineering** — stable rendering makes layout independent of completion order; 44.0 %
maximum prompt-token reduction at high duplicate ratio; context-locality placement cuts logical
handoff by 50 % / 66.7 % / 80 % at chain length 2 / 3 / 5; warm cache-miss reduction is workload
dependent (11.1 % at large scale, negative at small and medium scale).

**Multi-Agent** — Stage E1 single 28/36 vs static multi 3/36; multi prompt tokens 6.59×, cost 4.12×,
p50 latency 1.94×; selective router rejected 12/12 candidates live and its generalisation benefit
is unproven.

**Reliable Execution** — control-plane authorization scenario (24 requests → 15/1/8, 0 bypasses);
SQLite durability scenario; sandbox reattachment scenario; durable suspension / approval scenario;
G3-1 tool-error semantics 2/4 → 4/4 oracle with 3 `UNCERTAIN` records and 0 replays; Final-2 pilot
(post-crash accounting) recorded only in the pilot report — see §2 for the branch and SHA.

**Observability and Evaluation** — `result.json` vs trajectory 365 / 591 actions (38 % missing);
R1-1 input-digest coverage 1.00 over 32 runs; verification 0 false positives with weak recall;
oracle isolation 28/28 identical scores.

Not done by this stage: no number above was re-executed on `BASE_SHA`, and no live model run was
performed. The Final-2 pilot's own results belong to `c5fce5c`, not to this branch. The offline suite
was re-measured after the Final-1.6 port: 891 passed / 6 skipped / 1 deselected
(`docs/final-1-6-evaluation-baseline.md` §5).

## 6. Current Work

1. Repository baseline (Final-1.5) — identity, harness contract, document index, entry-point
   matrix, roadmap, regression record.
2. Evaluation environment readiness (Final-1.6) — the `18e523c` attribution fix is ported as
   `1b4f6be`; dependency inventory, skip analysis and the readiness matrix are in
   `docs/final-1-6-evaluation-baseline.md`.
3. Final evaluation protocol and holdout design (Final-2, not started).
4. Complete module evaluation (Track A).
5. Integrated runtime evaluation vs native AnyCode (Track B).
6. Adaptive delegation pilot (Track C).
7. Demo, release and public packaging (Final-6).

Decisions still owed by the project owner (all are Final-2 design inputs, none is implemented):
coding-composition checkpoint wiring, Context-Plane integration (if any), router C1 protocol and its
candidate-DAG source, a semantic-retrieval evidence requirement, and the ContextPilot
isolated-environment procedure (its `.pth` hook breaks 9 unrelated tests on Windows).

## 7. Closed Directions

- Commerce Agent Bench expansion and multi-industry business adaptation (adapter built, track
  stopped by decision; raw runs stay in Git history and local ignored directories).
- A generic business completion gate.
- Unbounded multi-worker optimisation (static decomposition repeatedly lost on success, cost and
  latency).
- Evidence-free Context Pack rewrites and KV-cache/serving-engine work (V2 scope, not V1).
- Recovery as the single main experiment: recovery is now one reliability module inside Track A.
