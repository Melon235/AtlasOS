# Stage 5 Snapshot — Durable Session Recovery (Formal)

Frozen 2026-10-22. Code commit `ece7536`; `git_dirty=false` for all three formal offline
records (`experiments/runs/stage5-formal.jsonl`).

## Workload

Deterministic fake inference. Two independent root tasks (`research-a`, `research-b`) feed one
dependent `review` task. Atlas context policy `task_scoped`, renderer `stable_deterministic`,
placement policy `capability_only`.

- **R0** — uninterrupted reference.
- **R1** — interrupt while `research-b` is in flight (after `research-a` is durable).
- **R2** — interrupt after both roots are durable and before `review` executes.

Interruptions are raised at verified durable boundaries (R1: provider call for the in-flight
task after the mid-wave checkpoint exists; R2: `task_start` for Review after the wave-0
checkpoint exists). No arbitrary sleep decides the boundary.

## Formal results

| Variant | success | completed before resume | completed reexecuted | in-flight reexecuted | rebuilt blocks | layout equivalent | final equivalent | recovery ms |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| R0 | true | 0 | 0 | 0 | 0 | — | — | — |
| R1 | true | 1 | 0 | 1 | 1 | true | true | 16.2 |
| R2 | true | 2 | 0 | 0 | 2 | true | true | 8.5 |

Task execution counts (adapter invocations, not inferred from final status):

```text
R0: research-a=1, research-b=1, review=1
R1: research-a=1, research-b=2, review=1
R2: research-a=1, research-b=1, review=1
```

Recorded checkpoint state before interruption:

```text
R1: wave_index=-1, Research A=completed, Research B=in_progress, Review=blocked
R2: wave_index= 0, Research A=completed, Research B=completed,  Review=pending
```

Append-only inference records by generation (`0` original, `1` resume):

```text
R0: {0: 3}          R1: {0: 2, 1: 2}          R2: {0: 2, 1: 1}
```

The completed root has no generation-1 record in R1/R2; the in-flight root has both.

## R0 vs R2 equivalence

All deterministic Atlas-owned representations match:

- recovered `ContextRef` set / `task-output:{task_id}` blocks at version 1, with identical
  content hashes for `research-a` and `research-b`;
- Review `context_layout_hash = ded6ce5315ee51d98a0b05277692da0e28b1111ce2a85b1b19b7f195c07dd95b`;
- Review full rendered-prompt hash
  `e38a01ae93601f552544c380416282c506808933a3581b87a7391a0566de13a4`;
- final deterministic output `review-out`.

R1 also matches R0 on all four equivalence checks even though `research-b` was invoked twice;
the duplicate in-flight invocation remains visible in the execution counts and inference
records and is not hidden.

## Reproduce

```bash
uv run python examples/stage5_recovery.py
```

The harness refuses to write formal records while the worktree is dirty. `ATLASOS_STAGE5_DRY_RUN=1`
prints the table without appending records.

## Boundary

This demonstrates task-level durable recovery: durably completed tasks were not re-executed in
the tested scenarios, and in-flight work has at-least-once semantics. It does not claim
exactly-once task execution, provider calls, tool effects, or external actions. Provider cache
state is not persisted and is irrelevant to correctness; recovery remains valid at 0% cache hit.
Live DeepSeek recovery was not run for Stage 5 (optional in the specification).
