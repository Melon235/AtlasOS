# Stage E0 — Multi-Worker and Context Plane Architecture Audit

Audited at `HEAD=c9e4f02`, clean worktree, 2026-11-09. No code changed in E0.

## 1. Actual execution paths

### V1 static workflow DAG

`AtlasRuntime.run()/resume()` compiles an `AtlasWorkflow` through `compile_workflow()` into
AnyCode `TaskSpec` objects. AnyCode owns wave scheduling, dependency resolution, checkpoints, and
the agent loop. `AtlasRuntime` owns context projection/selection/rendering, placement planning,
metrics attribution, and result normalization. This is a **static DAG**: the task graph is fixed
before execution; there is no runtime planner that creates or removes tasks.

### V2 scoped worker path

`ScopedWorkerFactory.create()/resume()` creates a trusted `WorkerInstance` with a profile,
execution context, task context, and an `AgentConfig`. In the evaluation/coding path, each
AnyCode task is assigned to one scoped worker. Workers are identities and authorization scopes;
they are **not** independent agent loops with their own private tool executors. A single
`AtlasToolRuntime` serves the shared project workspace.

### Real collaboration currently supported

The multi-worker evaluation path (`experiments/evaluation/runner.py`, mode `multi_worker`) creates
2–3 `WorkerInstance`s, compiles one static AnyCode task DAG, and runs it against one M5 sandbox.
Dependencies are real `TaskSpec.depends_on` edges. AnyCode injects completed dependency results
into the downstream task prompt; the downstream agent can also read the shared sandbox files. The
hidden oracle checks the delivered artifacts, not worker names or role prompts.

### Not implemented

- No dynamic supervisor that decomposes a goal at runtime.
- No recursive/autonomous delegation, worker-to-worker messaging, or agent-created tasks.
- No per-worker sandbox; all workers share the one project binding.
- No automatic conflict resolution for concurrent writers.
- No general agent-level M6 suspension in the coding loop. M6 is exercised through the trusted
  Stage D runtime path, not by pausing an ordinary AnyCode coding run.
- The coding evaluation adapter does **not** go through the V1 ContextPilot/selection path. It
  uses the M1 tool runtime and M5 sandbox, but not `AtlasRuntime._build_atlas_prompt()`.

## 2. Evidence sources available

| Evidence | Source | Authority |
|---|---|---|
| Worker identity/profile/context hash | `workers.json` (`WorkerInstanceRecord`) | M2 durable worker state |
| Task graph and assignees | compiled `TaskSpec` list in the run | static evaluation input |
| Task start/finish | `OrchestratorConfig.on_progress` events | ephemeral runtime observation |
| Worker output | `TeamRunResult.agent_results` | upstream AnyCode result |
| Tool calls per agent | `AgentRunResult.tool_calls` | upstream execution result |
| Handoff text | downstream worker's first user message / dependency result | prompt projection, not M7 |
| Files consumed | `tool_calls` (`file_read`, `bash` outputs) + exported workspace | M5 artifact + real execution |
| Lifecycle/claims | M7 `transcript.jsonl` | evidence only; not execution authority |
| Approval/suspension | SQLiteApprovalStore / SQLiteSuspensionStore | M6 authority |
| Sandbox ownership/state | SQLiteSandboxBindingStore + OS ownership lock | M5 authority |

M7 can explain what happened; it cannot establish that a downstream worker actually consumed an
artifact. E1 therefore checks the downstream prompt/tool trace and the hidden oracle, and keeps M7
as corroborating evidence.

## 3. E1 plan

- 12 new paired tasks, not copies of the prior 5 multi-worker samples.
- A0: one scoped worker receives the full goal.
- A1: the same fixture/oracle with 2–3 workers and a static dependency DAG.
- Same DeepSeek model, temperature, token budget, sandbox image, tool capability, and oracle.
- Evidence per run: worker IDs, profiles, task/dependency graph, task start/end, worker outputs,
  handoff text and byte size, downstream consumption, tool calls per worker, final oracle.
- Metrics: task success, handoff completeness/consumption, integration success, worker failure
  attribution, latency, tokens, cost, turns, tool calls, DAG nodes, critical path, worker duration.

## 4. E2 plan

Track A is the only path that can honestly evaluate the V1 Context Plane today:

- `AtlasRuntime` + real DeepSeek multi-task workflows;
- C0 upstream default;
- C1 task-scoped + stable rendering + identity transform;
- C2 C1 + ContextPilot dedup;
- P0 capability-only vs P1 context-locality placement as a separate factor;
- 8 machine-verifiable multi-stage tasks with clean, duplicate-heavy, and conflicting profiles;
- record context blocks, selection, transform chars/blocks/latency, rendered prompt hash, provider
  prompt/cache tokens, correctness, missing critical facts, handoff bytes, agent switches.

Track B (integrating Context Plane into the V2 coding adapter) is a feasibility question. The
coding adapter currently has no prompt-builder hook into `AtlasRuntime`; introducing one would
either duplicate the V1 context pipeline or require a runtime refactor. If that cannot be done by
reusing the existing `AtlasContextEngine` prompt seam and M3–M6 authority, E2 will stop at a
feasibility report and will not claim coding-benchmark context benefits.

## 5. Boundaries carried into E1/E2

- A1 is static DAG collaboration, not dynamic multi-agent autonomy.
- Worker role prompts are not evidence of decomposition; only dependencies, artifacts, and
  consumption are.
- No benchmark result may be attributed to "multi-agent intelligence" without a paired A0/A1
  comparison.
- Dedup, stable rendering, and placement are separate factors; E2 will not combine them into one
  headline number.
- Provider cache is uncontrolled; warm-state observations are not causal cold-start claims.
- Track A is a real LLM multi-stage context evaluation, not a coding-agent evaluation.
