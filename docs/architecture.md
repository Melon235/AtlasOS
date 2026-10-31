# AtlasOS V1 Architecture

## 1. Purpose

AtlasOS V1 is an experimental AgentOS-style runtime for long-horizon multi-agent workloads.

It is built by extending a lightweight existing multi-agent runtime rather than rebuilding orchestration from scratch.

The project studies one primary systems trade-off:

> Reducing model-visible context can lower prompt size, but frequent selection/reordering/compaction may destroy reusable prompt prefixes. AtlasOS therefore optimizes cache-miss prefill and end-to-end runtime cost rather than prompt length alone.

The architecture intentionally separates:
- task execution,
- logical context,
- model-visible prompt construction,
- provider-side cache metrics,
- durable state.

---

## 2. Technology Decisions

### Runtime base: AnyCode

AnyCode is used for existing orchestration mechanisms, including:
- agent execution,
- task/DAG orchestration,
- dependency scheduling,
- bounded concurrency,
- messaging/shared memory where needed,
- LLM adapter boundaries,
- tools/MCP,
- checkpoint/resume.

AtlasOS does not reimplement these mechanisms unless an experiment requires a controlled modification.

### Context optimization backend: ContextPilot

ContextPilot is used as the official paper implementation for:
- context block reordering,
- deduplication,
- prefix-reuse optimization,
- context-index-based cache-aware behavior where applicable.

AtlasOS wraps ContextPilot behind an adapter so it can be toggled during ablations.

### Inference backend: DeepSeek API

DeepSeek is the primary V1 inference backend because the API exposes:
- `prompt_cache_hit_tokens`,
- `prompt_cache_miss_tokens`.

These fields allow AtlasOS to evaluate actual provider-reported prompt-cache behavior.

DeepSeek cache is best-effort and not part of durable state.

### Development tool: DeepSeek Harness

DeepSeek Harness is used to implement and review the repository in stages.

It is not an AtlasOS runtime dependency.

### LangGraph

Not part of the V1 core path.

A future optional agent adapter may use LangGraph, but no core AtlasOS subsystem may depend on LangGraph semantics.

---

## 3. High-Level Architecture

```text
                    Multi-Agent Application
                Planner / Researcher / Coder / Reviewer
                              |
                              v
+-------------------------------------------------------------+
|                        AtlasOS V1                           |
|                                                             |
|  +--------------------- Task Runtime ---------------------+  |
|  | AnyCode DAG / lifecycle / task queue / concurrency    |  |
|  | Atlas scheduler-policy extension                      |  |
|  +-----------------------------+-------------------------+  |
|                                |                            |
|                                v                            |
|  +-------------------- Context Runtime ------------------+  |
|  | ContextStore / ContextRef / ContextBlock             |  |
|  | Selector: WHAT should this agent know?               |  |
|  | ContextPilot Adapter: optimize selected blocks        |  |
|  | Renderer: HOW is the final prompt laid out?          |  |
|  | CacheMetricsTracker                                  |  |
|  +-----------------------------+-------------------------+  |
|                                |                            |
|                                v                            |
|  +------------------- Provider Adapter ------------------+  |
|  | DeepSeek adapter                                      |  |
|  | usage -> hit/miss/cache ratio/latency                 |  |
|  +-----------------------------+-------------------------+  |
|                                |                            |
|  +---------------- Durable Session ----------------------+  |
|  | Existing AnyCode checkpoint/resume                    |  |
|  | + Atlas context refs/versions/experiment metadata     |  |
|  +-------------------------------------------------------+  |
+-------------------------------------------------------------+
                              |
                              v
                         DeepSeek API
```

---

## 4. Three-State Model

AtlasOS must keep three categories of state conceptually separate.

### 4.1 Durable runtime state

Answers:

> What actually happened and what must survive a crash?

Examples:
- session state,
- task DAG state,
- task assignments,
- checkpoint metadata,
- tool results already persisted by the underlying runtime,
- AtlasOS context references and versions,
- experiment configuration.

### 4.2 Logical context state

Answers:

> What information exists, and what information should an agent logically receive?

Examples:
- task description,
- dependency outputs,
- artifacts,
- messages,
- memory,
- tool results,
- summaries.

This layer is represented as `ContextBlock` / `ContextRef`.

### 4.3 Model-visible context

Answers:

> In what order and format should selected information be sent to the LLM?

Examples:
- stable system prefix,
- agent role,
- tool definitions,
- stable task context,
- ContextPilot-optimized blocks,
- dynamic current-turn tail.

Provider prompt cache belongs here only as a performance property.

Cache presence must not affect correctness.

---

## 5. Context Model

### 5.1 ContextBlock

V1 should keep the model deliberately small.

