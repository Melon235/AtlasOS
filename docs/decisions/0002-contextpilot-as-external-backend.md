# ADR 0002 — ContextPilot Is an External Algorithm Backend

- Status: accepted
- Date: 2026-10-22

## Decision

ContextPilot (`contextpilot==0.5.0`) is used as an external dependency behind an AtlasOS
adapter. AtlasOS does not reimplement its reordering/deduplication algorithms, vendor its
source, or copy paper pseudocode.

## Context

ContextPilot owns the question "given already-selected context blocks, how should they be
ordered/deduplicated to improve prefix reuse?" AtlasOS owns "which blocks should this agent
logically receive, and how does that interact with scheduling?" The split keeps each system's
responsibility testable and ablations switchable.

## Alternatives

- Reimplement from the paper: rejected — risks silent divergence from the official behavior.
- Vendor the package: rejected — provenance and upgrade burden.
- Proxy-only integration: deferred until the in-process path is proven insufficient.

## Consequences

- AtlasOS must record the exact ContextPilot API used and re-verify it on upgrades.
- The adapter must be toggleable at runtime so the C0–C4 matrix stays reproducible.
- ContextPilot internals stay out of AtlasOS source; only adapter code does.
