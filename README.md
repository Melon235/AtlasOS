# AtlasRuntime

**A context-aware and reliable agent execution runtime built on AnyCode.**

AtlasRuntime runs AI coding agents inside a controlled sandbox and gives the run the things an agent
runtime needs before anyone trusts it with a real repository: an explicit context plane, a progressive
tool surface, scoped workers, an authorization gate, a durable action ledger, sandbox binding,
durable suspension and approval, and an audit trail that survives a crash.

- Built on [AnyCode 0.9.0](https://github.com/Quantlix/anycode): the agent loop, DAG scheduler,
  checkpoints, provider adapters, tool executor and run store are upstream.
- Domain: software engineering workflows.
- Scope: a research-engineering runtime with a reproducible evaluation harness — not a commercial
  agent product, not a serving engine, not another agent framework.
- Honest boundary: it does **not** yet resume a killed coding run. Both coding compositions leave
  AnyCode checkpointing off; what AtlasRuntime preserves through a crash is the evidence needed to
  decide what happened. See [limitations §4](docs/limitations.md) and
  [`docs/runtime-entrypoints.md`](docs/runtime-entrypoints.md) §4.

Start here: [`docs/architecture-design-contract.md`](docs/architecture-design-contract.md) (the
owner-confirmed design baseline) · [`docs/README.md`](docs/README.md) (document index) ·
[`docs/project-status.md`](docs/project-status.md) (current baseline) ·
[`AGENTS.md`](AGENTS.md) (development contract) ·
[`docs/final-roadmap.md`](docs/final-roadmap.md) (what comes next).

---

## 1. What is AtlasRuntime?

Two of the groups below are the project's **cores** — *Context Engineering* and *Reliable
Execution*. *Adaptive Delegation* (inside Multi-Agent Coordination) is an **extension**: it decides
whether a candidate decomposition is accepted, never what a task's context is. *Observability and
Evaluation* verifies the rest; it is not a substitute for it. The binding statement of that hierarchy
is [`docs/architecture-design-contract.md`](docs/architecture-design-contract.md); this README
summarises it and the code, and where the two disagree the contract is right.

Five module groups, each with its own evidence and its own limits:

| Pillar | Question it answers | Code |
|---|---|---|
| **Context Engineering** | Which information does *this* task logically need, and how is it laid out so repeated prompts stay cheap? | `context/`, `placement/`, `durability/` |
| **Tool Engineering** | How does a model work with hundreds of tools without paying for all their schemas? | `tools/` |
| **Multi-Agent Coordination** | When is delegation worth it, and how is one budget, one identity, one handoff shared? | `workers/`, `workflow.py`, `handoff.py`, `budget.py` |
| **Reliable Execution** | Who authorized this action, what happened, and what is true after a crash? | `actions/`, `ledger/`, `sandbox/`, `suspension/`, `approvals/` |
| **Observability & Evaluation** | What actually ran, and did the deliverable pass an independent grader? | `trajectory/`, `metrics/`, `verification/`, `experiments/evaluation/` |

## 2. Core Architecture

Each pillar is stated as *problem → design → implementation → existing evidence → limitation*. The
evidence numbers are historical (frozen at their own commit and task set); §5 explains what they may
and may not be used for.

### 2.1 Context Engineering

*Problem.* Dependency outputs are injected in completion order, so the same logical context produces
a different prompt every run, and prompt-cache reuse becomes accidental.

*Design.* Logical context (`ContextBlock` / `ContextRef`) is separated from physical rendering:
a selector decides **what** a task receives, a renderer decides **how** it is ordered —
stable system prefix → stable role → stable task prefix → context blocks → dynamic tail. A transform
(dedup) can sit between them; placement decides which agent executes which task by context locality;
the store is rebuilt deterministically from a checkpoint on resume.

*Implementation.* `context/models.py`, `context/store.py`, `context/selectors.py`
(`TaskScopedSelector`), `context/renderer.py` (`StablePromptRenderer`), `context/transform.py`
(`ContextPilotDedupTransform`), `placement/`, `durability/context_rebuild.py`.

*Evidence.* Stable rendering makes the layout hash and the full prompt hash identical under an
inverted completion order, while the naive renderer does not (S6 Table A). Dedup cuts prompt tokens
by up to 44.0 % at ~50 % exact duplicates (S6 Table B). Context-locality placement cuts logical
handoff by 50 % / 66.7 % / 80 % at chain length 2 / 3 / 5 (S6 Table C).

*Limitation.* A shorter prompt is not a cheaper prompt: warm cache-miss reduction was 11.1 % at
large scale but **negative** at small and medium scale, so the cache benefit is workload-dependent
and is not claimed. Dedup is off by default.

### 2.2 Tool Engineering

*Problem.* Exposing every tool costs schema tokens on every request, and a large surface makes tool
choice worse.

*Design.* One registry holds every executable tool. Only core tools enter the native `tools[]`
surface; everything else (project tools, MCP tools) is discoverable through `tool_search` and usable
only after `dispatch_action` discloses it for the run. The native surface is hashed and must not
change between turns.

*Implementation.* `tools/runtime.py`, `tools/catalog.py`, `tools/search.py` (keyword and semantic),
`tools/state.py` (`RunToolState`), `tools/dispatch.py`, MCP bridge in `tools/runtime.py`.

*Evidence.* On a fixed 24-tool catalog and 24 queries: keyword Recall@1 0.792 vs semantic 0.875,
Recall@3 0.958 vs 1.000, MRR 0.861 vs 0.938. Native schema 8 558 → 4 207 characters; tool-surface
prompt 2 599 → 1 399 tokens (−46.2 %). Disclosure survives a task-level crash/resume.

*Limitation.* The 46.2 % figure is a **tool-surface microbenchmark**, not an end-to-end coding cost
reduction, and the shorter surface did not improve the cache-hit ratio in that sample. Recall@3 =
1.000 holds for that catalog and query set only.

### 2.3 Multi-Agent Coordination

*Problem.* Every worker multiplies context and budget, and a decomposition can silently degrade the
result.

*Design.* A workflow is compiled to AnyCode task specs; each worker is constructed by
`ScopedWorkerFactory` with a trusted `ExecutionContext` (run/task/worker/profile) rather than a
prompt claim. One `RunBudgetState` is shared by the whole run and admitted before every request.
Cross-worker transfer is either upstream task-output injection or a dual-channel handoff. Delegation
is a separate, evidence-gated decision.

*Implementation.* `workflow.py`, `workers/factory.py`, `workers/store.py`, `handoff.py`, `budget.py`,
`workers/routing.py` (selective delegation).

*Evidence.* Paired comparison over 72 runs: single worker 28/36 success vs static multi 3/36, prompt
tokens 6.59×, cost 4.12×, p50 latency 1.94×. Later stages reproduced the direction (G3-4a 6/6 vs
2/6; R0 5/6 vs 2/6; R0.5 forced-positive admission 6/6 admitted, still +40 % cost). Parallelism is
real — 20/20 multi-worker runs overlapped in wall clock — but the net benefit is not.

*Limitation.* Static delegation has a quality, cost and latency problem on the measured task
families; the selective router has never admitted a live `DELEGATE_PARALLEL` in a benchmark, so its
generalisation benefit is unproven. Single worker is the default.

### 2.4 Reliable Execution

*Problem.* A model response can partially execute; a process can die with a side effect in flight;
"the worker said it succeeded" is not evidence.

*Design.* Every project action passes one gate (`ALLOW` / `DENY` / `REQUIRE_HUMAN`) bound to a digest
of the tool implementation and its input. A durable SQLite ledger is written **before** the side
effect and closed after it; an interrupted action becomes `UNCERTAIN` and is never replayed
automatically. A whole model response can be suspended before any of its tools run, and approvals are
durable. Sandbox binding pins one run to one sandbox and reattachment is explicit, never automatic.

*Implementation.* `actions/gate.py`, `actions/policy.py`, `actions/binding.py`, `ledger/`,
`sandbox/binding.py`, `sandbox/reattachment.py`, `suspension/`, `approvals/`, `trusted_recovery.py`.

*Evidence.* Deterministic control plane: 24 action requests → 15 ALLOW / 1 DENY / 8 REQUIRE_HUMAN,
0 bypasses, 0 stale-approval reuses. 30-task benchmark: 0/90 unauthorized side effects, 0 host
isolation violations. Tool-error semantics: oracle 2/4 → 4/4, `side_effect_unknown` 2 → 0, 3
`UNCERTAIN` records, 0 replays.

*Limitation.* Whole-response preflight is a barrier before execution, not transaction atomicity;
ownership is a local OS lock, not a distributed lease; and no coding composition enables
checkpointing, so crash recovery is accountability, not resumption.

### 2.5 Observability and Evaluation

*Problem.* The upstream task result is an incomplete record, and a grader that runs next to the agent
cannot be trusted.

*Design.* A recorder writes `action_proposed` before execution and the observation after, with input
and result digests, and a worker-terminal event for workers that never return. Tokens, cache hit/miss,
latency and cost are captured per call. Deliverables are checked by a deterministic contract engine
(observation only), and hidden tests run in a disposable network-disabled sandbox with the oracle's
own copy of the fixture.

*Implementation.* `trajectory/recorder.py`, `trajectory/reconcile.py`, `metrics/collector.py`,
`verification/engine.py`, `experiments/evaluation/sandboxed_oracle.py`.

*Evidence.* Two records of the same 30 runs: `result.json` captured 365 actions, the trajectory 591 —
38 % missing, concentrated in failed workers. After the fix, `input_digest_coverage = 1.00` over 32
runs and 111 actions were recovered. Verification: 0 false positives on 66 deliverables at
8.1 ms–5.0 s and 0 model tokens. Oracle isolation: 28/28 identical scores at ~6 s and 0 model tokens.

*Limitation.* Verification recall is thin (23 TP / 23 FN) and it never blocks a run; 28/28 is a
statement about those historical workspaces, not a security proof of the oracle.

## 3. Runtime Architecture

```
application     atlas CLI · run registry · HTTP projection · in-process coding session
                        │
coordination    workflow compiler · scoped workers · handoff · shared budget · delegation (opt-in)
                        │
execution       tool runtime · action gate · durable ledger · sandbox binding · trajectory · metrics
                        │
reliability     suspension · approvals · manifest · context rebuild · verification (evaluation only)
                        │
AnyCode 0.9.0   agent loop · DAG scheduler · checkpoints · providers · tool executor · run store
```

The three entry points are **not** the same composition, and a claim must name its entry point:

| | Workflow (`atlas run --mode workflow`) | App coding (`--mode coding`) | Evaluation (`experiments.evaluation.cli`) |
|---|---|---|---|
| Context plane (selector + stable renderer) | **on** | opt-in (`context.policy="task_scoped"`) | opt-in (same switch); ran live 2026-11-14 |
| Tool search + progressive disclosure | no tools at all | **on** | **on** |
| Action gate + ledger | **off** | **on** | **on** |
| Sandbox | none | **on** | **on** |
| Durable suspension / approval | off | **on** | off |
| AnyCode checkpoint + resume | **on** (library API) | **off** | **off** |
| Verification / isolated oracle | none | none | **on** |

Full call chains, the recovery paths and the per-mechanism matrix:
[`docs/runtime-entrypoints.md`](docs/runtime-entrypoints.md). The AnyCode boundary is
[`docs/anycode-delta.md`](docs/anycode-delta.md).

## 4. Quick start

Windows-native `.venv`, Python 3.12 (`requires-python = ">=3.12"`):

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev,sandbox,app]"
```

Offline checks — no API key, no sandbox service (this is what CI runs):

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
.\.venv\Scripts\python.exe -m pyright
.\.venv\Scripts\atlas.exe --help
.\.venv\Scripts\python.exe examples\m1_demo.py      # deterministic tool-runtime demo, no provider
```

