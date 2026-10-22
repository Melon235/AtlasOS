# Stage 5 Notes — Durable Session and Task-Level Recovery

Base: Stage 4 frozen at `38b83cc`; resume implementation frozen at `ece7536`. Upstream resume
mechanics are verified in [05-recovery-verification-notes.md](05-recovery-verification-notes.md);
formal results are in [stage5-baseline.md](../../experiments/results/stage5-baseline.md).

## Architecture boundary

```text
AnyCode checkpoint          execution truth
AtlasSessionManifest        session identity + compatibility
Atlas ContextStore          derived logical context
metrics / result.json       observations, not checkpoint truth
```

No custom DAG, wave, checkpoint, or resume engine exists. AtlasOS calls
`AnyCode.run_tasks(..., resume_from="latest")` and never marks upstream tasks complete itself.

## Manifest schema v2 (`ece7536`)

Durable fields: `schema_version=2`, `run_id`, `created_at`, `git_commit`, `git_dirty`,
`workflow_hash`, `workflow_input`, `semantic_config_hash`, `placed_assignment_hash`,
`anycode_version`, `contextpilot_version`, `model`, `placement_policy`, `placement_weights`,
`context_policy`, `renderer_policy`, `context_transform`, `checkpoint_location`, `status`.

The ambiguous v1 `config_hash` is gone. `semantic_config_hash` covers provider/model/base URL,
context policy/renderer/transform, scheduler policy, and placement policy/weights.
`placed_assignment_hash` covers the canonical `task_id -> agent_id` mapping. Both exclude run
id, resume marker, fault injection, paths, logging, live flags, timestamps, and secrets. Unknown
schema versions fail fast; no migrations exist.

`workflow_hash` now includes workflow input as well as tasks, agents, dependencies, required
capabilities, and required context.

## Single resume API

`AtlasRuntime.resume(run_id, workflow, experiment=..., persist=...)` is the only recovery entry
point. There is no `run(..., resume_from=...)` variant.

Order inside `resume`:

```text
resolve run dir -> read manifest -> validate schema
-> reconstruct workflow input from manifest
-> deterministic placement -> verify workflow/semantic/placement hashes
-> load latest AnyCode checkpoint (by created_at)
-> rebuild ContextStore from completed checkpoint tasks
-> configure generation-1 metrics
-> run_tasks(resume_from=<exact latest checkpoint id>)
-> normalize -> append metrics -> write result.json
```

The latest checkpoint is selected by its durable `created_at` field, and that exact checkpoint id
is passed upstream. Checkpoint filenames use millisecond-granular UUIDv7 ids, so same-millisecond
checkpoints can sort incorrectly by filename; upstream `resume_from="latest"` sorts filenames.
The Stage 6 regression test covers this ordering bug.

Compatibility rejects changed workflow/input, semantic configuration, placement policy or
weights, task-to-agent mapping, and schema. Operational changes (paths, logging, pricing,
timestamps) do not enter the semantic identity.

## Rebuild-before-execution invariant

`rebuild_context_store()` creates `task-output:{task_id}` blocks at version 1 only for
checkpoint tasks with status `completed` and a durable output. During a resumed generation,
`_build_atlas_prompt` does **not** re-project recovered tasks from the restored queue: their
context comes only from the rebuilt store. Before building a resumed task prompt it verifies
that dependency outputs recovered from the checkpoint are present; if a resume skipped the
rebuild, Review's prompt construction fails and its provider is never called. Live projection
is limited to tasks that actually completed in the current generation, so rebuilding plus
resuming cannot create duplicate logical blocks (`put` overwrites the same block id/version).

`PlacementContextState` remains planner-simulated; observed recovery context comes only from
completed checkpoint tasks.

## Deterministic interruption

`InjectedCrash(BaseException)` lives in `atlasos/durability/faults.py` and is raised only by
tests/experiments. R1 raises it inside `research-b`'s provider call after `research-a` is durable
and the in-flight status exists; R2 raises it in the runtime event hook at Review's `task_start`,
after the wave-0 checkpoint and before Review's prompt/provider call. The hook is opt-in
(`AtlasRuntime(..., event_hook=...)`) and is not used by production paths.

## Recovery metrics and append-only records

`AtlasRunResult.recovery` carries `resumed`, `resume_count`, `recovery_latency_ms`,
`completed_tasks_before_resume`, `completed_tasks_reexecuted`,
`inflight_tasks_before_resume`, `inflight_tasks_reexecuted`,
`unfinished_tasks_executed_after_resume`, and `context_blocks_rebuilt`.

Inference observations are appended to `runs/<run_id>/inference.jsonl` as each call happens,
with `resume_generation` (`0` original, `1` resume). A crash therefore preserves pre-crash
records; a resume appends generation-1 records to the same file. The final normalized
observation is `runs/<run_id>/result.json`; a session with a manifest and checkpoint but no
`result.json` is recognizably incomplete. Neither file is checkpoint truth.

## Formal findings

R0 (uninterrupted), R1 (sibling in flight), and R2 (roots durable, Review not run) all succeed.
Execution counts: R1 `A=1 B=2 Review=1`; R2 `A=1 B=1 Review=1`. R2 rebuilds both root blocks
before Review; R1 rebuilds only the durable root and projects the recovered in-flight sibling
after it completes. R0/R2 (and R1/R0) agree on ContextRefs, block content hashes,
`context_layout_hash`, full prompt hash, and final output. Formal records use
`git_dirty=false` at commit `ece7536`.

## Limitations

- One resume generation is supported and exercised; repeated/recursive resume is out of scope.
- Only task-level at-least-once semantics are available. No exactly-once claim is made for
  tasks, provider calls, tools, or external side effects.
- Provider cache/KV state is never persisted or restored; recovery correctness is independent of
  cache hit rate.
- Live DeepSeek recovery (optional) was not run; formal evidence is deterministic fake inference.
- The workload is side-effect-free by construction.

Stage 5 is frozen on this evidence. Stage 6 has not been started.
