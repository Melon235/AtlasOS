# AtlasRuntime — Architecture Design Contract

**Status:** owner-confirmed design baseline. This document is the highest design authority in the
repository. It states what AtlasRuntime *is*, not what the current branch happens to run.

Path note: the repository keeps active architecture documents flat under `docs/` (`architecture.md`,
`anycode-delta.md`, `runtime-entrypoints.md`, …). This contract follows that convention instead of
introducing a new `docs/architecture/` directory next to a file already named `architecture.md`.

---

## 0. Three layers that must never be mixed

| Layer | Question | Authority | Documents |
|---|---|---|---|
| **Design Contract** | What is AtlasRuntime, permanently? | this document | this file |
| **Implementation Status** | What is wired *today*, on which entry point? | source code, verified by audit | `project-status.md`, `runtime-entrypoints.md`, `final-4-architecture-coverage.md` |
| **Experiment Results** | What has been measured, under which conditions? | frozen run records | `evaluation-summary.md`, `experiments/results/` |

Binding rules:

1. The contract is **never trimmed to match the current implementation**. A mechanism that no coding
   path uses yet stays in the contract and is marked as not wired in a status document.
2. A status document never overrides the contract. A status document describing fewer wired
   mechanisms is an implementation gap, not an architecture change.
3. An experiment result never redefines the contract. A negative result is recorded as a result.
4. Where the three layers disagree, the disagreement is written down as an explicit gap. It is never
   resolved by deleting the mechanism from the contract, and never resolved by claiming the
   mechanism runs.

---

## 1. Project positioning

**AtlasRuntime — Context-aware and Reliable Agent Runtime.**

AtlasRuntime is a context-aware, reliability-oriented agent runtime built on AnyCode. It addresses:

- logical organisation and physical rendering of agent context;
- context transfer between tasks;
- context-aware task assignment;
- trusted worker and tool execution;
- reliability and recoverability of agent execution;
- selective delegation across different task structures.

AtlasRuntime does **not** reimplement a general agent framework. AnyCode supplies the agent loop, DAG
execution, task lifecycle, scheduling and checkpoint primitives. AtlasRuntime implements a Context
Plane and Reliable Execution on top of them, both with independent engineering value.

AtlasRuntime is not an operating system, not a model serving engine, not a KV-cache manager, and not
a general-purpose planner.

---

## 2. Core one — Context Engineering

This is the design origin of the project and its first permanent contribution.

### A. Logical context model

`ContextBlock`, `ContextRef`, `ContextStore`, context versioning, and the association between task
outputs and logical context.

Goal: decouple logical context from concrete prompt strings so a task result can be selected,
rendered and restored as a structured reference rather than as pasted text.

### B. Task-scoped context selection

`TaskScopedSelector`: select the context a task needs from the task itself and its dependency
relations; keep unrelated task results out of the current prompt.

The current implementation uses direct dependencies as the selection boundary. It must not be
described as supporting full transitive closure.

### C. Deterministic context rendering

`StablePromptRenderer`: deterministic block ordering, a stable task prefix, a context layout hash, a
prompt hash and explicit context version markers.

Goal: keep logical context and prompt layout consistent, and study the effect of that consistency on
model cache utilisation.

AtlasRuntime controls only the content, layout and order of the context it supplies. It must never
claim control over a provider's internal KV cache.

### D. Context transform

Identity transform, plus an optional ContextPilot exact-duplicate block deduplication.

ContextPilot is a replaceable context **transform** component, not the subject of Context Engineering.

Preserved historical findings:

- multi-line, block-separable duplicate payloads do reduce character volume;
- single-line payloads may yield no reduction at all;
- a character reduction is not a proportional reduction in real model cost or cache miss.

Using identity by default is a legitimate engineering decision. It is not a reason to delete the
context-transform architecture.

### E. Context-locality placement

