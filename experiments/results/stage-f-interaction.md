# Stage F - Local AtlasOS CLI and Web Interaction

Implemented at `HEAD=456bf9b` (Stage F core), with later analysis-only commits not changing the
application layer.

## 1. Architecture

```
atlas CLI --\
             +--> AtlasApplicationService --> existing AtlasRuntime (workflow mode)
Web UI -----/                              \-> existing experiments.uat_cli subprocess (coding mode)
                                                  |
                                                  +--> AnyCode + Atlas M1-M7 + OpenSandbox
```

The service is a thin application layer:

- validates inputs and workspace scope;
- submits a run and starts the existing execution path;
- projects persisted `result.json` / `session.json` / `events.jsonl`;
- serves SSE events and history;
- delegates approval decisions to the existing M6 `ApprovalCoordinator`, but only when the
  approval is attached to an active trusted suspension and is one of that suspension's pending
  human decisions;
- refuses trusted resume when the application layer has no Stage D/M5 authority.

It does **not** schedule agents, execute tools, approve automatically, or reattach/destroy
sandboxes. `run.json` and `events.jsonl` are UI projections, not an execution state machine.

## 2. Files

```
src/atlasos/app/__init__.py
src/atlasos/app/service.py          # AtlasApplicationService
src/atlasos/app/cli.py              # Typer + Rich CLI
src/atlasos/app/web.py              # FastAPI + SSE
src/atlasos/app/static/index.html  # native HTML/CSS/JS, no build step
tests/unit/test_app_service.py
tests/unit/test_app_cli.py
tests/unit/test_app_approvals.py
tests/integration/test_app_web.py
```

Optional dependency group: `app` (`fastapi`, `uvicorn`, `typer`, `rich`, `httpx`). Console script:
`atlas = atlasos.app.cli:app`.

## 3. Run modes

### Workflow mode

Runs a static two-task AtlasOS DAG through the real DeepSeek model:

- `research` (researcher) -> `review` (reviewer), with `depends_on` and a context requirement.
- No tool execution and no sandbox. The UI labels this boundary explicitly.
- Real token/cache/cost/latency metrics come from `AtlasRunResult`.

### Coding mode

Delegates to the existing `experiments.uat_cli` composition:

- real self-hosted OpenSandbox, one coding worker;
- existing UAT fixture with `csv_parser.py`;
- visible tests may be run by the agent;
- workspace export and tool calls are projected by the app.

Coding mode intentionally rejects arbitrary host paths and non-UAT workspaces. That is a current
product boundary, not a fake success path.

## 4. CLI

```bash
pip install -e ".[app]"

atlas run "Fix the CSV parser so quoted commas are preserved" \
  --mode coding --workspace uat_repo --max-turns 10

atlas run "Explain why hidden tests stay outside the sandbox" --mode workflow
atlas run "..." --detach
atlas runs
atlas status <run-id>
atlas logs <run-id> --follow
atlas workers <run-id>
atlas approvals <run-id>
atlas approve <approval-id>
atlas reject <approval-id>
atlas resume <run-id>
atlas result <run-id>
atlas export <run-id> --output run-export.json
atlas web --host 127.0.0.1 --port 8000
```

`atlas run` waits for the terminal projection by default. This intentionally keeps the process
alive because the service executes in a background thread; `--detach` returns after submission.
JSON output is available on every command via `--json`.

## 5. Web UI

Start:

```bash
atlas web --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000`.

Pages/sections:

- **New Task**: task, mode, max turns, UAT workspace.
- **History**: persisted runs, status, failure reason; survives service restart.
- **Approvals**: pending approvals from the authoritative store, including the trusted review
  projection; Approve/Reject require an explicit click and confirmation.
- **Run Detail**: status, metrics, task/worker table (including explicit dependencies for the
  two-stage workflow), tool operations, artifacts, final output, events, approvals, recovery
  state.

