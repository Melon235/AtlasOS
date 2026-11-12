# AGENTS.md — AtlasRuntime Development Contract

This file is the operating contract for any AI harness working in this repository. It is short on
purpose. Project history is not restated here.

## 1. Project Identity

- Public name: **AtlasRuntime** — a context-aware, tool-aware agent execution runtime with trusted
  execution, resource governance, persistence and evaluation. Built on **AnyCode 0.9.0**.
- Repository: `https://github.com/Melon235/AtlasRuntime` (default branch `master`).
- Python distribution and import package: `atlasos`. CLI: `atlas`. Environment variables: `ATLASOS_*`.
  These are frozen compatibility identifiers — never rename them as a "cleanup".
- Application domain: software engineering workflows.
- Five technical pillars, all of them part of the project:
  1. **Context Engineering** — `context/`, `context/transform.py`, `placement/`, `durability/`.
  2. **Tool Engineering** — `tools/`, `tools/search.py`, `tools/dispatch.py`, MCP integration.
  3. **Multi-Agent Coordination** — `workers/`, `workflow.py`, `handoff.py`, `budget.py`,
     `workers/routing.py`.
  4. **Reliable Execution** — `actions/`, `ledger/`, `sandbox/`, `suspension/`, `approvals/`,
     `trusted_recovery.py`, `durability/`.
  5. **Observability & Evaluation** — `trajectory/`, `metrics/`, `verification/`,
     `experiments/evaluation/`.

Do not describe the project as a recovery runtime only, and do not describe optional mechanisms as
default behaviour.

## 2. Current Development Objective

Current objective: **final integration and evaluation**. The scope-expansion era is closed.

- Work only from an explicit user instruction for the current stage. A historical stage document is
  not an authorisation to continue that stage.
- No unbounded feature growth: no new runtime modules, planners, schedulers, routers, tool executors,
  business adapters, benchmark runners or datasets unless the user asks for exactly that.
- Closed directions: commerce/business benchmark adapters, multi-industry business adaptation,
  generic business completion gates, unbounded multi-worker optimisation, evidence-free context-pack
  rewrites.

## 3. Architecture Boundaries

AnyCode 0.9.0 owns the agent loop, `AgentRunner`, task queue, DAG scheduler, wave execution,
`AgentPool`, `ToolRegistry`, `ToolExecutor`, checkpoints, native resume, idempotency
claim/complete/replay, provider infrastructure, native HITL and the native transcript.

AtlasRuntime adds explicit context management and stable rendering, progressive tool discovery,
scoped-worker construction, trusted authorization decisions, durable action state, sandbox binding
and continuity checks, durable suspension/approval, run-scoped shared budget, structured handoff,
selective delegation, operation-level trace integrity, and independent deliverable verification.

The per-module provenance is `docs/anycode-delta.md`. Never present an upstream capability as an
AtlasRuntime original. Never reimplement an upstream mechanism that already exists; prefer adapter →
subclass → minimal patch.

Further invariants that must not be broken:

- Prompt-cache state is an optimisation hint, never source-of-truth state. Correctness must not
  depend on a cache hit; a 0 % hit run must still be correct.
- Stable prefix before dynamic context. No volatile value (timestamp, run id, UUID, metric) in the
  stable prefix.
- Context selection (WHAT) and prompt rendering (HOW) stay separate subsystems.
- Atlas task scheduling and ContextPilot request ordering stay separate layers.
- Every research mechanism must be switchable off via configuration so its baseline stays
  reproducible.

## 4. Source of Truth

Active documents (read these, in this order, before changing anything):

- `docs/project-status.md` — current baseline, architecture status, mechanism status.
- `docs/architecture.md` — layers, execution paths, persisted state.
- `docs/anycode-delta.md` — upstream vs AtlasRuntime responsibility.
- `docs/runtime-entrypoints.md` — real call chains and the per-entry-point capability matrix.
- `docs/design-decisions.md` — trade-offs and rejected designs.
- `docs/final-roadmap.md` — the only forward plan.
- `docs/limitations.md` — known constraints.
- `docs/README.md` — the index of all three document classes.

Archived documents (`docs/stages/`, `docs/stage-*.md`, historical experiment reports) are evidence
for traceability. They carry no current implementation instruction. Keep them; do not rewrite their
recorded results.

## 5. Development Rules

- Reuse existing modules first; read the call chain before adding an abstraction.
- Evidence before action: reproduce or cite the defect, then make the smallest coherent change.
- No large refactors for tidiness, no opportunistic renames, no deleting working research code
  because it is optional or performed badly in one experiment. Record, do not remove.
- Do not promote an optional research mechanism into the default path without a measured result and
  an explicit instruction.
- Negative results stay in the repository; deleting evidence is a regression.
- Every optimisation must remain ablation-switchable through `config.py` / YAML.
- If a problem is out of the current stage's scope: write it down, do not fix it.

## 6. Evaluation Rules

Final evaluation has exactly three tracks (details: `docs/final-roadmap.md`):

- **Track A — Complete Module Evaluation**: functional status and benefit status per retained module.
- **Track B — Integrated Runtime Evaluation**: native AnyCode 0.9.0 vs AtlasRuntime stable, same
  model, tasks, tools, sandbox, resources and oracle.
- **Track C — Adaptive Delegation Evaluation**: always-single vs always-multi vs selective router.

Before a formal run, freeze runtime commit, model, task set, tool surface, sandbox image, oracle,
budget and router policy. Development tasks and holdout tasks must be disjoint; a task debugged
against a result is development evidence, not holdout. Router inputs must never include oracle
outcomes. Runtime success is not oracle success — report the oracle verdict separately.

Do not disable a legitimate native AnyCode capability to manufacture an advantage.

## 7. Git Rules

- Never modify `master` history; never force-push; never rebase shared branches; never delete
  branches; never merge or cherry-pick automatically.
- Never overwrite uncommitted user work. Do not `reset --hard`, `clean -fd`, or stash without an
  explicit instruction.
- Do not commit raw run artifacts (transcripts, tool payloads, `ledger.db`, checkpoints, provider
  requests/responses, `.env`, `.venv`). Explicitly stage files; never `git add -A`.
- Never read, print, or commit secrets. A discovered secret is a blocker: report the path, do not fix
  it by rewriting history.
- All pushes, PRs and CI-affecting changes require the project owner's approval.

## 8. Verification

Run from the repository root in a Python 3.12 `.venv` created inside the workspace:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[app,dev,evaluation,sandbox]"
.\.venv\Scripts\python.exe -c "import atlasos; print(atlasos.__file__)"   # must be this workspace
.\.venv\Scripts\python.exe -m pytest                                     # offline suite
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
.\.venv\Scripts\python.exe -m pyright
.\.venv\Scripts\atlas.exe --help
.\.venv\Scripts\python.exe -m experiments.evaluation.cli list
.\.venv\Scripts\python.exe examples\m1_demo.py
```

`pytest` runs with `-m 'not live'` plus `ATLASOS_RUN_LIVE_TESTS=0` /
`ATLASOS_RUN_LIVE_SANDBOX=0`; no command above calls a provider or a sandbox service. A live
DeepSeek or OpenSandbox run is opt-in, budget-bounded and never part of the default suite.

Report results honestly: failed, skipped and not-run checks are recorded as such.

## 9. Read Next

Read only the documents the current task needs: `docs/README.md` for the index,
`docs/project-status.md` for the baseline, `docs/runtime-entrypoints.md` before touching an entry
point, `docs/final-roadmap.md` before any evaluation work. Do not load the whole historical stage
archive at session start.