Task-to-worker assignment driven by task dependencies and context handoff cost, subject to worker
capability and execution constraints, to reduce logical cross-worker context handoff.

Placement is an assignment mechanism, not a router. A reduction in logical handoff references is not
a reduction in network traffic, physical KV cache, or model inference cost.

### F. Context-aware recovery

AnyCode-checkpoint-based task recovery, an Atlas manifest with compatibility verification,
`ContextStore` reconstruction, and equivalence checks over context layout, prompt and refs.

The existing workflow recovery path is preserved.

Whether an ordinary coding app supports full crash recovery must be stated from the implementation.
Workflow-recovery capability must not be advertised as something every coding run has.

### Overall goal of Context Engineering

Jointly manage task execution and context: reduce redundant information, reduce unnecessary context
handoff, improve model input layout and cache utilisation, and keep context state verifiable and
recoverable.

Context Engineering is not a prompt template, and it is not an auxiliary feature of the router.

---

## 3. Core two — Reliable Execution

An engineering extension of the Context Runtime, with equal standing as a core.

### A. Tool engineering

Progressive tool disclosure, keyword/semantic tool search, the search → disclose → dispatch path, MCP
integration, and an explicit boundary between tool capability and actually executable tools.

### B. Scoped worker

Trusted worker profiles, worker identity, an independent `ExecutionContext`, and explicit capability
and permission boundaries.

### C. Action safety

`ActionSafetyGate`: tool-argument checks, resource-scope checks, worker-permission and external-policy
checks, and human approval.

### D. Durable execution

Durable action ledger, stable action identity, idempotency and execution replay, classification of
uncertain side effects, and no unverified automatic retry.

### E. Sandbox binding

Trusted project-workspace binding, sandbox lifecycle management, execution isolation, workspace
export, sandbox cleanup, and trusted reattachment when necessary.

Existing infrastructure: free, locally self-hosted OpenSandbox.
Windows runtime → `localhost:8080` → WSL2 OpenSandbox → Docker Engine.

AtlasRuntime does not build a container runtime, does not purchase a commercial sandbox service, and
does not change the original sandbox design to suit an evaluation baseline.

### F. Approval and recovery

Human approval, durable suspension, state reconstruction, trusted recovery, and handling of uncertain
execution states.

### G. Resource governance

Shared run budget, cross-worker resource limits, and evidence for token, model-request, tool-call and
task-termination limits.

Admission limits that estimate must be distinguished from hard resource ceilings.

---

## 4. Extension — Adaptive Delegation

Adaptive Delegation is a follow-up optimisation motivated by the historical performance regression of
static multi-agent decomposition.

Its responsibility: decide, from public task structure, a candidate DAG, dependency relations and
collaboration risk, whether to execute with a single worker or multiple workers.

Three mechanisms must be kept distinct:

| Mechanism | Decides |
|---|---|
| **Router** | whether to accept a candidate multi-task decomposition |
| **Placement** | after acceptance, how tasks are assigned to legal workers |
| **Context Plane** | what context each task receives |

Adaptive Delegation does not replace Context Engineering and does not replace Reliable Execution.

The current router makes an admission decision on an existing candidate proposal. It is not a general
automatic task-decomposition planner.

The project's overall positioning must never be narrowed to "multi-agent router" or "coding agent".

---

## 5. Evaluation and Observability

Verify the real function, benefit, cost and applicability boundary of the architecture above through
independent workloads, real model calls, real tool execution, a real sandbox, an independent oracle,
fair ablation, trajectories, context evidence, token/cache-hit/cache-miss accounting, cost and
latency, resource cleanup, and failure attribution.

Standing principles:

1. Implemented does not mean enabled.
2. Declared in configuration does not mean actually executed.
3. Unit tests passing does not mean live model integration passing.
4. Functional correctness does not mean performance benefit.
5. Character reduction does not mean token, cache-miss or cost reduction.
6. Less logical handoff does not mean physical cache reuse.
7. One successful task does not mean the system works generally.
8. Negative results must be preserved.
9. The oracle is never modified to obtain a positive result, and no configuration is given extra
   answers.