The evaluation layer needs one more extra, because the Inspect AI adapter is imported at module
scope:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[evaluation]"
.\.venv\Scripts\python.exe -m experiments.evaluation.cli list    # 30 benchmark tasks
```

A real coding run needs a DeepSeek key and a self-hosted OpenSandbox:

```powershell
# .env (from .env.example)
#   DEEPSEEK_API_KEY=...
#   ATLASOS_SANDBOX_ENDPOINT=localhost:8080
docker build -t atlas-uat-python:3.12 experiments/fixtures/uat_image

.\.venv\Scripts\atlas.exe run "fix the CSV quoting bug in csv_parser.py" --mode coding
.\.venv\Scripts\atlas.exe status <run_id>
.\.venv\Scripts\atlas.exe logs <run_id> --follow
```

`atlas run --mode coding` requires a **UAT-shaped workspace** under `experiments/fixtures` containing
`csv_parser.py` (default: `experiments/fixtures/uat_repo`) and a reachable real sandbox provider;
there is no fake fallback. `--mode workflow` needs the provider key but no sandbox. One benchmark
sample through the full runtime:

```powershell
.\.venv\Scripts\python.exe -m experiments.evaluation.cli run --sample bugfix-001-slugify --repetitions 1
```

## 5. Evaluation

Five kinds of evidence, deliberately separated:

| Kind | What it is | Where |
|---|---|---|
| Historical microbenchmark | fixed tool catalog / context-scaling measurements, not end-to-end tasks | `experiments/results/` (M1, S6) |
| Deterministic mechanism test | no model: control plane, ledger, suspension, sandbox, oracle isolation | `tests/`, `experiments/results/` |
| Live model evaluation | real DeepSeek runs with an independent oracle | `docs/evaluation-summary.md` |
| Current candidate regression | offline suite on this branch: 951 passed, 6 skipped; ruff, format and pyright clean; 30 benchmark tasks load | `docs/final-4-context-restoration-report.md` §G |
| Final holdout evaluation | **pending** — not designed, not run | `docs/final-roadmap.md` |

Selected historical results, each with its own conditions (never add these up — see
[`docs/evaluation-summary.md`](docs/evaluation-summary.md) §8):

| Experiment | Result |
|---|---|
| 30-task coding benchmark (90 runs) | 84/90 success, 0/90 unauthorized side effects, 0 host-isolation violations |
| Same benchmark vs matched AnyCode baseline (25 runs) | 21/25 vs 20/25 — **not** a capability claim |
| Single vs static multi-worker (72 paired runs) | 28/36 vs 3/36; cost 4.12×, p50 latency 1.94× |
| Tool discovery, semantic retrieval (24 tools) | Recall@1 0.875 / Recall@3 1.000 / MRR 0.938; surface 2 599 → 1 399 tokens |
| Context plane, dedup scaling | up to 44.0 % prompt-token cut; no matching cache-miss win |
| Deliverable verification (66 artifacts) | 0 false positives, weak recall, 0 model tokens |
| Oracle sandbox isolation (28 workspaces) | 28/28 identical scores, ~6 s, 0 model tokens |
| Execution-trace integrity (32 runs) | input-digest coverage 1.00; 111 actions recovered |
| Ownership enforcement (32 runs) | enforce 1/16 vs baseline 7/16 — **negative**, default off |

We do not claim that AtlasRuntime is a more capable agent than AnyCode, that multi-worker is better,
or that cache-aware context reduces cost. Each was tested and not supported.

## 6. Design trade-offs

- **Single worker by default.** Four independent comparisons put static decomposition below a single
  worker on success, at 1.4–4.1× cost. The router is kept because the question is still open, not
  because it is already answered.
- **The router is evaluation-only until it proves itself.** It can reject a bad decomposition, not
  improve one; admitting it into the product path without evidence would be a default nobody asked
  for.
- **Most context mechanisms are optional.** Stable rendering is on where the context plane is used;
  dedup and context-locality placement stay behind config because their benefit is workload-specific.
- **The oracle runs in its own sandbox.** Host grading made failures ambiguous and let benchmark code
  touch host paths.
- **Verification never blocks.** A detector with 0 false positives and weak recall is a report, not a
  gate.
- **Ownership enforcement is opt-in and off.** It blocked `python3 -m pytest` and scored 1/16 against
  7/16.
- **Business-domain adaptation is closed.** A commerce adapter was built and verified end-to-end,
  then the track was stopped: a second domain tests the adapter, not the runtime.

## 7. Development

- [`docs/architecture-design-contract.md`](docs/architecture-design-contract.md) — the design
  baseline: positioning, two cores, one extension, claim boundaries, owner-approval rule.
- [`AGENTS.md`](AGENTS.md) — development contract, invariants, verification commands, Git rules.
- [`docs/README.md`](docs/README.md) — ACTIVE / EVIDENCE / ARCHIVED document index.
- [`docs/project-status.md`](docs/project-status.md) — baseline commit, per-pillar status, mechanism
  status, closed directions.
- [`docs/final-roadmap.md`](docs/final-roadmap.md) — Track A/B/C, holdout principle, stop conditions.
- [`docs/runtime-entrypoints.md`](docs/runtime-entrypoints.md) — real call chains and capability
  matrix.
- [`docs/limitations.md`](docs/limitations.md) — every known constraint.

`src/` and `tests/` are the runtime; `experiments/` is the evaluation harness and never a dependency
of `src/`.

## 8. Provenance and Limitations

| Component | Version / licence | Role |
|---|---|---|
| [AnyCode](https://github.com/Quantlix/anycode) | `anycode-py==0.9.0`, MIT | Agent loop, DAG scheduler, checkpoints, providers, tool executor, run store |
| [OpenSandbox](https://github.com/opensandbox/opensandbox) | `opensandbox>=1.2,<2`, Apache-2.0 | Self-hosted sandbox backend |
| [Daytona](https://github.com/daytonaio/daytona) | optional extra | Alternative sandbox backend |
| [ContextPilot](https://github.com/EfficientContext/ContextPilot) | `contextpilot==0.5.0`, optional extra | Dedup/reorder transform (off by default) |
| [Inspect AI](https://github.com/UKGovernmentBEIS/inspect_ai) | `inspect-ai`, MIT, evaluation extra | External evaluation harness |
| [DeepSeek API](https://platform.deepseek.com) | OpenAI-compatible | Reference provider and prompt-cache metrics |

Acknowledgements and licence texts: [`THIRD_PARTY.md`](THIRD_PARTY.md). Upstream capabilities are
attributed to AnyCode everywhere in this repository: [`docs/anycode-delta.md`](docs/anycode-delta.md).
Known constraints: [`docs/limitations.md`](docs/limitations.md).

Naming: the project is **AtlasRuntime** (`Melon235/AtlasRuntime`). The Python distribution and import
package (`atlasos`), the CLI (`atlas`), the `ATLASOS_*` environment variables and historical run
identifiers keep their original names; renaming them is a separate migration.
