# Stage 5 Verification — AnyCode 0.9.0 Checkpoint / Resume

Inspected pinned `anycode-py==0.9.0` source.

## Checkpoint data

`CheckpointData` persists `tasks` (status, output, assignee, dependencies, timestamps),
`agent_results`, `wave_index`, `total_token_usage`, and metadata. It is written after each
completion wave and after each completed task within a wave.

## Resume entry point

`AnyCode.run_tasks(..., resume_from=...)`:

- `"latest"` loads the newest checkpoint for the computed workflow id;
- any other value loads that checkpoint id via `checkpoint.store.load(id)`;
- `detect_spec_change()` rejects changed task specs;
- restored state = checkpoint tasks, agent results, token usage, `start_wave = wave_index + 1`.

## Behavior

- Tasks already `completed` in the checkpoint are skipped (`pending_tasks` filters them).
- `failed` / `in_progress` / `pending` tasks in remaining waves are executed again.
- Assignees from the checkpoint are preserved; the scheduler only auto-assigns tasks without one.
- Interruption during an LLM call leaves the task non-completed, so it is re-executed after
  resume: durable-completed skip + at-least-once for in-flight work.
- The original `TeamConfig`/workflow must be supplied; spec drift is rejected upstream.
- Checkpoint location is configured by `CheckpointConfig(path=...)`.

## AtlasOS side

AnyCode checkpoints remain execution truth. AtlasOS adds only `AtlasSessionManifest` (identity,
hashes, policies, versions, checkpoint location) and deterministic context reconstruction from
completed task outputs.

## Empirical partial-wave probe (deterministic)

Setup: wave 0 = A, B; C depends on A+B. Agent-b adapter waits until the checkpoint file shows
`A=completed` and `B!=completed`, then raises a non-retryable `InjectedCrash`.

Observed checkpoint: `wave_index = -1`, tasks `A=completed`, `B=in_progress`, `C=blocked`.

Resume via `run_tasks(..., resume_from="latest")` on a fresh engine:

- A remained completed and was **not** re-executed (adapter calls: A stayed at 1),
- B was re-executed (in_progress -> adapter calls 2),
- C executed and the run succeeded.

Conclusion: AnyCode 0.9.0 supports **task-level durable recovery** at partial-wave boundaries.
Guarantee: completed tasks are skipped; in-flight tasks may re-execute (at-least-once). The
non-retryable fault must be a `BaseException`; a plain `RuntimeError` is retried by
`ResilientAdapter` and never reaches the recovery boundary.

## AtlasOS integration verification (commit `ece7536`)

The runtime-level tests reproduce the same boundaries through `AtlasRuntime` and additionally
verify the Atlas-owned invariants:

- R2 checkpoint: `wave_index=0`, roots completed, Review not completed; resume calls Review
  once and does not call either root again.
- R1 checkpoint: `wave_index=-1`, A completed, B in_progress, Review not completed; resume
  re-executes only B, then Review. Adapter calls: A=1, B=2, Review=1.
- The recovered `task-output:{id}` blocks exist in the ContextStore before the resumed Review
  prompt is built. Disabling the rebuild makes Review's prompt construction fail with
  `resume context invariant violated`, and Review's provider is never called.
- Rebuild plus resume leaves exactly one version of each logical block.
- `inference.jsonl` keeps generation-0 records and appends generation-1 records; the completed
  root has no generation-1 record.
- R0/R2 Review records agree on `context_layout_hash`, `full_prompt_hash`,
  `stable_prefix_hash`, rendered dependency order, recovered block content hashes, and final
  output.

Formal records: [stage5-baseline.md](../../experiments/results/stage5-baseline.md).

### Latest-checkpoint ordering (Stage 6 fix)

AnyCode's `resume_from="latest"` selects the lexicographically last checkpoint filename.
Checkpoint ids are UUIDv7, whose ordering is only millisecond-granular: two checkpoints written
in the same millisecond can sort in the wrong order, and `created_at` can also collide. AtlasOS
selects the most advanced checkpoint by durable execution state (completed-task count, wave
index, `created_at`, file mtime) and passes that exact checkpoint id to `run_tasks`. Regression
tests construct checkpoints whose filenames and timestamps invert the true order and assert the
advanced state wins. Stage 5's R0/R1/R2 counts and equivalence checks are unchanged after the
fix.
