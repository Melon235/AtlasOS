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

## Development-only tooling

DeepSeek Harness is used to implement and review this repository. It is not a runtime dependency
of AtlasOS and is not shipped.

## AtlasOS license

Not yet chosen. Do not claim a license for AtlasOS code until an ADR records the decision.
