# Stage 5 Notes — Durable Session and Recovery (Partial)

Base: Stage 4 frozen at `38b83cc`. Verified upstream resume semantics are in
[05-recovery-verification-notes.md](05-recovery-verification-notes.md).

## Implemented in this session

- `AtlasSessionManifest` (schema v1): run identity, git metadata, workflow/config hashes,
  versions, placement/context/renderer/transform labels, checkpoint location, status.
  Atomic temp-file + `os.replace` write; secrets are never stored.
- `verify_compatible()`: fails fast on workflow hash, config hash, placement policy, context
  policy, renderer policy, or transform mismatch; unknown schema rejected on read.
- `rebuild_context_store()`: builds deterministic `task-output:{task_id}` blocks (version 1,
  source `anycode_task:{id}`, owner assignee) **only** from checkpoint tasks whose status is
  `completed` and whose output is present. Failed/pending tasks produce nothing; nothing is
  inferred and no task is re-run.
- Unit tests: manifest round-trip/schema/secret check, compatibility rejection, completed-only
  reconstruction, deterministic rebuild.

## Not yet implemented (remaining Stage 5 work)

- Runtime `resume` path (`AtlasRuntime.run(..., resume_from=...)`) that loads the manifest,
  validates compatibility, calls `run_tasks(resume_from="latest")`, rebuilds context, and
  persists `result.json` / recovery metrics.
- Deterministic fault injection (`InjectedCrash`, after-checkpoint hook) and the R0/R1/R2
  offline experiment with fake providers.
- Metrics append across resume, `resume_count`, `execution_attempt`.
- Live DeepSeek resume validation.
- Formal clean-worktree recovery results.

Stage 5 is therefore **not frozen**. No custom checkpoint engine, event sourcing, or exactly-once
side-effect claim exists; AnyCode remains execution truth.
