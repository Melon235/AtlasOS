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
(context-locality placement planning), and Stage 5 (durable session, task-level recovery, and
deterministic context reconstruction) are complete and frozen. Per-request context
transformation is separated from batch request ordering; ContextPilot reorder is experiment-only
and classified as impractical for the online V1 CPU path. No cache-aware scheduling exists yet.
Stage 6 has not been started.

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
- [Architecture decision records](docs/decisions/)
- [Repository constitution](AGENTS.md)

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
tests/unit             deterministic offline tests
tests/integration      runtime-level tests with fake or live providers
examples/              baseline, live, Stage 2/3/4/5 experiment entry points
experiments/configs/   experiment configurations (U0, C1, C2, S0-S2, T0/T1)
experiments/results/   frozen stage summaries
experiments/runs/      generated run records (gitignored)
```

## Attribution and provenance

- **AnyCode 0.9.0** provides the agent runtime, DAG/wave execution, task lifecycle,
  checkpointing, and `resume_from` mechanics. AtlasOS does not reimplement or patch these; it
  calls them.
- **AtlasOS** owns the durable session manifest and schema closure, semantic and placement
  identity verification, deterministic `ContextStore` reconstruction from completed checkpoint
  tasks, the rebuild-before-execution resume integration, append-only recovery observations, and
  the R0/R1/R2 recovery experiments.
- **ContextPilot** is used only through the optional per-request dedup transform; its reorder
  path is characterized but not part of the online runtime.
- **DeepSeek** is the live inference backend and the source of best-effort cache metrics.

Upstream checkpoint/resume code is not AtlasOS-original infrastructure and is not presented as
such.

## Current limitations

- Prompt-cache metrics are provider-reported and best-effort; correctness never depends on them.
- Placement is deterministic pre-placement, not physical KV residency or dynamic scheduling.
- Recovery is task-level with at-least-once semantics for in-flight work; no exactly-once claim
  is made for tasks, provider calls, tools, or external side effects.
- Stage 5 supports one resume generation; recursive/multi-generation resume is out of scope.
- Provider KV/cache state is never persisted. Live DeepSeek recovery was not run for Stage 5.
- Multi-node scheduling, self-hosted serving, and KV prefetch are explicitly out of V1 scope.

