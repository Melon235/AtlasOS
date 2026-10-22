# Stage 4 Notes — Context-Locality Task Placement Planning

Frozen 2026-10-22. Code commit for formal runs: `391eb8c` (`git_dirty=false`).

## Pre-placement semantics (AnyCode 0.9.0 limitation)

`AnyCode._execute_tasks` calls `Scheduler.auto_assign(queue, agents)` once before building waves;
`_run_wave_task` only reads `task.assignee`. There is no ready-task placement hook. Dynamic
scheduling would require overriding the orchestration loop, which is prohibited.

AtlasOS therefore performs **deterministic pre-placement**: the planner assigns agents before
`compile_workflow()`, and AnyCode executes the already-placed DAG unchanged. This is placement
planning, not dynamic runtime scheduling.

## Planned vs observed context state

`PlacementContextState` is planner-simulated ownership. The planner **assumes dependency tasks
succeed** when simulating future output ownership; it does not observe runtime success. Actual
successful context remains represented by the runtime `ContextStore` / `ContextBlock` /
`ContextRef` path after execution. The state is per run (`fresh_per_run`), never persisted, and
is not provider KV state.

## Runtime path

```text
AtlasWorkflow -> Placement planner -> placed AtlasWorkflow
              -> compile_workflow -> AnyCode.run_tasks (unchanged)
```

`AtlasRuntime.run()` runs the planner when `placement.policy != bypass`; the default `bypass`
preserves Stage 1–3 semantics exactly. Placement traces and metrics are persisted in
`AtlasRunResult` / JSONL: policy, weights, decisions, mean locality, handoff refs/chars,
dependency switches, assignment distribution.

Policies: `static_baseline` (documented static reference, not upstream_default),
`capability_only` (S1), `context_locality` (S2). Capability is a hard filter; ties are
deterministic; no cache-history term.

## Formal offline results (`placement-stress`, deterministic, clean tree)

| variant | switches | mean locality | handoff refs | distribution |
|---|---:|---:|---:|---|
| S0 static | 6 | 0.000 | 6 | a=2, b=2, c=2, reviewer=1 |
| S1 capability | 6 | 0.000 | 6 | a=2, b=2, c=2, reviewer=1 |
| S2 locality | 2 | 0.800 | 2 | a=3, b=3, reviewer=1 |

Relative reduction S2 vs S1: dependency switches **66.7%**, logical handoff refs **66.7%**.
S2 keeps each chain on one worker; only the cross-chain merge hands off. Raw records:
`experiments/runs/stage4-formal.jsonl` (gitignored).

## Live functional validation (secondary metrics)

| variant | status | switches | locality | prompt | hit | miss | latency_ms |
|---|---|---|---:|---:|---:|---:|---:|
| S0 | succeeded | 2 | 0.0 | 910 | 128 | 782 | 4,109.6 |
| S1 | succeeded | 2 | 0.0 | 887 | 128 | 759 | 4,445.0 |
| S2 | succeeded | 2 | 0.0 | 890 | 128 | 762 | 4,228.3 |

The short functional workflow has fixed roles, so placement variants coincide; it validates
prompt correctness and integration, not locality. Provider metrics do not improve and are not
claimed.

## Tests

Placement unit tests cover capability rejection, version-aware locality, deterministic ties,
planned-state simulation, handoff and switch metrics. Runtime tests cover placement wiring,
persistence, and config validation. Full suite green (Stage 0–3 regressions included).

## Guardrails

No DAG/wave executor, task queue, or orchestrator copy; no provider-cache-history scheduling; no
physical KV claims. Logical handoff ≠ network bytes; logical locality ≠ KV locality.
