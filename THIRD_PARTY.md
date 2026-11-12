# Third-Party Provenance

AtlasOS builds on other projects. This file records what is reused, at which version, and under
which license. Keep it current before any public release.

## AnyCode

- Repository: <https://github.com/Quantlix/anycode>
- Package: `anycode-py`
- Pinned version: `0.9.0` (tag `v0.9.0`, commit `ffdc7d28e8d5ceefedf3e490ccbe37499eb6eec8`)
- License: MIT
- AtlasOS usage: multi-agent runtime base — agent execution, task/DAG orchestration, dependency
  scheduling, bounded concurrency, LLM adapter boundary, tool/MCP interfaces, checkpoint/resume.
- AtlasOS does not present AnyCode scheduling, checkpointing, MCP, tool execution, or lifecycle
  mechanisms as original work.

## ContextPilot

- Repository: <https://github.com/EfficientContext/ContextPilot>
- Package: `contextpilot`
- Pinned version: `0.5.0` (tag `v0.5.0`, commit `7f3b62b1cb5afd77a49652c47488d2642a7b6b62`)
- License: Apache-2.0
- Paper: *ContextPilot: Efficient Retrieval-Augmented Generation with Accuracy-Preserving Context
  Reuse* (MLSys 2026).
- AtlasOS usage: context block reordering/deduplication and prefix-reuse optimization, wrapped
  behind an adapter so it can be toggled during ablations. Its algorithms are not reimplemented
  or vendored inside AtlasOS.

## DeepSeek API

- Service: <https://api-docs.deepseek.com/>
- Usage: primary V1 inference backend. The API reports `prompt_cache_hit_tokens` and
  `prompt_cache_miss_tokens`, which AtlasOS records as provider-reported performance metrics.
- Cache state is best-effort and is never treated as durable AtlasOS state.

## Commerce Agent Bench (evaluation-only)

- Repository: <https://github.com/Accio-org/CommerceAgentBench>
- Pinned commit: `d38bd1711aa3926653452458a1be02c1efd12930` (release `v1.3.1`)
- Pinned container image: `acciolyk/accio_bench@sha256:1e9cf5c72a56794175b7d06ece036b92e296e6b7e9e9a7fa244026f6acea3859`
- Code license: Apache-2.0. Task dataset license: CC BY 4.0 (`LICENSE-DATA`).
- AtlasRuntime usage: Stage B0 runs five native text-only tasks through the upstream environment,
  tools and grader. The upstream checkout is **not vendored**: `CAB_UPSTREAM` points at a local copy
  and every task load is verified against the pinned digests in
  `experiments/business_benchmarks/commerce_agent_bench/manifest.json`.
- The adapter
  (`experiments/business_benchmarks/commerce_agent_bench/`) only starts the environment, exposes the
  native tool surfaces and invokes the original grader. Benchmark tasks, mock services, rubrics and
  graders are upstream work and are never presented as AtlasRuntime contributions. No upstream file
  is modified.

## Development-only tooling

DeepSeek Harness is used to implement and review this repository. It is not a runtime dependency
of AtlasOS and is not shipped.

## AtlasOS license

Not yet chosen. Do not claim a license for AtlasOS code until an ADR records the decision.
