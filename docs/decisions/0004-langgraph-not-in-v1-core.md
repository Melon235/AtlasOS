# ADR 0004 — LangGraph Is Not in the V1 Core Path

- Status: accepted
- Date: 2026-10-22

## Decision

No AtlasOS V1 subsystem may depend on LangGraph. A future LangGraph-backed agent may exist only
as an optional adapter behind the AtlasOS agent interface.

## Context

The runtime base is AnyCode. Adding a second orchestration framework to the core path would
duplicate lifecycle, state, and checkpoint semantics, and would make the scheduler and context
experiments harder to attribute. Popularity is not a measured need.

## Alternatives

- LangGraph as the runtime base: rejected — conflicts with ADR 0001.
- LangGraph as a required dependency for demos: rejected — hidden coupling.

## Consequences

- Agent adapters stay small and replaceable; LangGraph can appear later behind the same boundary.
- If a future experiment requires LangGraph, it must be opt-in and documented as an adapter.
