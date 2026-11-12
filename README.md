# AtlasRuntime

**Reliable execution runtime for long-horizon agent workflows.**

AtlasRuntime runs an AI coding agent inside a controlled sandbox and makes the run *survivable*:
every action is authorized before it executes, recorded after it executes, and recoverable if the
process dies or the outcome is unknown. It is built on top of
[AnyCode 0.9.0](https://github.com/Quantlix/anycode) and adds the execution-control layer that an
agent runtime needs before anyone trusts it with a real repository.

- Application domain: autonomous software engineering tasks (multi-step edits, multi-module changes,
  tool execution, interruption/resume, deliverable verification).
- Not another agent framework, not another coding CLI, not a serving engine.
- Evaluation: a 30-task coding benchmark with an independent oracle that runs in its own sandbox.

Documentation: [architecture](docs/architecture.md) ·
[AnyCode contribution delta](docs/anycode-delta.md) ·
[design decisions](docs/design-decisions.md) ·
[evidence](docs/evaluation-summary.md) ·
[limitations](docs/limitations.md) ·
[research history](docs/research-history.md)

---

## 1. The problem

An agent that can edit files and run shell commands is easy to build and hard to operate. The failure
modes we actually observed while running multi-hour coding workloads:

| Failure | What we measured |
|---|---|
| A model response executes partly, then a tool needs a human | No upstream seam between "model responded" and "tools ran" |
| The process dies with a command in flight | The command's effect is unknown; retrying may duplicate it |
| A tool result is lost from the run record | `result.json` held 365 of 591 recorded actions (**38 % missing**, concentrated in failed workers) |
| N workers each get the full token budget | Upstream budget is per agent, so a 3-worker run can spend 3× |
| A worker writes another worker's files | No cross-worker write protection at the tool boundary |
| The grader runs next to the agent | Host code execution during grading, and ambiguous failures |
| "It succeeded" is the worker's own claim | 8 of 36 multi-worker runs had every worker report success while the oracle failed |

The repository is the record of what we changed in response, including what did **not** work.

## 2. Architecture

```
application        atlas CLI · run registry · HTTP projection
                          │
orchestration      AtlasRuntime facade · workflow compiler · scoped workers · delegation (opt-in)
                          │
execution runtime  tool runtime · action safety gate · execution ledger · sandbox binding · budget
                          │
reliability        suspension · approvals · durable manifest · trajectory · reconciler
                          │
AnyCode 0.9.0      agent loop · DAG scheduler · checkpoints · providers · tool executor · run store
```

The boundary is explicit: AnyCode owns the agent loop and scheduling; AtlasRuntime owns
authorization, durable action state, sandbox lifecycle, suspension and evidence. The full mapping is
in [`docs/anycode-delta.md`](docs/anycode-delta.md).

### Main execution path

```
task text → run identity → scoped worker (trusted identity) → AnyCode agent loop
   → tool call → action safety gate → execution ledger → sandbox execution
   → trajectory + budget accounting → task result → optional verification → artifact
```

Recovery, multi-worker scheduling and the evaluation oracle are side paths, not part of the normal
chain. `docs/architecture.md` §3–§9 gives the entry function, key classes, persisted state and tests
for each path.

## 3. Core design

### Controlled tool execution

*Problem* — the model sees every tool, and nothing decides whether *this* call, from *this* worker,
with *this* argument digest, may run.

*Design* — one registry holds every executable tool; the model sees `tool_search` and
`dispatch_action`; a tool must be disclosed before dispatch. Every dispatch passes
`ActionSafetyGate`, which returns `ALLOW`, `DENY` or `REQUIRE_HUMAN` from policy, reviewer,
external policy and approval state, and binds the decision to a digest of the tool implementation and
input.

*Mechanism* — `tools/runtime.py`, `tools/catalog.py`, `tools/state.py`, `actions/gate.py`.

*Evidence* — deterministic control plane: 24 action requests → 15 ALLOW / 1 DENY / 8 REQUIRE_HUMAN,
0 bypasses, 0 hard-deny overrides. 30-task benchmark: 0/90 unauthorized side effects.

*Limitation* — the gate judges a tool call, not intent; shell text classification is not OS
isolation.

### Reliable state and recovery

*Problem* — idempotency exists upstream, but nothing records *what was authorized* and *what
happened*, so an interrupted action cannot be classified.

*Design* — a SQLite action ledger keyed by run/task/worker/tool/input digest, written by a bridge
that sequences gate → execute → outcome. Unknown outcomes become `UNCERTAIN`, never an automatic
retry. Approvals and suspensions are durable, and a whole model response is suspended **before** any
of its tools execute.

*Mechanism* — `ledger/*`, `suspension/*`, `approvals/*`, `tools/conversation.py`.

*Evidence* — G3-1: `side_effect_unknown` workers 2 → 0, oracle 2/4 → 4/4, 3 `UNCERTAIN` records,
0 replays. Integrated scripted run: 6 suspensions, 13 resume revalidations, 0 stale approvals.

*Limitation* — settlement requires the operator harness; the CLI refuses to reattach a sandbox on
its own.

### Observable execution

*Problem* — after a failed run you need to know which action produced which effect.

*Design* — the recorder writes `action_proposed` before execution and `tool_observed` after, with
input and result digests, plus a worker lifecycle terminal for workers that never return.

*Mechanism* — `trajectory/recorder.py`, `trajectory/models.py`, `trajectory/reconcile.py`.

*Evidence* — R1-0 showed the upstream task result loses 38 % of actions; after the change, trace
coverage was `1.00` over 32 runs and 111 actions were recovered that the result object had dropped.

*Limitation* — evidence writes are best-effort and not fsync'd.

### Shared resource control

*Problem* — budget per agent means a multi-worker run multiplies the allowance.

*Design* — one `RunBudgetState` per run, admitted before each request, with a learned token ratio.
A rejection is converted into a real `budget_exceeded` failure instead of an empty success.

*Mechanism* — `budget.py`, `tools/conversation.py::AtlasToolAgent._apply_budget_outcome`.

*Evidence* — G2a: 0/4 vs 2/4 overruns, total tokens −24 %, request accounting 95/95/0. Success rate
unchanged (both arms 0/4), so this is a *capability*, not a speed-up.

*Limitation* — the estimate is not an upper bound; the lock is process-local.

### Verified deliverables and isolated grading

*Problem* — "the agent said it finished" and "the grader ran next to the agent" are both unusable.

*Design* — deterministic contract checks on produced files returning `PASS/FAIL/UNKNOWN`
(observation only, never a gate), and hidden tests executed inside a disposable network-disabled
sandbox.

*Mechanism* — `verification/*`, `experiments/evaluation/sandboxed_oracle.py`.

*Evidence* — 66 verified deliverables with **0 false positives** (8.1 ms static, 5.0 s dynamic,
0 model tokens); 28/28 historical workspaces re-graded identically at ~6 s and 0 model tokens.

*Limitation* — public-contract recall is thin (23 TP / 23 FN), so verification reports, it does not
block.

## 4. Quick start

Windows-native `.venv`, Python 3.12:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev,sandbox,app]"
```

Offline checks (these need no API key and no sandbox):

```powershell
.\.venv\Scripts\python.exe -m pytest                 # 895 tests
.\.venv\Scripts\atlas.exe --help                     # CLI surface
.\.venv\Scripts\python.exe examples\m1_demo.py       # deterministic tool-runtime demo
```

The evaluation layer needs its own extra:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[evaluation]"
.\.venv\Scripts\python.exe -m experiments.evaluation.cli list   # 30 benchmark tasks
```

