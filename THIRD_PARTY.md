# Third-Party Provenance

AtlasRuntime builds on other projects. This file records what is reused, at which version, and under
which license. Keep it current before any public release.

Each entry states whether the dependency is a **runtime dependency**, an **optional extra**, or
**evaluation-only**.

## AnyCode

- Repository: <https://github.com/Quantlix/anycode>
- Package: `anycode-py`
- Pinned version: `0.9.0` (tag `v0.9.0`, commit `ffdc7d28e8d5ceefedf3e490ccbe37499eb6eec8`)
- License: MIT
- Class: runtime dependency (`anycode-py[openai]==0.9.0`)
- Reused: agent loop, task/DAG orchestration, dependency scheduling, bounded concurrency, LLM
  adapter boundary and provider registry, tool registry and executor, idempotency primitives, MCP
  transport, checkpoint/resume, run-store transcript, quality-gate sensors.
- AtlasRuntime does not present AnyCode scheduling, checkpointing, MCP, tool execution or lifecycle
  mechanisms as original work. The per-module delta is `docs/anycode-delta.md`.

## OpenSandbox

- Site: <https://open-sandbox.ai>
- Package: `opensandbox`
- Pinned version: `>=1.2,<2` (resolved `1.2.0`)
- License: Apache License
- Class: optional extra (`sandbox`), default sandbox backend
- Reused: containerised sandbox service. AtlasRuntime adds the binding state machine, ownership
  lock, reattachment check, workspace tools and validation probes around it.

## Daytona

- Repository: <https://github.com/daytonaio/daytona>
- Package: `daytona`
- Pinned version: `>=0.197,<1` (resolved `0.227.0`)
- License: Apache-2.0
- Class: optional extra (`sandbox`), alternative backend
- Reused: sandbox provider client through the AnyCode `SandboxProvider` protocol.

## ContextPilot

- Repository: <https://github.com/EfficientContext/ContextPilot>
- Package: `contextpilot`
- Pinned version: `0.5.0` (tag `v0.5.0`, commit `7f3b62b1cb5afd77a49652c47488d2642a7b6b62`)
- License: Apache-2.0
- Paper: *ContextPilot: Efficient Retrieval-Augmented Generation with Accuracy-Preserving Context
  Reuse* (MLSys 2026).
- Class: optional extra (`contextpilot`), **off by default**
- Reused: context block deduplication, wrapped behind `context/transform.py` so it can be toggled.
  Its algorithms are neither reimplemented nor vendored. The evaluation did not show a cache-miss
  benefit (see `docs/evaluation-summary.md` §7).

## Inspect AI

- Documentation: <https://inspect.aisi.org.uk/>
- Package: `inspect-ai`
- Pinned version: `>=0.3.287` (resolved `0.3.287`)
- License: MIT
- Class: optional extra (`evaluation`), evaluation only
- Reused: the external evaluation harness behind `experiments/evaluation/inspect_adapter.py`. It is
  not a runtime dependency.

## DeepSeek API

- Service: <https://api-docs.deepseek.com/>
- Class: external service (no bundled code)
- Usage: reference inference provider. The API reports `prompt_cache_hit_tokens` and
  `prompt_cache_miss_tokens`, which AtlasRuntime records as provider-reported performance metrics.
- Cache state is best-effort and is never treated as durable runtime state.

## Other runtime libraries

`pydantic` (MIT), `python-dotenv` (BSD-3-Clause), `pyyaml` (MIT), plus `typer`, `rich`, `fastapi`,
`uvicorn` and `httpx` for the optional `app` extra. None of them are modified.

## Development-only tooling

DeepSeek Harness is used to implement and review this repository. It is not a runtime dependency and
is not shipped.

## License of this project

Not yet chosen. Do not claim a license for the AtlasRuntime code until an ADR records the decision.