```python
class ContextBlock:
    id: str
    content: str
    kind: ContextKind
    version: int
    source: str | None
    owner_agent_id: str | None
    task_id: str | None
    importance: float | None
```

Suggested `ContextKind`:
- SYSTEM
- TASK
- MESSAGE
- TOOL_RESULT
- ARTIFACT
- MEMORY
- SUMMARY

Do not add transaction semantics or MVCC in V1.

### 5.2 ContextRef

Used in task handoff/checkpoint metadata:

```python
class ContextRef:
    block_id: str
    version: int
```

The referenced content must be retrievable without relying on provider cache.

### 5.3 ContextStore

Responsibilities:
- store/retrieve context blocks,
- resolve references,
- return versioned content,
- expose blocks to selectors.

Not responsible for:
- relevance ranking policy,
- prompt ordering,
- ContextPilot cache optimization.

---

## 6. Context Pipeline

```text
Context Store
      |
      v
Selector
WHAT information is logically required?
      |
      v
Selected Context Blocks
      |
      v
ContextPilot Adapter
Reorder / deduplicate / prefix-reuse optimization
      |
      v
Renderer
Stable Prefix + Stable Task + Optimized Blocks + Dynamic Tail
      |
      v
Provider Adapter
      |
      v
DeepSeek
```

Each layer must be independently testable and switchable.

---

## 7. Selectors

Required V1 selectors:

### 7.1 FullContextSelector

Returns all eligible context.

Purpose:
- correctness/reference baseline,
- measure the cost of no logical pruning.

### 7.2 TaskScopedSelector

Returns:
- stable system/agent context,
- current task,
- dependency outputs,
- explicitly shared artifacts/messages.

Purpose:
- simple structured baseline with no semantic ranking.

### 7.3 RelevanceSelector

May use BM25/embedding/simple score.

Purpose:
- measure whether aggressive selection reduces total tokens but harms prefix-cache reuse.

Do not use an LLM judge by default.

---

## 8. Prompt Rendering Contract

Prompt order must be stable.

```text
[Stable System Prefix]
[Stable Agent Role]
[Stable Tool Definitions / Policy]
[Stable Task Prefix]
[ContextPilot-Optimized Blocks]
[Dynamic Tail]
```

### Stable prefix rules

Must not contain:
- timestamps,
- random IDs,
- changing runtime stats,
- changing summaries,
- request-specific debugging data.

### Dynamic tail

May contain:
- latest user message,
- current tool result,
- current task-local scratch output,
- turn-specific instruction.

This layout is a core experimental invariant.

---

## 9. ContextPilot Boundary

ContextPilot receives context blocks only after AtlasOS has selected them.

AtlasOS owns:
- block lifecycle,
- logical selection,
- task/agent association,
- version/reference metadata,
- prompt structure surrounding optimized blocks,
- experiment toggles.

ContextPilot owns:
- its official reorder/dedup/context-index logic.

Do not duplicate ContextPilot internals inside AtlasOS.

---

## 10. Scheduler Architecture

AtlasOS modifies task-to-agent assignment, not provider GPU scheduling.

```text
Ready Task
   |
   v
Candidate Agents
   |
   v
Atlas Scheduling Policy
   |
   v
Assigned Agent
```

Required policies:

### 10.1 BaselineScheduler

Use upstream/default AnyCode wavefront/FCFS-like behavior.

### 10.2 CapabilityScheduler

Score agents by declared capability/task fit.

### 10.3 ContextAwareScheduler

Adds logical context affinity, such as overlap between:
- task-required context refs,
- context already associated with an agent/session.

### 10.4 CacheAwareScheduler

Adds historical provider cache signal.

Example score:

```text
score =
  w_capability * capability
+ w_context    * context_overlap
+ w_cache      * historical_cache_hit_ratio
- w_load       * runtime_load
```

V1 does not claim that this represents physical KV residency.

The cache term is an application-level heuristic using provider-reported history.

---

## 11. Cache Metrics

DeepSeek response metrics are recorded per inference:

```text
prompt_tokens
prompt_cache_hit_tokens
prompt_cache_miss_tokens
completion_tokens
latency_ms
```

Derived:

```text
cache_hit_ratio =
    hit_tokens / (hit_tokens + miss_tokens)
```

Primary performance target:

> Reduce cache-miss prompt tokens while maintaining task quality.

Do not optimize only:
- prompt length,
- total tokens,
- average cache-hit ratio.

All three may be misleading without task quality and latency.

---

## 12. Compaction Boundary

V1 does not require a custom learned compaction algorithm.

If compaction is added:
- it must be a policy behind an interface,
- it must be compared with a fixed-threshold baseline,
- it must not mutate the stable system/agent prefix,
- it must not destroy durable raw context,
- it must be optional in ablations.

The preferred first V1 experiment is stable layout + selection + ContextPilot before introducing custom compaction.