A real coding run needs a DeepSeek key and a self-hosted OpenSandbox:

```powershell
# .env
#   DEEPSEEK_API_KEY=...
#   ATLASOS_SANDBOX_ENDPOINT=localhost:8080
docker build -t atlas-uat-python:3.12 experiments/fixtures/uat_image

.\.venv\Scripts\atlas.exe run "fix the CSV quoting bug in csv_parser.py" --mode coding
.\.venv\Scripts\atlas.exe status <run_id>
.\.venv\Scripts\atlas.exe logs <run_id>
```

One benchmark sample through the full runtime:

```powershell
.\.venv\Scripts\python.exe -m experiments.evaluation.cli run --sample bugfix-001-slugify --repetitions 1
```

`atlas run --mode workflow` runs the workflow path (in-process `AtlasRuntime`) and needs the same
provider key but no sandbox.

## 5. Evaluation

Headline numbers, each with its own conditions (full detail and comparability warnings in
[`docs/evaluation-summary.md`](docs/evaluation-summary.md)):

| Experiment | Result |
|---|---|
| 30-task coding benchmark (90 runs, `deepseek-flash`) | **84/90 success**, 0/90 unauthorized side effects, 0 host-isolation violations |
| Same benchmark vs matched AnyCode baseline (25 runs) | 21/25 vs 20/25 — **not** a capability claim |
| Single vs static multi-worker (E1, 72 runs) | 28/36 vs 3/36; cost 4.12×, latency 1.94× |
| Deliverable verification (66 artifacts) | 0 false positives, 0 model tokens |
| Oracle sandbox isolation (28 workspaces) | 28/28 identical scores, ~6 s, 0 model tokens |
| Execution trace integrity (32 runs) | input digest coverage 1.00; 111 actions recovered |
| Ownership enforcement (32 runs) | enforce 1/16 vs baseline 7/16 — **negative**, default off |

