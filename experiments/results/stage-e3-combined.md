# Stage E3 - Combined Multi-Worker and Context Plane Findings

## 1. Separation first

Stage E produced two different execution paths and they must not be merged into one headline:

| path | what ran | context plane? | result |
|---|---|---|---|
| E1 coding | V2 scoped workers + AnyCode + real OpenSandbox | **No** | A0 28/36 vs A1 3/36 |
| E2 workflow | V1 AtlasRuntime -> AnyCode -> DeepSeek | **Yes** | C0 23/24, C1 23/24, C2 22/24 |

The coding adapter has no `AtlasRuntime` prompt-builder/context-store integration. Any statement
that multi-worker coding benefited from ContextPilot would be false. E3 therefore reports separate
findings and only compares structural effects that are genuinely shared.

## 2. Worker count and context volume

E1 shows the dominant cost of static multi-worker decomposition:

| metric | A0 | A1 | ratio |
|---|---:|---:|---:|
| prompt tokens | 757,828 | 4,992,381 | 6.59x |
| completion tokens | 61,598 | 171,656 | 2.79x |
| model turns | 257 | 987 | 3.84x |
| tool calls | 310 | 1,075 | 3.47x |
| cost USD | 0.0987 | 0.4063 | 4.12x |
| p50 latency | 37.5 s | 72.7 s | 1.94x |
| success | 28/36 | 3/36 | - |

The prompt growth is not one giant prompt; it is repeated per-worker context and exploration.
Each worker reconstructs its own view of the workspace. This is a property of the static worker
decomposition, not of ContextPilot, because the coding path does not run ContextPilot.

## 3. Dedup and handoff

E2 tested dedup with controlled duplicate-heavy workflows:

- 0/72 C1/C2 inference calls were deduplicated.
- 0 blocks and 0 context characters were removed.
- C1 and C2 had identical prompt bytes (76,161 total, 24 runs).
- C2 added 45.38 ms of transform CPU across 24 runs.

Short dependency outputs do not trigger measurable ContextPilot dedup in this workload. The
handoff prompt bytes were 8,469 for C0 and 9,285 for C1/C2: stable rendering added **+9.6%**
wrapping bytes around the same logical content. There is no evidence of "dedup reduces repeated
handoff" here; the honest conclusion is that the tested duplicate profiles did not create a
dedupable context shape.

E1 independently shows that handoff is fragile even without dedup:

- 36 dependency edges; 21 complete and 21 consumed (58.3%).
- Actual transmitted upstream prefix bytes: 9,390.
- 8 runs had all workers self-report success while the final task failed.
- 6 edges had an upstream success followed by downstream failure.
- AnyCode truncated observed long prerequisite output at a 500-character projection cap.

Logical handoff and actual transferred information are different quantities. The data supports
neither "handoff exists because M7 has worker events" nor "more workers means more shared context".

## 4. Placement

E2 P0/P1 is the one place where a Context Plane component has a clear structural effect:

| placement | success | prompt tokens | dependency agent switches | mean locality |
|---|---:|---:|---:|---:|
| capability_only | 12/12 | 6,021 | 48 | 0.000 |
| context_locality | 12/12 | 6,021 | 24 | 0.667 |

Locality cut logical handoff references by **50%** with identical prompt tokens, completion
tokens, cost, and correctness. It did not reduce actual handoff bytes or provider tokens in this
workflow. Latency direction was inconsistent between rounds (P1 faster in one round, slower in
another), so no latency claim is made.

This is a useful result precisely because it is narrow: placement controls **which agent** owns a
dependency chain, not how much text the provider receives.

## 5. More workers, more information loss?

The E1 evidence suggests yes, but not in the naive "messages are dropped" sense:

- Workers often stop after a benign non-zero command (`side_effect_unknown`, 18 stops) or hit
  `max_turns` (28 stops).
- 8/36 A1 runs had all workers report success and still fail the oracle.
- Upstream/downstream handoff was complete for only 58.3% of edges.
- The one repeatable A1 win was the conflict/merge task (2/3 vs A0 0/3), where independent
  writers produced information a single worker did not.

So the issue is not "more workers always lose information". It is that static decomposition adds
coordination and truncation costs, and only pays when the subproblems are genuinely independent
and the merge is artifact-aware.

## 6. Warm cache and duplicate context

E2 execution order was C0, C1, C2 per task. C0 had a slightly higher cache hit ratio
(0.4864) than C1/C2 (0.4826), and C1/C2 had 138 more miss tokens. This is an observed warm-state
result under uncontrolled provider caching, not a causal claim.

The data does **not** support either of these stronger claims:

- "ContextPilot improves cache hits" - no; hit tokens were identical and misses slightly higher.
- "Duplicate context destroys warm-cache performance" - not tested; dedup did not trigger.

## 7. Did context optimization lose critical facts?

No evidence of dedup-induced fact loss, because dedup did not run. The C1/C2 conflict task had
one/two wrong-authority outputs, but C0 also failed the same task once; this is model
instruction-following on the authority rule, not a demonstrated context-plane loss. The E2 report
keeps this distinction.

## 8. Practical conclusions

1. **Default to one worker for small/moderate coding tasks.** In this suite it was cheaper,
   faster, and far more reliable.
2. **Use static multi-worker only for genuinely independent writes plus an artifact-level merge.**
   The conflict task is the sole positive case; it does not justify a dynamic supervisor or broad
   parallelism.
3. **Do not advertise dedup benefits on short handoffs.** Measure dedup calls and actual removed
   bytes; zero is a valid result.
4. **Keep placement as a locality primitive.** It halves logical switches; do not equate that
   with token or cost reduction.
5. **Measure actual handoff bytes and consumption.** Worker lifecycle events and worker
   self-reports are insufficient; 8 runs had all workers "successful" while the task failed.
6. **Do not integrate ContextPilot into the coding adapter without a real prompt seam.** A
   feasibility-only conclusion is the honest one for Track B.

## 9. Boundary

- E1 is a 12-task, one-model, three-repetition study; E2 is an 8-workflow, one-model,
  three-repetition study.
- E1 is a static DAG. There is no dynamic supervisor, recursive delegation, or worker-created
  task to evaluate.
- E2 does not use sandbox tools and is not a coding benchmark.
- No combined "multi-worker + ContextPilot" benefit is claimed; the paths are separate.