SSE endpoint: `GET /api/runs/{id}/stream`; reconnect uses `Last-Event-ID` and the server sends only
events after that id. SSE reconnect never resubmits a task.

Security boundaries:

- default bind is `127.0.0.1`;
- API key stays in the server environment; never sent to the browser;
- no browser API exposes arbitrary host file reads or commands;
- coding workspace is restricted under `experiments/fixtures`;
- approval buttons call the backend authority, never a UI-only toggle;
- ordinary failure is never presented as a safe Stage D resume.

## 6. Real end-to-end evidence

### CLI coding run

- App run: `app-f4bbe92571e5`
- UAT run: `uat-ad7f437123164ac4a667d3b728feebf8`
- Mode: coding; real OpenSandbox self-hosted
- Status: `COMPLETED`
- Tool calls: 10
- Sandbox: created, ready, destroyed; network-none probe PASS
- Host fixture digest unchanged
- Workspace export: `experiments/runs/app-cli-e2e2/app-f4bbe92571e5/coding/uat-ad7f.../workspace`
- Changed files: 1 (`csv_parser.py`); agent also added visible tests in the sandbox

### Web coding run

- App run: `app-46d3fe2bddfd`
- Status: `COMPLETED`; cleanup error: null
- Tool calls: 11
- Same shared Application Service and UAT execution path
- Final output reported 9 passed visible tests

### Web workflow run

- App run: `app-8fe9eee83ea6`
- Status: `COMPLETED`
- Metrics: prompt 428, completion 804, cache hit 0, cache miss 428, cost $0.0010932
- SSE: first stream received ids 1-2; reconnect with `Last-Event-ID: 2` received ids 3-10 with no
  duplicates; final status `COMPLETED`.
- A second run on the restarted UI (`app-6168fc33a59a`) projected the real assignees
  `researcher`/`reviewer` and both tasks as `completed`.

### Approval authority path

A test fixture created a real M6 active suspension with one `REQUIRE_HUMAN` pending invocation and
a derived approval challenge. On the final Web service the projection listed it as `PENDING`; an
explicit `POST /api/approvals/{id}/approve` resolved it through `ApprovalCoordinator` to
`APPROVED` (version 1, actor `host:user:web`). An approval without an active suspension is refused
with HTTP 409 and remains `PENDING`. The UI never calls approve/reject on load, polling, refresh,
or SSE reconnect.

The same suspension projected as `RECOVERY_REQUIRED`; a resume request was refused with HTTP 409
because the app has no trusted Stage D reattachment/destruction authority. The projection contains
no `canonical_input` or held model message.

This fixture validates the application authority path; it is not claimed as an agent-generated
approval.

## 7. Test results

- `pytest tests/unit/test_app_service.py tests/unit/test_app_cli.py tests/unit/test_app_approvals.py tests/integration/test_app_web.py -q`:
  **18 passed**
- Full offline suite: **461 passed**
- Ruff: pass
- Pyright: 0 errors
- Real CLI coding, Web coding, Web workflow, and SSE reconnect runs recorded above.

## 8. Known boundaries

- No dynamic supervisor, autonomous delegation, or worker-created tasks.
- Coding mode supports only the existing UAT-compatible fixture path; arbitrary workspaces need
  a new coding-session composition, not a UI-only change.
- `atlas resume` / the Web recovery button refuse all ordinary app runs. Trusted Stage D recovery
  remains in the real OS-process recovery harness; the app does not reattach or destroy sandboxes.
- `RECOVERY_UNCERTAIN`, `CANCELLED`, and `SANDBOX_LOST` are display states only when backed by a
  real runtime/store signal; the UI does not fabricate them.
- Coding token/cost metrics are incomplete because the UAT session does not persist provider usage
  per tool call; the app reports what the session actually contains.
- The Web UI is local and unauthenticated by design; it must stay on loopback unless an
  authentication layer is added.

