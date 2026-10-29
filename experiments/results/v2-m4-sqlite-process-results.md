# M4.1 Real SQLite Cross-Process Durability — Results

## Provenance

```text
starting HEAD:          6316f94
implementation commit:  a0f2f55
verification commit:    a0f2f55（git_dirty=false）
formal record:          experiments/runs/v2-m4-sqlite-process.jsonl
```

## Dependency

```text
anycode-py     0.9.0
aiosqlite      0.22.1
source:        anycode-py[persistence]==0.9.0
declared in:   pyproject.toml [project.optional-dependencies].dev
lock:          uv.lock regenerated（aiosqlite 0.22.1）
```

The process tests explicitly construct `SQLiteToolIdempotencyStore`; no
`InMemoryToolIdempotencyStore` is used.

## Quality gates

```text
ruff check .             All checks passed
ruff format --check .    170 files already formatted
pyright                  0 errors, 0 warnings, 0 informations
pytest                   215 passed（clean tree, twice）
M4.1 process focused:     4 passed
```

## Formal metrics

| Metric | Result |
|---|---|
| processes_spawned | 48 |
| completed_restart_replays | 1 |
| unresolved_restart_in_progress | 2 |
| restart_conflicts | 1 |
| original_payload_replays_after_conflict | 1 |
| concurrency_rounds | 20 |
| concurrency_execute_outcomes | 20 |
| concurrency_in_progress_outcomes | 20 |
| concurrency_replay_outcomes | 0 |
| **dual_execute_rounds** | **0** |
| **duplicate_side_effects** | **0** |
| unexpected_sqlite_errors | 0 |
| child_process_failures | 0 |
| timeouts | 0 |
| P01 / P02 / P03 / P04 | true / true / true / true |

## P01 — Completed replay across process restart

```text
Process A: claim → execute → complete(result) → exit
Process B: new interpreter, same DB/key/fingerprint
           → replay
           → same ToolResult
```

Different PIDs; durable test-provider side-effect count = 1.

## P02 — Unresolved claim survives restart

```text
Process A: claim → execute → exit without complete
Process B: in_progress
Process C: in_progress
```

Three distinct PIDs. No side effect. No delete; no execute; no replay.

## P03 — Conflict survives restart

```text
A: payload A → execute → complete
B: payload B, same key → conflict
C: payload A → replay original result
```

The conflict did not corrupt the original replayable record.

## P04 — Concurrent independent processes

```text
20 rounds × 2 independent Python interpreters
shared DB/key/fingerprint + filesystem ready/GO barrier
```

Each round:

```text
execute outcomes: 1
in_progress/replay: 1
provider effects: 1
```

No dual execute and no duplicate side effect across 20/20 rounds.

## Process evidence

Each child was started with `sys.executable`, separately importing AnyCode and constructing
`SQLiteToolIdempotencyStore` on the shared temp DB. No object, thread, fork, or same interpreter
was shared. Assertions use only public `claim()`/`complete()` outcomes, never AnyCode private SQL.

The side-effect count is a separate test-provider SQLite artifact; no AnyCode table was read or
mutated by Atlas.

## What this proves

```text
completed claim durability across process boundaries
stored ToolResult replay in a new process
unresolved claim durability and non-deletion
same-key/different-input conflict durability
original record preservation after conflict
atomic single-winner behavior across independent processes
cross-process duplicate suppression
```

It does not prove provider-side idempotency or exactly-once execution.

## Limitations

- Provider idempotency remains a fake contract; no production adapters.
- No durable HITL (M6), sandbox (M5), or distributed idempotency.
- SQLite concurrency is AnyCode's public behavior; Atlas adds no retry loop.
- `CONFIRMED_NOT_EXECUTED` cannot safely clear an unresolved AnyCode claim.

## Reproduction

```powershell
cd D:\Work\AtlasOS

.venv\Scripts\ruff.exe check .
.venv\Scripts\ruff.exe format --check .
.venv\Scripts\pyright.exe
.venv\Scripts\python.exe -m pytest

.venv\Scripts\python.exe -m pytest tests\integration\test_sqlite_idempotency_process.py
.venv\Scripts\python.exe -m pytest tests\integration\test_durable_actions.py
.venv\Scripts\python.exe -m pytest tests\integration\test_m1_m4_reliability.py
.venv\Scripts\python.exe -m pytest tests\integration\test_action_safety.py
.venv\Scripts\python.exe -m pytest tests\integration\test_control_plane_e2e.py

# Formal record requires clean worktree.
.venv\Scripts\python.exe examples\m4_sqlite_process_eval.py
```
