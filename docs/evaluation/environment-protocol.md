# Environment Protocol

Three separate environments, three separate result streams. Results from one environment are never
merged into another environment's statistics.

---

## E1 — Development and offline regression (the only main environment)

| Item | Value |
|---|---|
| Path | `D:\Work\AtlasRuntime` |
| Interpreter | `D:\Work\AtlasRuntime\.venv\Scripts\python.exe`, CPython **3.12.15** |
| Install | `pip install -e ".[app,dev,evaluation,sandbox]"` + `".[tools]"` |
| Runtime base | `anycode-py[openai]==0.9.0` (pinned; never upgraded) |
| Third-party | `opensandbox 1.2.0`, `daytona 0.231.1`, `inspect-ai 0.3.289`, `mcp 1.30.0`, `sentence-transformers 6.3.0`, `faiss-cpu 1.16.0`, `torch 2.15.0`, `pydantic 2.14.3`, `numpy 2.5.4` |
| Forbidden in E1 | `contextpilot` (its `.pth` hook breaks 9 unrelated child-process tests — Final-1.6 §4) |
| Measured baseline | 891 passed / 6 skipped / 1 deselected, 213.74 s |
| Lock | `uv.lock` is **not** regenerated; `uv` is not installed here, so pip with the declared extras is the only supported path |

Mandatory invocation for any offline run:

```powershell
$env:ATLASOS_RUN_LIVE_TESTS = "0"
$env:ATLASOS_RUN_LIVE_SANDBOX = "0"
.\.venv\Scripts\python.exe -m pytest
```

E1 hosts: T1 functional suites, T2 microbenchmarks that need only stdlib/scientific libs, the CLI, the
benchmark inventory, the offline demo, and every module study that does not call a model.

Prohibitions: no second project installed into this venv; no dependency version changed to make a test
pass; no third-party source edited.

---

## E2 — Isolated ContextPilot environment

Purpose: run the **real** ContextPilot transform without contaminating E1.

| Item | Rule |
|---|---|
| Location | a separate venv (e.g. `D:\Work\AtlasRuntime\.venv-cp`), never E1 |
| Commit | must be the same runtime commit as the main environment; recorded in every output |
| Python | same minor version as E1 (3.12.x) unless the wheel demands otherwise — record it |
| Extra | `.[contextpilot]` only, plus `pytest` for the dedicated suites |
| Versions | record `contextpilot` version, its resolved scientific stack, and any version that differs from E1 |
| Startup artefact | the `.pth` hook behaviour **must** be recorded verbatim (it raises `ModuleNotFoundError` at interpreter start in this platform and contaminates child-process stderr) |
| Scope | only context-related work: the 4 ContextPilot tests, the dedup microbenchmark (A1-05), the content-preservation checks |
| Isolation of results | E2 results are reported in their own table with `environment=E2`; they are **never** added to E1 pass counts |

Procedure:

1. Create the venv, install `.[contextpilot]` (+ `pytest`), install the project editable from the same
   worktree.
2. Record `pip freeze`, the commit, and the first three lines of interpreter stderr.
3. Verify the API is importable (`from contextpilot import dedup_chat_completions`).
4. Run the 4 dedicated tests and the microbenchmark; capture which code path executed
   (`transform_name` must be `contextpilot_dedup`, not `identity`).
5. Record whether the transform actually fired (`deduplicated=true`, `original_block_count !=
   optimized_block_count`). A run where dedup fired 0 times is reported as such, never as a positive.
6. Tear down the venv afterwards; do not leave it as a second supported environment.

If the real transform cannot be executed (import failure, hook prevents a clean process, API drift),
the module is marked **BLOCKED** and no substitute dedup algorithm is written.

---

## E3 — Live model and sandbox environment

Everything here is frozen before a formal run and recorded in the run header.

| Category | Fields |
|---|---|
| Provider | name (`deepseek`), base URL, API key source (`DEEPSEEK_API_KEY`, never logged) |
| Model | requested id, **resolved** id (the runner records both), temperature, `max_tokens`, timeout, retry policy |
| Python | interpreter path/version, `pip freeze` digest |
| Sandbox | provider + version, image reference + digest, `network="none"`, workspace root, CPU/memory fields as accepted by the provider (record `UNAVAILABLE` when the provider does not enforce them) |
| Tools | tool catalog digest, search backend, `max_results`, MCP servers (none by default) |
| Workspace | fixture digest of the initial tree; a **fresh** materialisation per run |
| Oracle | oracle mode (`sandbox`), oracle image digest, hidden-test bundle digest |
| Budget | total tokens per task, max tool calls, max turns per worker, wall-time timeout |
| Concurrency | max parallel workers, max concurrent samples |

Rules:

1. A run never starts from a workspace mutated by a previous run; the digest before and after is
   recorded.
2. `git_dirty` must be `false`; a dirty tree makes the run non-comparable and is disclosed.
3. The API key is read from the environment, never written to a result file, never echoed.
4. Provider-side errors are classified (`RATE_LIMIT`, `TIMEOUT`, `PROVIDER_ERROR`) and are retried at
   most once under a pre-declared rule; the attempt count is recorded.
5. Cache state is recorded as "first-in-run" vs "warm", never as "cold" (the provider controls it).

### Environment digest

`environment_digest = sha256(git_commit | python_version | sorted(pip freeze) | model config |
sandbox image digest | oracle digest | runner settings digest)`. It appears in every result header so a
table can be proven to belong to one environment.

---

## Fault-injection safety (only if a kill experiment is approved)

A forced-termination experiment may run only when **all** of the following exist before the first kill:

1. Exact sandbox ownership: run id → provider sandbox id(s), recorded by the harness.
2. The set of sandboxes created by this run, captured before the kill (list + creation timestamps).
3. A resource inventory: containers, workspaces, temporary directories, databases.
4. A verifiable cleanup procedure: destroy by id, then re-list to prove absence; the result is written
   to `cleanup.json`.
5. A rule for unknown side effects: an `IN_PROGRESS` ledger row is reported as *unknown*, never as
   "did not happen"; no automatic replay.
6. An orphan-reaping step, executed and recorded even when the runtime has no sweeper.

An operator-run cleanup is acceptable and does not require building an automatic sweeper. What is not
acceptable is a kill experiment with no cleanup plan.

Out of scope for this round: measuring ordinary coding crash recovery (no checkpoint exists), and any
kill experiment whose purpose is a completion-rate claim.

---

## Cross-environment rules

| Rule | Statement |
|---|---|
| X-1 | A result file records `environment=E1|E2|E3` and the environment digest. |
| X-2 | Pass counts from different environments are never summed. |
| X-3 | A module measured only in E2 is reported with `environment=E2` in the matrix. |
| X-4 | A live run (E3) is never required for the offline suite to pass. |
| X-5 | Moving a result between environments requires a new run, not a relabel. |
