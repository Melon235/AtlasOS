# M4.1 — Real SQLite Cross-Process Durability

Status: verification only. No M4 redesign and no production semantic change.

```text
This verification uses real independent Python processes
and AnyCode SQLiteToolIdempotencyStore.

It does not use the in-memory backend.
```

## Dependency

The process tests need AnyCode's persistence extra:

```text
anycode-py[persistence]==0.9.0
  → aiosqlite>=0.20
```

It is declared in the `dev` optional dependency group (the test/CI dependency position) and
`uv.lock` was regenerated. The verified environment reported:

```text
anycode-py  0.9.0
aiosqlite   0.22.1
```

## Process model

The parent test spawns new interpreters with:

```text
sys.executable tests/helpers/sqlite_idempotency_process_worker.py ...
```

Each child:

```text
imports AnyCode itself
constructs SQLiteToolIdempotencyStore(db_path)
calls only public claim()/complete()
prints one JSON result
exits
```

No store object, memory state, thread, or forked state is shared. SQLite WAL/SHM files are not
used as assertions; outcomes come from the public API.

## P01 — completed replay

```text
A: claim → execute → complete(result) → exit
B: new process, same DB/key/fingerprint → replay, same ToolResult
```

A and B have different PIDs. A separate durable side-effect counter records exactly one effect.

## P02 — unresolved claim

```text
A: claim → execute → exit without complete
B: new process, same key/fingerprint → in_progress
C: another new process → still in_progress
```

No side-effect counter row. No `delete()`; unresolved state survives repeated restarts.

Atlas M4 semantics: in_progress after restart means no blind retry and classifies as UNCERTAIN
where recovery observation is unavailable.

## P03 — conflict and original record

```text
A: payload A → execute → complete
B: payload B, same key → conflict
C: payload A → replay original result
```

The conflicting attempt does not destroy the original replayable record.

## P04 — real concurrent processes

Two fresh interpreters open the same SQLite DB, write ready files, wait for a common GO file, then
call claim with the same key/fingerprint.

```text
rounds: 20
allowed: {execute, in_progress} or {execute, replay}
forbidden: {execute, execute}
```

The winner increments a separate durable test-provider counter and completes. The loser must not
execute a side effect.

## Side-effect proof

The fake provider counter is a separate test SQLite file with its own public test table, never
AnyCode's private table. The test reads only that counter artifact, not AnyCode schema.

## Limitations

- This proves AnyCode SQLite process durability, not provider-side idempotency.
- No production provider adapters.
- No durable HITL (M6), no sandbox (M5), no distributed idempotency.
- SQLite lock behavior is AnyCode's; tests do not add infinite retry.
- `CONFIRMED_NOT_EXECUTED` claim release remains an M4 limitation.

## Reproduce

```powershell
cd D:\Work\AtlasOS

.venv\Scripts\python.exe -m pytest tests\integration\test_sqlite_idempotency_process.py
.venv\Scripts\python.exe examples\m4_sqlite_process_eval.py
```
