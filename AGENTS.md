# AGENTS.md — AtlasOS Repository Constitution

## 1. Project Mission

AtlasOS is an experimental AgentOS-style runtime for long-horizon multi-agent workloads.

The project does **not** aim to reimplement a full agent framework, LLM serving engine, physical KV-cache manager, container runtime, vector database, or distributed operating system.

AtlasOS builds on a lightweight multi-agent orchestration runtime and focuses on one research question:

> How should a multi-agent runtime jointly manage task execution and model context so that it minimizes cache-miss prefill and runtime cost rather than merely minimizing prompt length?

V1 uses:
- **AnyCode** as the lightweight multi-agent runtime base.
- **ContextPilot** as the paper-derived context optimization backend.
- **DeepSeek API** as the primary inference backend and source of prompt-cache hit/miss metrics.
- **DeepSeek Harness** as a development tool only. Harness is not a runtime dependency of AtlasOS.

The primary V1 contributions are:
1. A multi-agent **Context Runtime** that separates logical context selection from physical prompt rendering.
2. A **stable-prefix / cache-aware rendering policy** designed to preserve prompt-cache reuse.
3. A pluggable **task-to-agent scheduling policy** that can use capability, context affinity, runtime load, and observed cache metrics.
4. A reproducible **ablation and evaluation harness** comparing baseline and cache-aware policies.
5. Integration of context references and cache metrics into the existing durable-session/checkpoint path.

---

## 2. Architectural Invariants

These rules are mandatory unless an ADR explicitly changes them.

### 2.1 Reuse before reimplementation

Do not reimplement capabilities that already exist in the selected upstream projects unless:
- the existing interface prevents the experiment,
- the required behavior cannot be obtained by composition/subclassing/adapters,
- or a controlled fork is necessary for an ablation.

Prefer, in order:
1. adapter/composition,
2. subclass/extension point,
3. minimal patch,
4. forked subsystem rewrite only as a last resort.

### 2.2 AnyCode is the runtime base, not a dependency to hide

AtlasOS must preserve clear provenance of reused code and APIs.

Do not copy large AnyCode subsystems into new files merely to rename them.
Do not present upstream DAG scheduling, checkpointing, MCP, tool execution, or lifecycle mechanisms as AtlasOS-original algorithms.

AtlasOS may fork or patch AnyCode where required, but patches must be:
- isolated,
- documented,
- test-covered,
- and justified by an AtlasOS experiment or runtime requirement.

### 2.3 ContextPilot is an algorithm backend

ContextPilot answers:

> Given context blocks already selected for a request, how should they be reordered/deduplicated/scheduled to improve prefix reuse?

AtlasOS answers:

> Which context blocks should a specific agent/task logically receive, when should they be compacted or retained, and how should task assignment interact with context/cache information?

Do not reimplement ContextPilot algorithms from paper summaries.
Use the official implementation whenever feasible.

### 2.4 Cache is an optimization, never source-of-truth state

DeepSeek prompt cache is best-effort and controlled by the provider.

Therefore:
- correctness must never depend on cache presence,
- checkpoints must never depend on provider KV state,
- a 0% cache-hit execution must still produce a valid prompt and correct runtime behavior,
- cache predictions are performance hints only.

The durable truth is AtlasOS/AnyCode runtime state plus AtlasOS context references and content.

### 2.5 Optimize cache-miss prefill, not prompt length alone

Never evaluate a context policy only by total input tokens.

At minimum record:
- prompt tokens,
- prompt-cache hit tokens,
- prompt-cache miss tokens,
- cache-hit ratio,
- latency,
- task success,
- estimated monetary cost when pricing is configured.

A shorter prompt is not automatically better.

### 2.6 Stable prefix before dynamic context

Prompt layout must follow this order unless an experiment explicitly disables it:

1. stable system prefix,
2. stable agent role/policy/tool definitions,
3. stable task prefix,
4. cache-optimized context blocks,
5. dynamic tail for the current turn/tool result.

Never insert volatile values into the stable prefix, including:
- timestamps,
- request IDs,
- random UUIDs,
- changing metrics,
- mutable summaries,
- per-turn diagnostics.

### 2.7 Separate WHAT from HOW

Context selection and prompt layout are different subsystems.

- **Selector** decides WHAT information the agent logically needs.
- **Optimizer/Renderer** decides HOW selected information is ordered/formatted for inference efficiency.

Do not merge retrieval, selection, reordering, formatting, and model invocation into one function.

### 2.8 Scheduling layers must remain distinct

AtlasOS scheduler:
- assigns runtime tasks to agents.

ContextPilot scheduling:
- may order inference requests/context blocks for cache reuse.

Do not conflate the two.

### 2.9 Deterministic scheduling outside semantic planning

An LLM planner may create a task graph.
Once tasks exist, dependency resolution and runtime scheduling should be deterministic for a fixed scheduler policy and runtime state.

