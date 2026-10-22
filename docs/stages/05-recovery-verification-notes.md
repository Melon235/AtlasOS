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