---

## 13. Durable Session Boundary

Reuse AnyCode checkpoint/resume.

AtlasOS adds only the data necessary for its own semantics:
- context refs and versions,
- active selector policy,
- ContextPilot enabled/options,
- scheduler policy,
- accumulated cache metrics/experiment metadata.

Do not persist provider-side cache state.

Recovery rule:

```text
restore durable state
-> resolve context refs
-> reconstruct prompt deterministically
-> invoke provider

cache hit = optimization
cache miss = still correct
```

---

## 14. Tool/MCP Boundary

Reuse AnyCode tool and MCP interfaces.

AtlasOS V1 does not reimplement:
- MCP protocol,
- tool registry,
- sandbox provider,
- tool executor.

Tools may generate context blocks as outputs.

Example:

```text
ToolResult
   -> persisted by runtime
   -> converted/reference-linked as ContextBlock
   -> later selected for another agent/task
```

---

## 15. Agent Boundary

V1 agents should be simple.

Recommended demo roles:
- Planner
- Researcher
- Coder
- Reviewer

The project evaluates runtime behavior, not persona engineering.

An agent backend must satisfy a small adapter interface and remain replaceable.

LangGraph-backed agents are optional future adapters.

---

## 16. Explicit V1 Non-Goals

AtlasOS V1 does not implement:
- physical KV-cache scheduling,
- self-hosted LLM serving,
- vLLM/SGLang internals,
- KV eviction/prefetch,
- GPU topology,
- multi-node task scheduling,
- Kubernetes,
- custom message broker,
- custom vector database,
- custom sandbox runtime,
- custom MCP stack,
- distributed state consistency,
- MVCC/CRDT context state,
- reinforcement learning schedulers,
- custom model training.

These belong to future work.

---

## 17. Experimental Matrix

### Context experiments

| Variant | Selector | ContextPilot | Stable layout |
|---|---|---:|---:|
| C0 | Full | Off | Yes |
| C1 | Task-scoped | Off | Yes |
| C2 | Relevance | Off | Yes |
| C3 | Task-scoped | On | Yes |
| C4 | Relevance | On | Yes |

Metrics:
- task success,
- prompt tokens,
- cache-hit tokens,
- cache-miss tokens,
- cache-hit ratio,
- TTFT/latency if measurable,
- estimated cost.

### Scheduler experiments

| Variant | Assignment signal |
|---|---|
| S0 | upstream/default |
| S1 | capability |
| S2 | capability + context affinity |
| S3 | capability + context affinity + historical cache signal |

### Durability experiment

At minimum:
- checkpoint a multi-task session,
- terminate/restart runtime,
- resume,
- verify task/context references are restored,
- verify correctness does not depend on provider cache survival.

---

## 18. Success Criteria

AtlasOS V1 is successful if it can demonstrate at least one of the following with reproducible data:

1. ContextPilot integration reduces cache-miss prefill or cost under multi-agent workloads without lowering task success.
2. Stable prompt construction outperforms aggressively rebuilt prompts on provider cache reuse.
3. A context/cache-aware task-assignment policy improves cache-miss tokens, latency, or cost over the default scheduler.
4. If cache-aware scheduling shows weak gains on the cloud API, the project produces a defensible negative result explaining the limitation and motivating V2 self-hosted serving experiments.

Negative experimental results are acceptable. Unmeasured claims are not.

---

## 19. V2 M6 Runtime Suspension Boundary

V2 M6 adds one durable pause point before tool execution when M3 requires a human decision:

```text
model response
  -> M3 whole-response preflight (evaluate only)
  -> if any REQUIRE_HUMAN: ExecutionSuspension (Runtime state)
  -> durable approval challenge(s)
  -> human decision
  -> resume: M1/M2/M3/M5 revalidation
  -> M4 execute/replay
  -> durable ToolResult continuation
  -> clear suspension
```

`ExecutionSuspension` belongs to Atlas Runtime state, not to M6 approval state. M6 owns durable
human-decision truth; M3 remains the authorization authority; M4 remains the execution dedupe and
replay authority; M5 remains the sandbox/project isolation authority. No second runtime, tool
registry, executor, policy engine, idempotency engine, or workflow engine is added. The exact
pending invocation is recovered from protected Runtime state; the model is never asked to
reconstruct it. Details: [`docs/stages/m6-durable-approval.md`](stages/m6-durable-approval.md).

## 20. V2 Boundary

Only after V1 completion, consider:

```text
AtlasOS
   -> self-hosted SGLang/vLLM
   -> ContextPilot engine hooks
   -> KVFlow-like physical cache scheduling
```

V2 may study:
- physical KV residency,
- KV eviction,
- prefetch,
- workflow-aware serving,
- runtime-to-serving hints.

No V2 feature should leak into the V1 critical path.
