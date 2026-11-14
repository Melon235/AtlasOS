# AGENTS.md — AtlasRuntime Development Contract

This file is the operating contract for any AI harness working in this repository. It is short on
purpose. Project history is not restated here.

## 1. Project Identity

- Public name: **AtlasRuntime** — *Context-aware and Reliable Agent Runtime*. Built on **AnyCode
  0.9.0**. The authoritative positioning, the two cores and the extension are defined once, in
  [`docs/architecture-design-contract.md`](docs/architecture-design-contract.md).
- Repository: `https://github.com/Melon235/AtlasRuntime` (default branch `master`).
- Python distribution and import package: `atlasos`. CLI: `atlas`. Environment variables: `ATLASOS_*`.
  These are frozen compatibility identifiers — never rename them as a "cleanup".
- Application domain: software engineering workflows.
- Module groups (code layout; the contract, not this list, ranks what is core):
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

## 2. Architecture Identity and Design Contract

**AtlasRuntime is a Context-aware and Reliable Agent Runtime.** The owner-confirmed design baseline
is [`docs/architecture-design-contract.md`](docs/architecture-design-contract.md). Read it before
starting any task that touches architecture, an execution path, an evaluation design or a project
description.

The hierarchy is fixed:

1. **Context Engineering** — core. Logical context model (`ContextBlock` / `ContextRef` /
   `ContextStore` / versioning), task-scoped selection, deterministic rendering with a stable prefix
   and layout/prompt hashes, context transform (identity / optional ContextPilot exact-duplicate
   dedup), context-locality placement, context-aware recovery.
2. **Reliable Execution** — core, equal standing. Tool engineering and progressive disclosure,
   scoped workers and trusted identity, `ActionSafetyGate`, durable action ledger and uncertain-side-
   effect classification, sandbox binding and trusted local OpenSandbox, approval / durable
   suspension / trusted recovery, shared run budget.
3. **Adaptive Delegation** — an **extension only**. The router decides whether to accept a candidate
   decomposition; placement decides assignment; the Context Plane decides what a task receives.
   Three separate mechanisms, three separate layers.
4. **Evaluation and Observability** — verification of the above, not a substitute for them.

Rules that follow from this contract:

- A stage task may narrow scope; it must never overwrite the long-term design. Do not delete,
  downgrade or replace a core mechanism without the project owner's explicit approval.
- Implemented ≠ enabled. Declared in a manifest or a YAML file ≠ executed. Report both facts
  separately, with the entry point named.
- Never present a mechanism that is not wired into the path as if the real runtime used it, and never
  present a configuration-only declaration as an executed mechanism.
- Never re-interpret the original design from the current implementation. The implementation is
  audited against the contract; the contract is not rewritten to match the implementation.
- Naming is frozen: `atlasos` / `atlas` / `ATLASOS_*` / historical run ids stay as they are.

## 3. Current Development Objective

Current objective: **final integration and evaluation**. The scope-expansion era is closed.

- Work only from an explicit user instruction for the current stage. A historical stage document is
  not an authorisation to continue that stage.
- No unbounded feature growth: no new runtime modules, planners, schedulers, routers, tool executors,
  business adapters, benchmark runners or datasets unless the user asks for exactly that.
- Closed directions: commerce/business benchmark adapters, multi-industry business adaptation,
  generic business completion gates, unbounded multi-worker optimisation, evidence-free context-pack
  rewrites.

## 4. Architecture Boundaries

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

## 5. Source of Truth

Active documents (read these, in this order, before changing anything):

- `docs/architecture-design-contract.md` — the owner-confirmed design baseline (layer 1).
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

## 6. Development Rules

- Reuse existing modules first; read the call chain before adding an abstraction.
- Evidence before action: reproduce or cite the defect, then make the smallest coherent change.
- No large refactors for tidiness, no opportunistic renames, no deleting working research code
  because it is optional or performed badly in one experiment. Record, do not remove.
- Do not promote an optional research mechanism into the default path without a measured result and
  an explicit instruction.
- Negative results stay in the repository; deleting evidence is a regression.
- Every optimisation must remain ablation-switchable through `config.py` / YAML.
- If a problem is out of the current stage's scope: write it down, do not fix it.

## 7. Evaluation Rules

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

## 8. Git Rules

- Never modify `master` history; never force-push; never rebase shared branches; never delete
  branches; never merge or cherry-pick automatically.
- Never overwrite uncommitted user work. Do not `reset --hard`, `clean -fd`, or stash without an
  explicit instruction.
- Do not commit raw run artifacts (transcripts, tool payloads, `ledger.db`, checkpoints, provider
  requests/responses, `.env`, `.venv`). Explicitly stage files; never `git add -A`.
- Never read, print, or commit secrets. A discovered secret is a blocker: report the path, do not fix
  it by rewriting history.
- All pushes, PRs and CI-affecting changes require the project owner's approval.

## 9. Verification

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

## 10. Context Handoff

At the start of every new long-running task, and after any context compaction or session switch,
restore state in this order before writing anything:

1. `docs/architecture-design-contract.md` — what the project is (layer 1).
2. The currently frozen runtime SHA and the current branch/HEAD.
3. The current active task and the instruction that authorised it.
4. The current implementation and actual execution state of the mechanisms that task touches
   (`docs/project-status.md`, `docs/runtime-entrypoints.md`, and the latest integration report).
5. The approved modification scope for that task.
6. What is still unfinished, blocked or owed to the project owner.

Do not infer the project's design positioning from the most recent stage report. A stage report
describes one increment; the contract describes the project.

## 11. Read Next

Read only the documents the current task needs: `docs/README.md` for the index,
`docs/project-status.md` for the baseline, `docs/runtime-entrypoints.md` before touching an entry
point, `docs/final-roadmap.md` before any evaluation work. Do not load the whole historical stage
archive at session start.