Do not ask an LLM to choose the next worker on every runtime step unless the experiment explicitly studies LLM routing.

### 2.10 Baselines must exist before optimized policies

Do not implement the final policy first.

Required scheduler baselines:
1. FCFS/wavefront default,
2. capability-aware,
3. context-aware,
4. cache-aware.

Required context baselines:
1. full-context,
2. task-scoped,
3. relevance-selective,
4. ContextPilot-enabled cache-aware rendering.

Each optimized mechanism must be switchable by configuration.

### 2.11 V1 scope is single-node application-level cache awareness

V1 DOES NOT implement:
- physical GPU KV-cache placement,
- vLLM/SGLang cache eviction,
- KV prefetch,
- GPU scheduling,
- Kubernetes,
- distributed consensus,
- distributed task queues,
- custom container runtimes,
- custom vector databases,
- custom MCP protocol implementations.

Self-hosted serving integration belongs to V2.

### 2.12 Keep versioning lightweight

V1 context objects may contain:
- id,
- version,
- source,
- owner,
- task association.

Do not implement MVCC, distributed transaction isolation, CRDTs, or automatic conflict merging in V1.

### 2.13 No framework stacking without a measured need

LangGraph is not a core V1 dependency.

Do not add LangGraph solely because it is popular.
If a later experiment requires a LangGraph-backed agent, implement it as an optional adapter behind the same AtlasOS agent interface.

### 2.14 Every optimization must be removable

All AtlasOS research mechanisms must support ablation through configuration.

Example:
```yaml
context:
  selector: task_scoped
  contextpilot:
    enabled: true
    reorder: true
    deduplicate: false

scheduler:
  policy: capability
```

Avoid designs where enabling one optimization permanently changes the data model or makes the baseline impossible to reproduce.

---

## 3. Required Engineering Workflow for DeepSeek Harness

For every stage:

1. Read this `AGENTS.md`.
2. Read `docs/architecture.md`.
3. Read the active stage specification.
4. Inspect existing source and tests before proposing changes.
5. Identify existing upstream interfaces before creating new abstractions.
6. Produce a short implementation plan:
   - current state,
   - files to change,
   - interfaces reused,
   - interfaces added,
   - risks,
   - out-of-scope items.
7. Implement the smallest coherent change.
8. Run formatting/lint/type/tests available in the repository.
9. Inspect the final diff.
10. Report:
    - behavior implemented,
    - tests run,
    - architectural deviations,
    - known limitations,
    - next stage prerequisites.
11. Update docs only when behavior or architecture actually changed.

Do not perform opportunistic refactors unrelated to the active stage.

---

## 4. Testing Rules

### Unit tests
Required for:
- context selectors,
- prompt renderer ordering,
- DeepSeek usage parsing,
- scheduler scoring,
- context reference/version handling,
- adapter boundaries.

### Integration tests
Use deterministic fake LLM adapters before live DeepSeek calls.

### Live API tests
Must:
- be opt-in,
- use explicit environment variables,
- have bounded token/cost budgets,
- record cache hit/miss fields,
- never be required for the default test suite.

### Regression tests
Every bug that changes:
- prompt ordering,
- task assignment,
- context selection,
- checkpoint restore behavior,
must receive a regression test.

---

## 5. Metrics Contract

Every live inference trace should be able to expose:

```text
session_id
task_id
agent_id
scheduler_policy
context_selector
contextpilot_enabled
prompt_tokens
prompt_cache_hit_tokens
prompt_cache_miss_tokens
cache_hit_ratio
completion_tokens
latency_ms
estimated_cost
success/failure
```

If the provider does not expose cache metrics, fields may be null. Never fabricate them.

---

## 6. Source and License Discipline

- Keep third-party licenses intact.
- Keep a `THIRD_PARTY.md` or equivalent acknowledgement before public release.
- Cite papers/repositories used for algorithm reproduction.
- Do not claim upstream algorithms or infrastructure as original AtlasOS work.
- Clearly distinguish:
  - reused framework capability,
  - reproduced paper method,
  - AtlasOS integration code,
  - AtlasOS experimental extension.

---

## 7. Definition of V1 Complete

V1 is complete when all of the following are true:

1. A multi-agent workload runs on the AnyCode-based runtime.
2. DeepSeek is available through a typed adapter.
3. Prompt cache hit/miss metrics are captured.
4. Context is represented as explicit AtlasOS context blocks/references.
5. Context selection and rendering are separate components.
6. ContextPilot can be toggled on/off without changing task semantics.
7. At least three scheduler policies are swappable through config.
8. Existing checkpoint/resume restores AtlasOS context references and experiment metadata.
9. Baseline vs optimized experiments can be executed from a single experiment runner.
10. The repository contains reproducible results for at least:
    - task success,
    - input tokens,
    - cache hit tokens,
    - cache miss tokens,
    - cache-hit ratio,
    - latency,
    - estimated cost.

Do not add V2 serving-engine work until V1 satisfies this definition.