10. A stage's evaluation need never changes the project's core positioning.

---

## 6. Unified architecture relation

```text
Public Task / Workflow / Candidate DAG
→ Adaptive Delegation        (optional extension: whether to delegate)
→ Context-locality Placement (assign legal candidates)
→ AnyCode DAG / Agent Execution
→ Atlas Context Plane        (Block / Store / Selector / Transform / Renderer)
→ Scoped Worker + Tool Engineering
→ ActionSafetyGate + Durable Ledger
→ Sandbox-bound Execution
→ Artifact / Task Output
→ ContextStore / Recovery / Trajectory / Evaluation
```

This is the **target** composition of the design contract. It must not be advertised as already
fully wired in the current coding app. The actually supported range is an independent audit
(`docs/final-4-architecture-coverage.md`).

---

## 7. Responsibility boundary with AnyCode

AnyCode owns: the agent loop, `AgentRunner`, the task queue, the DAG scheduler, wave execution,
`AgentPool`, `ToolRegistry`, `ToolExecutor`, checkpoints and native resume, idempotency
claim/complete/replay, provider infrastructure, native HITL and the native transcript.

AtlasRuntime owns: the Context Plane (Block/Ref/Store/Selector/Transform/Renderer), deterministic
prompt assembly, placement planning, scoped-worker construction, authorization decisions, durable
action state, sandbox binding and continuity, durable suspension/approval, run-scoped shared budget,
structured handoff, selective delegation, trace integrity and independent deliverable verification.

Never present an upstream capability as an AtlasRuntime original. Never reimplement an upstream
mechanism that already exists; prefer adapter → subclass → minimal patch.

---

## 8. Sandbox infrastructure boundary

Trusted local OpenSandbox is the standing execution substrate: free, self-hosted, reachable on
`localhost:8080` from the Windows runtime through WSL2, backed by the local Docker engine.

- AtlasRuntime does not implement a container runtime.
- AtlasRuntime does not buy a commercial sandbox service.
- The sandbox design is not changed to suit an evaluation arm.
- Every generated artifact must execute inside the real container; no host fallback.
- Every sandbox must be cleaned up under its actual ownership id.

---

## 9. Support boundary — what may and may not be claimed

May be claimed, when a status document says it is wired for the named entry point:

- logical context separated from prompt rendering;
- deterministic layout with a stable prefix;
- direct-dependency-scoped context selection;
- optional exact-duplicate block deduplication as a transform;
- task-to-worker assignment by context locality (workflow composition);
- trusted worker identity, action gate, durable ledger, sandbox binding, durable suspension,
  shared budget, isolated oracle, deliverable verification;
- a router that may reject a candidate decomposition.

May **not** be claimed:

- control of provider KV cache, prefix caching or an eviction policy;
- cache-hit or cost benefit from a shorter prompt;
- physical cache reuse from fewer logical handoff references;
- full crash recovery for ordinary coding runs;
- that a mechanism ran because it appears in a manifest or a config file;
- that the router improves a decomposition, or that it is a general planner;
- that the native baseline has been run in a neutral sandbox when it has not.

---

## 10. Change control

1. Changing anything in this document requires the project owner's explicit approval and a recorded
   decision.
2. A stage task may narrow the work, never the contract.
3. Removing, downgrading or replacing a core mechanism (Context Engineering, Reliable Execution) is
   an architecture change, not an implementation detail.
4. Adding a mechanism requires a measured need and a switchable configuration; every mechanism must
   stay ablation-switchable.
5. A status or evaluation document may record that a mechanism is not wired. It may not record that
   the mechanism is no longer part of the design.
6. When a stage's own scope conflicts with this contract, the stage stops and reports the conflict.
