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
| DeepSeek Harness | Development tool only, not a runtime dependency | — |

Provenance and licenses are recorded in [THIRD_PARTY.md](THIRD_PARTY.md).

## Status

Stage 0 (bootstrap/upstream verification) and Stage 1 (runtime baseline) are complete. The
baseline is frozen: no AtlasOS context optimization, context selection, or cache-aware
scheduling exists yet, by design.

- [Architecture](docs/architecture.md)
- [Stage 0/1 specification](docs/stages/00-01-bootstrap-and-baseline.md)
- [Stage 0 verification notes](docs/stages/00-verification-notes.md)
- [Stage 1 baseline notes](docs/stages/01-baseline-notes.md)
- [Architecture decision records](docs/decisions/)
- [Repository constitution](AGENTS.md)

## Running the baseline

```bash
uv run python examples/baseline_team.py   # deterministic fakes, no network
```

```bash
# live: needs DEEPSEEK_API_KEY in .env; runs the frozen workload twice
ATLASOS_RUN_LIVE_TESTS=1 uv run python examples/live_baseline.py
```

Every run appends one JSON object to `experiments/runs/runs.jsonl` with task outcomes,
per-call inference records, prompt fingerprints, and aggregate metrics. Raw provider prompts
are not persisted.

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
  runtime.py           AtlasRuntime thin facade, result normalization, attribution
  experiments.py       JSONL persistence and reproducibility metadata
  integrations/        AnyCode wiring + DeepSeek adapter
  metrics/             normalized usage, prompt fingerprints, run aggregation
  context/           (Stage 2) context blocks, store, selectors, renderer
  scheduler/         (Stage 4) task-to-agent scheduling policies
tests/unit             deterministic offline tests
tests/integration      runtime-level tests with fake or live providers
examples/              offline baseline and live DeepSeek baseline
experiments/configs/   experiment configurations
experiments/runs/      generated run records (gitignored)
```

## Current limitations

- Prompt-cache metrics are provider-reported and best-effort; correctness never depends on them.
- The cache-aware scheduler is a Stage 4 heuristic, not physical KV residency.
- Multi-node scheduling, self-hosted serving, and KV prefetch are explicitly out of V1 scope.
