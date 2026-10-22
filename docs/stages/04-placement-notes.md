# Stage 4 Notes — Context-Locality Task Placement (Partial)

Recorded 2026-10-22. Base commits: Stage 3 closure `6f6d943`.

## AnyCode 0.9.0 placement seam

Inspected pinned source: `AnyCode._execute_tasks` calls `Scheduler.auto_assign(queue, agents)` once,
before building dependency waves, then `_run_wave_task` reads `task.assignee`. There is no
per-ready-task callback and no placement hook in `OrchestratorConfig`/`AgentConfig`.

**Dynamic ready-task placement: NO**, without overriding `_execute_tasks` (prohibited).

**Chosen fallback:** AtlasOS placement planning. Assignees are decided deterministically before
compilation; AnyCode executes the already-placed DAG unchanged. This is called *placement
planning*, not runtime scheduling.

## Models

- `AgentProfile(agent_id, capabilities)`
- `TaskRequirements(required_capabilities, required_context)`
- `ContextRequirement(logical_key, source_task_id, required_version)` — version-aware when an
  exact version is known; no fake future `ContextRef`s.
- `AgentContextState(available_refs, consumed_task_ids, produced_task_ids)` — per-run logical
  working set, never provider KV state.
- `PlacementDecision` keeps candidate scores, required/matched context counts, handoff refs.

## Policies

| variant | policy | semantics |
|---|---|---|
| S0 | `static_baseline` | keeps the workflow's pre-assigned agents; documented static reference, **not** upstream_default |
| S1 | `capability_only` | hard capability filter, deterministic load tie-break, no locality |
| S2 | `context_locality` | capability filter + logical locality score minus load |

Capability filtering happens before scoring; incapable agents can never win. Tie-breaking is
deterministic (score, load, agent id).

## Offline deterministic benchmark

`placement_stress_workflow()`: A1→A2→A3 and B1→B2→B3 plus Merge; three interchangeable research
workers and one reviewer. No sleeps, no network, no LLM.

| variant | dependency agent switches | mean locality | logical handoff refs |
|---|---:|---:|---:|
| S0 static | 6 | 0.000 | 6 |
| S1 capability | 6 | 0.000 | 6 |
| S2 locality | 2 | 0.800 | 2 |

S2 keeps each chain on one worker (a1/a2/a3 on worker_a, b1/b2/b3 on worker_b), reducing
dependency switches and logical handoff from 6 → 2. The merge task requires context split across
both workers, so it hands off 2 refs, giving locality 0.8 mean.

Reproduce: `uv run python examples/stage4_placement.py`.

## Tests

`tests/unit/test_placement.py`: locality ratios (2/3, 1/3, 0), version mismatch, capability
rejection, deterministic ties, chain-switch and handoff reduction, explicit requirements.
Full suite result at freeze time: see final verification below.

## Status: PARTIAL

Completed: seam documentation, models, S0/S1/S2 policies, deterministic planner, offline
benchmark, unit tests.

Not completed: runtime/config integration (`placement` config section), live S0/S1/S2 functional
validation, optional T0/T1 factorial, removal of wall-clock delay tests, formal clean-worktree
run records. Stage 4 is therefore **not** frozen and Stage 5 has not been started.

## Guardrails preserved

- No DAG/wave executor, task queue, or orchestrator copy.
- No provider-cache-history scheduling.
- No physical KV/cache claims: locality is logical AtlasOS context state only.
- All Stage 0–3 invariants untouched; only additive models/policies/planner/tests/examples.
