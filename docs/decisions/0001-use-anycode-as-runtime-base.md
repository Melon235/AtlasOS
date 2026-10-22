# ADR 0001 — Use AnyCode as the Runtime Base

- Status: accepted
- Date: 2026-10-22

## Decision

AtlasOS V1 extends AnyCode (`anycode-py==0.9.0`) instead of building its own orchestration
runtime. Agent execution, task/DAG orchestration, dependency scheduling, bounded concurrency,
LLM adapter boundaries, tool/MCP interfaces, and checkpoint/resume stay upstream.

## Context

The research question is about joint task+context management under prompt caching, not about
task orchestration. Reimplementing a scheduler or DAG executor would consume the budget and
muddy provenance. AnyCode already provides the lifecycle and a provider boundary.

## Alternatives

- Build a minimal custom runtime: rejected — duplicates upstream work, violates reuse-first.
- Use LangGraph: rejected for V1 core; see ADR 0004.
- Fork AnyCode wholesale: rejected — a controlled patch is preferred.

## Consequences

- AtlasOS code is an adapter/facade layer plus new context, metrics, and scheduling policy
  modules; it must not duplicate AnyCode's scheduler or task queue.
- Upstream limitations become AtlasOS limitations until patched, and patches must be isolated,
  documented, tested, and justified.
- AtlasOS must track the pinned AnyCode version and re-verify extension points on upgrade.
