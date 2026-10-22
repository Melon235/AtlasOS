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