We do not claim that AtlasRuntime is a better agent than AnyCode, that multi-worker is faster, or
that cache-aware context reduces cost. Each of those was tested and not supported.

## 6. Design trade-offs

- **Single worker by default.** Four independent comparisons put static multi-worker decomposition
  below a single worker on task success, at 1.4–4.1× cost. Parallelism is real (20/20 runs
  overlapped); the net benefit is not. See [design decisions §D3](docs/design-decisions.md).
- **Context engineering is kept, the claim withdrawn.** Dedup cut prompt tokens up to 44 % while
  warm cache misses went *up* at high duplicate ratio. A shorter prompt is not a cheaper prompt.
- **Ownership enforcement is opt-in.** It blocked `python3 -m pytest` and scored 1/16 against 7/16.
- **The oracle runs in its own sandbox.** Host grading made failures ambiguous and let benchmark code
  touch host paths.
- **No business-domain adapters.** A commerce benchmark adapter was built and verified end-to-end,
  then the track was stopped: a second domain tests the adapter, not the runtime.
- **Verification never blocks.** A detector with 0 false positives and weak recall is a report, not a
  gate.

## 7. Limitations

Single host, SQLite state, no cross-host recovery, no CPU/memory quotas, text-level shell
classification, small sample sizes, one model family, and several mechanisms with deterministic
evidence only. The complete list is [`docs/limitations.md`](docs/limitations.md).

## 8. Provenance

| Component | Version / license | Role |
|---|---|---|
| [AnyCode](https://github.com/Quantlix/anycode) | `anycode-py==0.9.0`, MIT | Agent loop, DAG scheduler, checkpoints, providers, tool executor, run store |
| [OpenSandbox](https://github.com/opensandbox/opensandbox) | `opensandbox>=1.2,<2`, Apache-2.0 | Self-hosted sandbox backend |
| [Daytona](https://github.com/daytonaio/daytona) | optional extra | Alternative sandbox backend |
| [ContextPilot](https://github.com/EfficientContext/ContextPilot) | `contextpilot==0.5.0`, optional extra | Dedup/reorder transform (off by default) |
| [Inspect AI](https://github.com/UKGovernmentBEIS/inspect_ai) | `inspect-ai`, MIT, evaluation extra | External evaluation harness |
| [DeepSeek API](https://platform.deepseek.com) | OpenAI-compatible | Reference inference provider and cache metrics |

Third-party acknowledgements and license texts: [`THIRD_PARTY.md`](THIRD_PARTY.md).
AnyCode capabilities are attributed to AnyCode everywhere in this repository; the per-module delta
is [`docs/anycode-delta.md`](docs/anycode-delta.md).

Naming: the project is **AtlasRuntime**. The repository (`Melon235/AtlasOS`), the Python package
(`atlasos`), the CLI (`atlas`), `ATLASOS_*` environment variables and historical run IDs keep their
original names; renaming them is a separate migration.

## 9. Repository layout

```
src/atlasos/
  app/          CLI, application service, HTTP projection, coding session composition
  actions/      action catalog, policy, safety gate, reviewer
  ledger/       durable action ledger, bridge, recovery classification
  tools/        tool runtime, catalog, search, dispatch, conversation seam
  sandbox/      binding store, ownership, reattachment, workspace tools
  approvals/    persistent approvals and resume revalidation   (approvals/)
  suspension/   durable suspension store and coordinator
  trajectory/   recorder, payloads, reconciler
  workers/      scoped worker factory, registry, delegation router
  context/      logical context blocks, selectors, stable renderer
  verification/ deterministic deliverable contracts
  durability/   session sidecar manifest, context rebuild
experiments/
  evaluation/   benchmark tasks, runner, oracle, sandboxed oracle, analysis
  fixtures/     UAT repo, sandbox image, acceptance oracle
  configs/      baseline, benchmark, coding-session configuration
  evidence/     sanitized aggregates only
  results/      published reports and figures
docs/           architecture, delta, decisions, evidence, limitations, stage history
tests/          unit + integration (offline; live tests are opt-in)
```
