# AtlasOS

AtlasOS is an experimental AgentOS-style runtime for long-horizon multi-agent workloads.

It does not reimplement an agent framework, an inference engine, a KV-cache manager, or a
distributed OS. It extends a lightweight multi-agent runtime (AnyCode) and studies one question:

> How should a multi-agent runtime jointly manage task execution and model context so that it
> minimizes cache-miss prefill and runtime cost rather than merely minimizing prompt length?

V1 builds on:

| Component | Role | Version |
|---|---|---|
| [AnyCode](https://github.com/Quantlix/anycode) | Multi-agent runtime base | `anycode-py==0.9.0` |
| [ContextPilot](https://github.com/EfficientContext/ContextPilot) | Context reorder/dedup backend | `contextpilot==0.5.0` |
| DeepSeek API | Inference backend + prompt-cache metrics | OpenAI-compatible |
| DeepSeek Harness | Development tool only, not a runtime dependency | 鈥?|

Provenance and licenses are recorded in [THIRD_PARTY.md](THIRD_PARTY.md).

## Status

Stage 0 (bootstrap), Stage 1 (runtime baseline), Stage 2 (explicit context runtime + stable
rendering), Stage 3 (ContextPilot dedup transform + reorder characterization), Stage 4
(context-locality placement planning), Stage 5 (durable session, task-level recovery, and
deterministic context reconstruction), and Stage 6 (final evaluation and ablation) are complete.
**AtlasOS V1 is frozen at Stage 6.** No Stage 7 has been started.

V1 delivers a working integrated runtime and an honest evaluation: stable rendering removes
layout dependence on completion order; ContextPilot dedup reduces prompt size at millisecond CPU
cost but does not produce proportional warm cache-miss reduction; context-locality placement
reduces logical handoff as chains grow; placement and dedup retain independent effects; durable
recovery preserves unfinished-task context.

- [Architecture](docs/architecture.md)
- [Stage 0/1 specification](docs/stages/00-01-bootstrap-and-baseline.md)
- [Stage 0 verification notes](docs/stages/00-verification-notes.md)
- [Stage 1 baseline notes](docs/stages/01-baseline-notes.md)
- [Stage 2 context runtime notes](docs/stages/02-context-runtime-notes.md)
- [Stage 3 ContextPilot notes](docs/stages/03-contextpilot-notes.md)
- [Stage 3 API verification](docs/stages/03-contextpilot-verification-notes.md)
- [Stage 4 placement notes](docs/stages/04-placement-notes.md)
- [Stage 5 recovery notes](docs/stages/05-recovery-notes.md)
- [Stage 5 resume verification](docs/stages/05-recovery-verification-notes.md)
- [Stage 5 formal results](experiments/results/stage5-baseline.md)
- [**Stage 6 final evaluation**](docs/stages/06-final-evaluation.md)
- [**Stage 6 formal results**](experiments/results/stage6-final.md)
- [**V2 M1 tool runtime and discovery**](docs/stages/v2-m1-tool-runtime.md)
- [**V2 M1 formal results**](experiments/results/v2-m1-results.md)
- [**V2 M1 acceptance matrix**](docs/stages/v2-m1-acceptance.md)
- [**V2 M2 scoped worker construction**](docs/stages/v2-m2-scoped-worker.md)
- [**V2 M2 worker migration**](docs/stages/v2-m2-scoped-worker-migration.md)
- [**V2 M2 acceptance matrix**](docs/stages/v2-m2-acceptance.md)
- [**V2 M2 worker results**](experiments/results/v2-m2-workers-results.md)
- [**V2 M3 global action safety**](docs/stages/v2-m3-action-safety.md)
- [**V2 M3 migration**](docs/stages/v2-m3-migration.md)
- [**V2 M3 acceptance matrix**](docs/stages/v2-m3-acceptance.md)
- [**V2 M3 formal results**](experiments/results/v2-m3-results.md)
- [**M1–M3 control-plane E2E**](docs/stages/control-plane-e2e.md)
- [**M1–M3 control-plane results**](experiments/results/control-plane-results.md)
- [**V2 M4 durable action ledger**](docs/stages/v2-m4-durable-ledger.md)
- [**V2 M4 failure model**](docs/stages/v2-m4-failure-model.md)
- [**V2 M4 idempotency guarantees**](docs/stages/v2-m4-idempotency.md)
- [**V2 M4 acceptance matrix**](docs/stages/v2-m4-acceptance.md)
- [**V2 M4 formal results**](experiments/results/v2-m4-results.md)
- [**M1–M4 reliability experiment**](docs/stages/m1-m4-reliability.md)
- [**M1–M4 reliability results**](experiments/results/v2-m1-m4-reliability-results.md)
- [**M4.1 SQLite process durability**](docs/stages/m4-sqlite-process-durability.md)
- [**M4.1 SQLite process results**](experiments/results/v2-m4-sqlite-process-results.md)
- [**V2 M5 project sandbox**](docs/stages/m5-project-sandbox.md)
- [**V2 M5 formal results**](experiments/results/v2-m5-project-sandbox-results.md)
- [**V2 real Atlas Sandbox provider results**](experiments/results/v2-real-sandbox-results.md)
- [**V2 integrated system verification**](docs/verification/v2-system-verification.md)
- [Architecture decision records](docs/decisions/)
- [Repository constitution](AGENTS.md)

## V2 M1 — Tool Runtime and Progressive Discovery

**V2 M1 is complete at clean commit `fc9aa6d`.** V1 remains frozen and unchanged; M1 adds a tool
runtime layer around AnyCode's executor.

```text
Core tools (stable native tools[])
        +
tool.search -> Top-3 schemas in conversation tail
        +
RunToolState (disclosed ids)
        +
dispatch_action -> AtlasDispatchExecutor -> AnyCode ToolExecutor -> real tool / MCP
```

| Area | Result |
|---|---|
| One registry | 8 core + all searchable/MCP tools in one `ToolRegistry` |
| Native surface | 8 core tools; external schemas never enter `tools[]`; hash stable across turns |
| Semantic discovery (24-tool catalog) | Recall@1 0.875, Recall@3 **1.000**, MRR 0.938 |
| Schema footprint | 4,207 vs 8,558 chars; live prompt 1,399 vs 2,599 tokens (**−46.2%**) |
| Undisclosed execution | rejected with `tool_not_disclosed`; unavailable disclosed tool gives `tool_unavailable` |
| Recovery | disclosure set + turn-boundary conversation survive crash/resume |

Provider cache is warm-prone and uncontrolled; M1 claims schema-footprint reduction, not a
cache-hit improvement. Full details: [M1 evaluation](docs/stages/v2-m1-tool-runtime.md),
[M1 formal results](experiments/results/v2-m1-results.md).

## V2 M2 — Scoped Worker Construction

**M2 builds focused workers. M3 authorizes their actions.** V1 remains frozen; M1 tool semantics
are unchanged.

```text
trusted WorkerProfile + Planner assignment
→ validate profile/assignment
→ new child ExecutionContext (delegation cleared)
→ bounded worker metadata + separate task context
→ deterministic trusted/untrusted prompt render
→ AnyCode AgentConfig
→ persist WorkerInstanceRecord
```

Planner can submit only `profile_id`, `task`, and optional `context`. The trusted host owns the
four immutable profiles (`research_worker`, `coding_worker`, `data_worker`, `general_worker`),
including base tools, coarse `operating_scope`, max turns, and `can_delegate=false`.

The child gets a distinct AnyCode `ExecutionContext` preserving trusted run metadata and copying
safe parent attributes. Task text is stored outside the context and rendered under explicit
untrusted delimiters; it cannot change the profile, scope, tools, delegation flag, or context.
Workers use the one M1 registry; `base_tools` are starting tools, not final authority. Every
worker action still passes the M3 `ActionSafetyGate`.

Old capability authorization (`RunAuthority`, scopes, `GrantIssuer`, capability router/reviewer/
policy/HITL) was removed from active code. See [design](docs/stages/v2-m2-scoped-worker.md),
[migration inventory](docs/stages/v2-m2-scoped-worker-migration.md), and
[acceptance matrix](docs/stages/v2-m2-acceptance.md).

## V2 M3 — Global Action Safety Gate

**M3 authorizes concrete tool actions; it does not grant capability.** Every configured real
tool call passes one pre-invoke gate around the existing AnyCode `ToolExecutor`:

```text
validate input
→ AnyCode hard security
→ ActionCatalog / resource
→ deterministic ActionPolicy
→ optional scope reviewer
→ external policy if configured
→ digest-bound HITL if required
→ AnyCode idempotency claim
→ tool.execute
```

The gate covers native, searchable, MCP, custom, batch, main-agent, and worker calls. The
`dispatch_action` control wrapper is ALLOWed, but its target is re-authorized before execution.
Unknown tools are never guessed safe; the default is REQUIRE_HUMAN. Hard AnyCode deny, local
policy DENY, and external policy DENY cannot be overridden by reviewer or human approval.

M3 is opt-in at runtime construction (`AtlasToolRuntime(action_policy=...)`) to preserve V1/M1/M2
behavior. It does not add a second executor, registry, approval engine, policy engine, OPA/Rego,
or sandbox. See [design](docs/stages/v2-m3-action-safety.md),
[migration](docs/stages/v2-m3-migration.md), and [acceptance](docs/stages/v2-m3-acceptance.md).

## V2 M4 — Durable Action Ledger & Idempotency

**M4 records and recovers execution; AnyCode owns deduplication.** It answers whether an
authorized side effect has already executed, can be safely replayed, conflicts, or is uncertain
after a crash.

```text
M3 authorized final input
→ M4 ledger IN_PROGRESS
→ AnyCode IdempotencyStore.claim()
→ AnyCode ToolExecutor executes
→ AnyCode complete()
→ M4 ledger COMPLETED / UNCERTAIN
```

The ledger stores lifecycle metadata and digests, not raw arguments, results, secrets, or email
bodies. `action_id`, `attempt_id`, trusted idempotency key, and M3 call identity remain separate.
M4 adds no claim engine, replay lock, replacement executor, or second registry. M3 DENY/HITL
reject consume no claim and produce no execution record.

Only side-effecting tools enter M4 semantics. Safe replay is AnyCode's; M4 never caches read
results. Provider-enforced idempotency can be effectively-once where the provider honors the same
key; local-only effects can become UNCERTAIN after a crash and never auto-retry. Restart-safe
SQLite idempotency is verified across independent process boundaries (M4.1).

See [design](docs/stages/v2-m4-durable-ledger.md), [failure model](docs/stages/v2-m4-failure-model.md),
[idempotency](docs/stages/v2-m4-idempotency.md), and [acceptance](docs/stages/v2-m4-acceptance.md).

## V2 M5 — Project Sandbox Binding

**M5 binds one project task to one AnyCode-managed sandbox; it does not implement a second sandbox
runtime.** The same six logical workspace tools (`file_read`, `file_write`, `file_edit`, `grep`,
`list_files`, `bash`) keep one name/schema/registry but route their physical backend to the same
`SandboxHandle` at `/workspace/project`.

```text
trusted task config + clean Git repo
→ one-task single-flight binding
→ AnyCode SandboxProvider.create
→ Git bundle clone inside sandbox
→ M3 authorizes every logical tool call
→ M4 owns side-effect idempotency
→ ToolDefinition.execute → sandbox
```

Network is fixed to `none`, secrets to `{}`, image/provider come only from trusted config. A lost,
cancelled, dirty, oversized, or unprovisionable sandbox fails closed: no host fallback, no silent
recreation, no old M4 history continued on a new filesystem. Patches are exported as untrusted
artifacts and never auto-applied to Host.

See [design](docs/stages/m5-project-sandbox.md).

## Headline results

Measured from clean Stage 6 commits `a527796` (E1–E4) and `35b5774` (E5); details and caveats in
the [final results](experiments/results/stage6-final.md).

| Area | Result |
|---|---|
| Stable layout | C2 layout hash invariant to completion-order inversion; C1 hash changes |
| Dedup prompt scaling | 0% → 6.8% (small) → 31.2% (medium) → **44.0%** (large, ~50% duplicates) |
| Dedup warm cache miss | not proportional: negative at small/medium, 2.5–11.1% at large |
| Dedup CPU | ~1 ms (30 K chars), ~3 ms (110 K), ~7–8 ms (250 K) |
| Placement handoff | S2 vs S1: **50.0% / 66.7% / 80.0%** fewer refs on P2/P3/P5 |
| Placement × dedup | S2 keeps 6→2 handoff; T1 keeps 20.2% prompt reduction; not additive for warm miss |
| Recovery | R1 `A=1,B=2,Review=1`; R2 `A=1,B=1,Review=1`; R0/R2 context and output equivalent |

Provider cache is warm-prone and uncontrolled; no cold-start claim is made.

## Final V1 architecture

```text
Workflow
   ↓
Task Placement Planner          deterministic pre-placement (S1/S2)
   ↓
AnyCode Execution Runtime       DAG, waves, lifecycle, checkpoints, resume
   ↓
Atlas Context Runtime           logical selection, ContextStore, ContextRefs
   ↓
Per-request Context Transform   identity / official ContextPilot exact dedup
   ↓
Stable Renderer                 stable prefix + deterministic block order
   ↓
Provider                        DeepSeek; prompt cache is best-effort
   ↓
Checkpoint                      AnyCode execution truth
   ↓
Context-aware Recovery          manifest identity + rebuilt ContextStore
```

Provenance:

- **AnyCode 0.9.0** provides the agent runtime, DAG/wave execution, task lifecycle,
  checkpointing, and `resume_from` mechanics. AtlasOS calls these and does not reimplement them.
- **ContextPilot 0.5.0** provides the official exact-block dedup backend used by the optional
  per-request transform. Its reorder primitive is characterized but not on the online path.
- **AtlasOS** provides the integration architecture, task/context model, logical ContextStore and
  references, stable prompt layout, deterministic placement policy, durable session manifest and
  compatibility identity, rebuild-before-resume integration, recovery observations, and the
  evaluation harness.

Upstream checkpoint/resume and dedup algorithms are not AtlasOS-original infrastructure and are
not presented as such.

## Running the baseline

```bash
uv run python examples/baseline_team.py   # deterministic fakes, no network
```

```bash
# live: needs DEEPSEEK_API_KEY in .env; runs the frozen workload twice
ATLASOS_RUN_LIVE_TESTS=1 uv run python examples/live_baseline.py

# live Stage 2 ablation: U0 vs C1 (naive) vs C2 (stable) with inverted completion order
ATLASOS_RUN_LIVE_TESTS=1 uv run python examples/stage2_ablation.py

# live Stage 3: ContextPilot P0-P3 functional + cache-stress (needs the contextpilot extra)
ATLASOS_RUN_LIVE_TESTS=1 uv run python examples/stage3_contextpilot.py

# Stage 4 placement planning (offline deterministic; live functional optional)
uv run python examples/stage4_formal.py
ATLASOS_RUN_LIVE_TESTS=1 uv run python examples/stage4_live.py

# Stage 5 recovery R0/R1/R2 (deterministic durable-boundary interruption; clean tree required)
uv run python examples/stage5_recovery.py

# Stage 6 final evaluation (clean tree required)
uv run python examples/stage6_offline.py
ATLASOS_RUN_LIVE_TESTS=1 uv run python examples/stage6_live.py
uv run python examples/stage6_report.py

# V2 M1 tool runtime (needs the tools extra for semantic search and real MCP)
uv run python examples/m1_demo.py
uv run python examples/m1_tool_search_eval.py
uv run python examples/m1_surface_eval.py
ATLASOS_RUN_LIVE_TESTS=1 uv run python examples/m1_surface_eval.py

# V2 M2 scoped worker evaluation (clean tree required for formal record)
uv run python examples/m2_scoped_worker_eval.py

# V2 M3 action safety evaluation (clean tree required for formal record)
uv run python examples/m3_action_safety_eval.py

# M1–M3 control-plane reliability evaluation (clean tree required for formal record)
uv run python examples/m1_m2_m3_control_plane_eval.py

# V2 M4 durable action evaluation (clean tree required for formal record)
uv run python examples/m4_durable_action_eval.py

# M1–M4 crash/replay/idempotency reliability evaluation (clean tree required)
uv run python examples/m1_m4_failure_injection_eval.py

# M4.1 real SQLite cross-process idempotency verification (clean tree required)
uv run python examples/m4_sqlite_process_eval.py

# V2 M5 project sandbox evaluation (clean tree required for formal record)
uv run python examples/m5_project_sandbox_eval.py
```

Every run appends one JSON object to `experiments/runs/runs.jsonl` with task outcomes,
per-call inference records, prompt fingerprints, and aggregate metrics. A session also writes
`runs/<run_id>/atlas_manifest.json` (identity/compatibility), upstream checkpoints under
`runs/<run_id>/checkpoints/`, append-only `inference.jsonl`, and a final `result.json`. Raw
provider prompts are not persisted.

## Development

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --extra dev
uv run ruff check .
uv run pyright
uv run pytest
```

ContextPilot is an optional extra because it pulls a heavy scientific stack:

```bash
uv sync --extra dev --extra contextpilot
```

V2 M1 semantic tool search and real MCP use the `tools` extra (`mcp<2`, sentence-transformers,
faiss-cpu):

```bash
uv sync --extra dev --extra tools
```

Live DeepSeek tests are opt-in and never part of the default suite. The adapter loads the
nearest `.env` automatically, and real environment variables take precedence over the file:

```bash
cp .env.example .env        # set DEEPSEEK_API_KEY
ATLASOS_RUN_LIVE_TESTS=1 uv run pytest -m live
```

On PowerShell:

```powershell
$env:ATLASOS_RUN_LIVE_TESTS = "1"
uv run pytest -m live
```

## Repository layout

```text
src/atlasos/
  config.py            experiment configuration and ablation switches
  workflow.py          Atlas task ids + compiler onto AnyCode title-based specs
  runtime.py           AtlasRuntime thin facade, run/resume, normalization, attribution
  experiments.py       JSONL persistence and reproducibility metadata
  integrations/        AnyCode wiring + DeepSeek adapter
  metrics/             normalized usage, prompt fingerprints, run aggregation
  context/             context blocks, store, selectors, naive/stable renderers
  durability/          session manifest, compatibility checks, context rebuild, fault injection
  placement/           deterministic pre-placement policies and planner
  tools/               one registry, catalog, search index, disclosure state, dispatch, MCP
  experimental/        batch characterization and M1 retrieval evaluation set
tests/unit             deterministic offline tests
tests/integration      runtime-level tests with fake or live providers
examples/              baseline, live, Stage 2/3/4/5/6 experiment entry points
experiments/configs/   experiment configurations (U0, C1, C2, S0-S2, T0/T1)
experiments/results/   frozen stage summaries, Stage 6 tables and SVG figures
experiments/runs/      generated run records (gitignored)
```

## Attribution and provenance

- **AnyCode 0.9.0** provides the agent runtime, DAG/wave execution, task lifecycle,
  tool registry/executor, MCP discovery, checkpointing, and `resume_from` mechanics. AtlasOS does
  not reimplement or patch these; it calls them.
- **AtlasOS** owns the integration architecture, task/context model, logical `ContextStore` and
  references, stable prompt layout, deterministic placement policy, durable session manifest and
  semantic/placement identity verification, rebuild-before-execution resume integration,
  progressive tool catalog/discovery/dispatch, tool-state reconstruction, append-only recovery
  observations, and the Stage 2–6 plus V2 M1 experiment harnesses.
- **ContextPilot** is used only through the optional per-request dedup transform; its reorder
  path is characterized but not part of the online runtime.
- **DeepSeek** is the live inference backend and the source of best-effort cache metrics.

Upstream checkpoint/resume code is not AtlasOS-original infrastructure and is not presented as
such.

## Current limitations

- Prompt-cache metrics are provider-reported and best-effort; the Stage 6 provider cache is
  uncontrolled and warm-prone, so no cold-start claim is made.
- ContextPilot dedup reduces prompt size but did not reduce warm cache-miss prefill in the Stage 6
  workload; it can increase the uncached tail. Prompt length is not a cache-miss proxy.
- E4 evaluates placement and context effects on the same topology but in separate measurement
  paths; it does not execute a single seven-task live workflow with synthetic large outputs.
- Placement is deterministic pre-placement, not physical KV residency or dynamic scheduling.
- Recovery is task-level with at-least-once semantics for in-flight work; no exactly-once claim
  is made for tasks, provider calls, tools, or external side effects.
- Stage 5 supports one resume generation; recursive/multi-generation resume is out of scope.
- Provider KV/cache state is never persisted. Live DeepSeek recovery was not run.
- V2 M1 model-facing tool names are provider-safe (`tool_search`); search results and dispatch
  targets keep the logical `tool.search` id. MCP 1.x and semantic retrieval are optional extras.
- M1 disclosure/conversation sidecars are session metadata, not a second checkpoint engine;
  AnyCode checkpoints remain execution truth.
- A crash during a tool call after disclosure but before the next turn snapshot may leave the
  disclosure set durable while the conversation snapshot is one turn behind.
- V2 M2 worker profiles are trusted host-owned Python definitions; M2 does not implement
  authentication, IAM, OAuth, credential issuance, or tenant provisioning.
- M2 does not perform tool action authorization, HITL, policy enforcement, capability grants, or
  capability-to-tool mapping. Those are M3 concerns.
- M2 workers do not recursively delegate in V1; profiles set `can_delegate=false` and no delegate
  tool is added.
- Worker assignment task/background text is untrusted prompt data and is not persisted as trusted
  runtime metadata.
- M2 worker records are local trusted-host sidecars, not an authentication boundary against a
  compromised host filesystem.
- M3 is opt-in at tool-runtime construction; a deployment must install `ActionSafetyGate` before
  agents start. Without an action policy, the pre-M3 tool path remains for backwards compatibility.
- M3 policy/WorkerProfile files must be supplied by the trusted host outside agent-writable
  workspaces; M3 does not implement authentication or an OS sandbox.
- M3 HITL is current-run/synchronous only. Durable pending approvals and velocity/sequence limits
  are later stages; M4 adds the durable execution ledger, not durable approval state.
- M3 does not map tool capabilities to M2 worker profiles; action safety and worker construction
  are separate planes.
- M4 does not claim exactly-once execution. Local-only external effects can be UNCERTAIN after a
  crash; UNCERTAIN never auto-retries.
- M4 ledger and AnyCode idempotency store are separate SQLite stores without 2PC. Recovery and
  reconciliation handle short-lived inconsistency.
- AnyCode lacks a universal public idempotency inspection API; M4 fails closed to UNCERTAIN when
  no safe observation exists instead of querying private tables.
- M4 does not implement real provider idempotency adapters; it defines the trusted key contract
  and deterministic fakes.
- M5 v1 formal PASS uses a deterministic local FakeSandboxProvider; live Modal/other cloud
  backends are optional smoke tests and are not required for the record.
- M5 v1 fixes network to none and injects no secrets; no domain allowlist or credential proxy.
- M5 v1 has no automatic sandbox resume/snapshot restore/workspace replay and no patch apply.
- A provisioning crash may leave an orphan remote sandbox; this is documented operational debt.
- Multi-node scheduling, self-hosted serving, and KV prefetch are explicitly out of V1 scope.

