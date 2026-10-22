# ADR 0003 — DeepSeek Cache Metrics Are Performance Hints

- Status: accepted
- Date: 2026-10-22

## Decision

`prompt_cache_hit_tokens` and `prompt_cache_miss_tokens` reported by DeepSeek are recorded as
provider-reported performance metrics. They never become durable state, never gate
correctness, and are never fabricated when absent.

## Context

Provider prompt caching is best-effort: residency can be evicted or simply miss. Tests must
still pass with a 0% cache-hit ratio, and checkpoints must never depend on provider KV state.
At the same time, cache-miss prefill is the quantity V1 optimizes, so the metrics must be
captured faithfully whenever the provider exposes them.

## Alternatives

- Treat cache state as durable runtime state: rejected — no provider guarantee, breaks restore.
- Estimate cache behavior locally: rejected — would fabricate metrics.
- Ignore cache metrics and optimize prompt length: rejected — that is the hypothesis under test.

## Consequences

- Missing provider fields yield `None`, never zero.
- Every live trace records hit tokens, miss tokens, weighted ratio, latency, and success.
- A negative or zero-hit result is a valid experimental outcome, not a test failure.
