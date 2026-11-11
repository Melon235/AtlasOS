# Stage G3-4a — Benchmark Integrity Isolation

Round: `g34a-20261111T060433Z`
Question: are the artifacts that G3-0/G3-3 workers were reading (`/tmp/*.stdout`, `/tmp/*.stderr`,
`/tmp/project.bundle`) an actual leak of grading information, and if so what is the minimal
evaluation-side fix?

**Answer up front: they were not an oracle leak.** They were (a) an AtlasOS provisioning staging
artifact and (b) the sandbox platform's own per-command output captures — both containing public or
worker-generated content, plus harness *identity* metadata. The fix removes the stale staging
artifact and the benchmark name from the public workspace's git history, keeps the hidden oracle
host-side (it already was), and adds a per-run invariant check that fails the run closed.

---

## 1. Evaluation lifecycle audit

Traced in the local tree (`experiments/evaluation/runner.py`, `fixtures.py`, `oracle.py`,
`src/atlasos/sandbox/{binding,provision,export,opensandbox_provider}.py`):

```
BenchmarkTask (stage_e_tasks.py, public text + visible_files + hidden_files)
  ↓  materialize_fixture()            run_dir/fixture  — visible files only            runner.py:524
  ↓  stage_git_repo()                 one commit in that repo                          runner.py:526
  ↓  ProjectSandboxManager.ensure_binding(source_repo=fixture)                          runner.py:739
       ↓ GitProjectProvisioner.inspect()   host temp file atlas-m5-*.bundle (git bundle HEAD)
       ↓ provider.write_file("/tmp/project.bundle")   ← staging artifact INSIDE the sandbox
       ↓ git clone /tmp/project.bundle /workspace/project
  ↓  worker execution                 AnyCode tool loop, sandbox only
  ↓  export_patch() + export_workspace()   run_dir/workspace (+ workspace.patch)
  ↓  evaluate_oracle()                HOST-side, run_dir/oracle-workspace
       ↓ copy_workspace(exported) + overlay hidden_files + pytest  → run_dir/oracle.log
  ↓  result.json / tool_conversations.json / trajectory/*
  ↓  manager.close() → provider destroy (sandbox_destroyed=true in every run)
```

Answers:

1. **Fixture created** in `run_dir/fixture` by `materialize_fixture` — `visible_files` only.
2. **Public task files** are copied from the in-repo `StageEDefinition.visible_files` dictionary.
3. **Hidden oracle files** live only in `BenchmarkTask.hidden_files` (memory) and are written to
   `run_dir/oracle-workspace` *after* the worker finished, on the host.
4. **Worker workspace** is `/workspace/project` inside the container.
5. **`/tmp` belongs to the sandbox container**, not to AtlasOS and not to the host.
6. **`/tmp/project.bundle`** is created by `GitProjectProvisioner.provision`
   (`src/atlasos/sandbox/provision.py:110`) — an upload of the fixture commit, used for the clone.
7. **`*.stdout` / `*.stderr`** are written by the **OpenSandbox exec server** (32-hex execution id
   per command). AtlasOS never writes them: `project.py`/`export.py` capture output through the SDK
   (`execution.logs`), not through files.
8. They appear **during** the run, one pair per executed command; the provisioning clone's own pair
   exists before the first model turn.
9. **Yes** — a worker can `cat /tmp/*.stdout` with an ordinary bash call; that is exactly what the
   G3-0/G3-3 probes did.
10. **The oracle runs on the host**, in `run_dir/oracle-workspace`, outside the sandbox lifecycle.
11. **No cross-run residue**: one fresh sandbox per run (`sandbox_created`/`sandbox_destroyed` true
    in all 12 runs), per-run directories under `experiments/results/...`.
12. **No host↔sandbox directory mapping**: the workspace is populated by `git clone` from an
    uploaded bundle and exported back with `git diff` + a tar manifest. `SandboxSpec` carries image,
    network (`none`), env and labels only — no volume mount.

---

## 2. Artifact provenance

Automated with `experiments/evaluation/stage_g3_4a_integrity.py audit`, which replay-reads recorded
transcripts (no live sandbox, no model) and classifies each tool result by markers only
(`provenance.csv` / `provenance.json`).

Scope: the G3-3 round (12 runs) plus the Stage E1 round (65 runs) — **77 runs, 143 worker tasks,
682 tool calls touching a suspicious path**.

| artifact | creator | stage | sandbox path | content category | worker visible | needed by worker | needed by oracle |
|---|---|---|---|---|---|---|---|
| `/tmp/project.bundle` | `GitProjectProvisioner.provision` (AtlasOS M5) | provisioning | `/tmp/project.bundle` | **HARNESS_INTERNAL** — copy of the public fixture commit + harness git identity | yes | no (only the clone needs it) | no |
| `/tmp/<32hex>.stdout` | OpenSandbox exec server | every command | `/tmp/*.stdout` | **HARNESS_INTERNAL / worker's own output** — 676 of 682 touches were the worker's own command output, 6 were platform/provisioning output | yes | no | no |
| `/tmp/<32hex>.stderr` | OpenSandbox exec server | every command | `/tmp/*.stderr` | same; the provisioning clone's stderr carries "Cloning into …" | yes | no | no |
| hidden tests (`tests/test_hidden_*.py`) | `BenchmarkTask.hidden_files` | after the worker exits | **never in the sandbox** | **ORACLE_PRIVATE** | **no** | no | yes (host) |
| `oracle.log`, `oracle-workspace/` | `evaluate_oracle` | after the worker exits | **never in the sandbox** | **ORACLE_PRIVATE / grading feedback** | **no** | no | yes (host) |
| harness logs (`result.json`, `tool_conversations.json`, `trajectory/`) | runner / recorder | run time, host | **never in the sandbox** | **HARNESS_INTERNAL** | **no** | no | yes (host) |
| previous runs (`experiments/results/**`) | runner | previous rounds | **never in the sandbox** | **HARNESS_INTERNAL** | **no** | no | no |
| fixture source, public tests, worker temp files | fixture / worker | run time | `/workspace/project`, `/tmp` | **PUBLIC_TASK_DATA** | yes | yes | n/a |

Evidence rows (from `provenance.csv`, content previews truncated to 240 chars):

```text
r0-eval-59581d807e8b / parser  cd /tmp && head -c 3000 <hex>.stdout
    → worker's own earlier `ls`/`find` output (report/*.py listing)     category=WORKER_OWN_OUTPUT
r1-eval-8417b5a70458 / stats   cd /tmp && for f in *.stderr; do cat "$f"; done
    → "Cloning into '/workspace/project'…" (provisioning log)           category=WORKER_OWN_OUTPUT
r2-eval-bd3223dfea83 / loader  cd /tmp && head -c 2000 <hex>.stdout
    → "commit 14d9f2a Author: AtlasEval … evaluation fixture e1-01-config-a1"
                                                                        category=WORKER_OWN_OUTPUT
```

Classification tally over the 682 touches: `WORKER_OWN_OUTPUT` 676, `PLATFORM_INTERNAL` 6,
`ORACLE_PRIVATE` **0**, `GRADING_FEEDBACK` **0**, `TEST_OUTPUT` **0**; grading-marker hits 10, all
from the workers' own `pytest` runs on their own code.

**Correction to G3-3.** That report said one e1-07 run "used the artifacts to inspect hidden-test
output". The audit does not support it: no tool result in any recorded run contains a hidden test
path or content. The claim was inferred from the *shape* of the command, not from its result. The
real finding is weaker but still an integrity problem: harness identity (`evaluation fixture
<sample_id>`, `AtlasEval`) was readable through the workspace's own git history and through the
staging bundle.

---

## 3. Worker sandbox visibility

| Observation surface | Before | After |
|---|---|---|
| `/workspace/project` (public files) | visible | visible |
| `git log` in the workspace | `evaluation fixture e1-02-report-a1` (author `AtlasEval`) | `initial project import` |
| `/tmp/project.bundle` | present for the whole run | removed before the first turn |
| `/tmp/<hex>.stdout|.stderr` | present (platform capture of the worker's own commands) | present (unchanged, see §10) |
| `/tmp` scratch (`mktemp`, tempfile, pipes) | works | works |
| host `run_dir` (oracle, logs, results) | not mounted | not mounted |
| other runs' sandboxes | separate containers | separate containers |

---

## 4. Confirmed leakage paths

1. **Staging bundle left behind** — `provision.py:110` uploads `/tmp/project.bundle`, the clone
   consumes it, and nothing deletes it. It is public data plus harness identity, and it was the
   single most attractive probe target (`project_bundle` appears in 77 of the 682 touches).
2. **Benchmark identity in the public git history** — `stage_git_repo(..., label=f"evaluation
   fixture {task.sample_id}")` wrote the sample id and the word "evaluation" into the only commit a
   worker can read. A worker doing `git log` learns it is inside a benchmark and which sample.
3. **Platform output captures** — `/tmp/<hex>.stdout|.stderr` are created by the sandbox server for
   every command. Content is the worker's own output; the risk is that they *look* like a grading
   channel and consume turns. Not removable without changing the sandbox platform (out of scope).

Not a leakage path (verified): hidden tests, `oracle.log`, `oracle-workspace`, `result.json`,
transcripts and previous runs — none is mounted, uploaded or referenced by a sandbox-visible path.

---

## 5. Minimal isolation design

```text
Trusted evaluation side                     Worker sandbox
──────────────────────                      ──────────────
BenchmarkTask.hidden_files  ──overlay──▶    (never)
run_dir/oracle-workspace    ──pytest───▶    (never)
run_dir/{result,tool_conversations,trajectory}
host fixture repo ──git bundle──▶ /tmp/project.bundle ──clone──▶ /workspace/project
                                   └── rm -f after clone (new)
                      workspace git history: neutral commit message (new)
```

Three evaluation-side changes, no sandbox policy change:

1. `enforce_evaluation_isolation()` (new, `stage_g3_4a_integrity.py`) runs after `ensure_binding`
   and before the first model turn: it records whether the staging bundle exists, deletes it,
   verifies the deletion, sweeps `find / -maxdepth 4` for private-name patterns, and returns a
   report. Any hit raises `EvaluationEnvironmentError`, so **no worker turn runs in an environment
   that still exposes a private artifact name**.
2. The fixture commit message is a neutral constant (`FIXTURE_COMMIT_LABEL`), so `git log` no longer
   announces the benchmark or the sample id.
3. The report's integrity fields are persisted in `metrics.integrity` for every run.

Rejected: prompt bans, shell blacklists, hiding files via the shell, deleting `/tmp`, changing
`SandboxProjectConfig`/provider permissions, moving hidden tests into the sandbox and deleting them
afterwards.

---

## 6. Changed files

| File | Change |
|---|---|
| `experiments/evaluation/stage_g3_4a_integrity.py` | **new** — provenance audit (`audit`), `enforce_evaluation_isolation`, `FIXTURE_COMMIT_LABEL`, `canary_for` |
| `experiments/evaluation/runner.py` | neutral fixture commit label; isolation step + `metrics.integrity`; fail-closed on integrity failure; `failure_class`/`failure_reason` initialised before the try (removes a latent `NameError` in the early-abort path) |
| `experiments/evaluation/stage_g3_4a_analysis.py` | **new** — baseline + integrity analysis |
| `experiments/evaluation/stage_e_rounds.py` | `g3-4a` suite (A0/A1 × 3 tasks × 2 reps, everything else pinned) |
| `tests/integration/test_stage_g3_4a_isolation.py` | **new** — 10 tests (A–G) |
| `tests/integration/test_stage_g3_4a_isolation_live.py` | **new** — real-OpenSandbox visibility verification (env-gated) |
| `.gitignore` | ignore `experiments/results/stage-g3-4a/` |

Nothing in `src/atlasos` changed: M5 provisioning, the sandbox provider, M1–M7, G1/G2a/G3-1/G3-2/
G3-3 are untouched. The staging-bundle upload in `provision.py` is still there — this stage cleans it
up from the evaluation side (see §10 for the recommended upstream fix).

---

## 7. Isolation tests

`tests/integration/test_stage_g3_4a_isolation.py` (fake provider + command-recording stub, no model):

| Test | Asserts |
|---|---|
| A `test_public_workspace_is_staged_for_the_worker` | `README.md`, `calc.py`, `tests/test_public.py`, `pyproject.toml` are staged; no hidden file, no hidden test name |
| B `test_private_oracle_is_materialized_only_outside_the_fixture` | the oracle dir holds the canary; **no file under the fixture contains it** |
| C `test_grading_logs_are_not_staged_into_the_fixture` | a canary in `run/oracle.log` never appears in any staged file; the harness can still read its log |
| D `test_runs_are_isolated_by_directory` | run A's canary is absent from run B's fixture and vice versa |
| E `test_normal_posix_temp_workflow_still_works` | `tempfile.mkdtemp()`, file round-trip and a piped subprocess still work in the sandbox |
| F `test_oracle_still_evaluates_after_isolation` | hidden tests load, `hidden_tests_passed=True`, `oracle.log` and `oracle-workspace/` are written |
| G `test_isolation_step_removes_staging_bundle` / `…fails_closed…` | bundle present → removed → absent, private sweep runs, and a hit makes `isolation_checks_passed=False` |
| — `test_fixture_commit_message_is_neutral` | commit subject == `initial project import`, no "evaluation", no sample id |
| — `test_workspace_tools_refuse_paths_outside_the_project` | `../../etc/passwd`, `/etc/passwd`, `/workspace/project/../secrets` raise; in-project paths normalise correctly |

`tests/integration/test_stage_g3_4a_isolation_live.py` (real OpenSandbox, `ATLASOS_RUN_LIVE_SANDBOX=1`)
— executed once for this report, **passed**:

1. `/tmp/project.bundle` present in the container before the fix step;
2. after `enforce_evaluation_isolation` it is absent;
3. `grep -rl <canary> /` inside the container → **empty** (content-based, canary never sent);
4. `find / -name 'test_hidden*'` → empty;
5. `/workspace/project/calc.py` and `tests/test_public.py` still readable;
6. `git log --pretty=%s` contains neither the sample id nor "evaluation";
7. symlink/traversal into `/workspace/project/../../oracle-workspace` → "No such file or directory",
   canary absent;
8. `mktemp` + `python3 tempfile` workflow succeeds;
9. the hidden test file with the canary still exists host-side for grading.

---

## 8. Full regression

```text
.\.venv\Scripts\python.exe -m pytest   → 597 passed, 1 skipped, 1 deselected in 210.59s
ruff check .                            → All checks passed!
ruff format --check .                   → 334 files already formatted
pyright                                 → 0 errors, 0 warnings, 0 informations
```

The skipped test is the env-gated live sandbox test (it was executed manually, see §7). No test was
deleted, and no oracle assertion, hidden test or scoring rule was modified (git shows no change under
`experiments/evaluation/oracle.py` or `stage_e_tasks.py`).

---

## 9. Re-established benchmark baseline

Round `g34a-20261111T060433Z`, 12 runs, pinned: `handoff=dual`, `budget=guarded`,
`tool_error=corrected`, `compaction=off`, `tool_result_mode=off`, `worker_contract_mode=legacy`,
DeepSeek chat, temperature 0, OpenSandbox, 120 k tokens, 14 turns, 60 tool calls.

| Task | Rep | A0 (single) | A1 (multi) |
|---|---|---|---|
| e1-02-report | 0 | PASS `TASK_SUCCESS` | FAIL `BUDGET_EXCEEDED` |
| e1-02-report | 1 | PASS `TASK_SUCCESS` | FAIL `BUDGET_EXCEEDED` |
| e1-07-export | 0 | PASS `TASK_SUCCESS` | FAIL `ORACLE_FAILURE` (2/2 workers "success") |
| e1-07-export | 1 | PASS `TASK_SUCCESS` | FAIL `ORACLE_FAILURE` |
| e1-12-conflict | 0 | PASS `TASK_SUCCESS` | PASS `TASK_SUCCESS` |
| e1-12-conflict | 1 | PASS `TASK_SUCCESS` | PASS `TASK_SUCCESS` |

| Metric | A0 | A1 |
|---|---|---|
| Oracle pass / `TASK_SUCCESS` | 6/6 · 6/6 | 2/6 · 2/6 |
| Worker success | 6/6 | 11/16 |
| Model turns | 44 | 119 |
| Tool calls | 49 | 124 |
| Prompt tokens | 123,806 | 432,956 |
| Completion tokens | 8,928 | 23,330 |
| Cache hit / miss | 114,432 / 9,374 | 380,160 / 52,796 |
| Cache hit ratio | 0.9243 | 0.8781 |
| Cost (USD) | 0.014212 (avg 0.002369) | 0.046116 (avg 0.007686) |
| Budget exceeded | 0 | 2 |
| Unrelated probe turns | 5 (3 of 6 workers) | 33 (9 of 16 workers) |
| Exploration ratio | 0.5526 | 0.5327 |
| First implementation turn | 4.75 | 5.50 |
| Isolation checks passed | 6/6 runs | 6/6 runs |

Per-run artifacts (`result.json`, `tool_conversations.json`, `provider_calls.jsonl`, `analysis/`)
are under `experiments/results/stage-g3-4a/g34a-20261111T060433Z/`.

Reference reading — *not* a causal comparison with earlier stages, because the environment changed:

* **A0 is now clean 6/6 at ~1/6 the cost of A1.** The single-worker configuration is the trustworthy
  reference for the next stage; multi-worker decomposition, not the environment, is the source of
  the remaining failures.
* `e1-07` reproduces the G3-I04 shape: in rep 0 **both** workers reported success and the hidden
  tests still failed — a contract/integration gap, not a delivery gap.
* `e1-02` A1 fails on the 120 k budget in both reps (1 of 3 workers succeeds), while A0 does the
  same task in 8–10 turns and 24–33 k prompt tokens.
* Probe turns did **not** disappear (5 in A0, 33 in A1) even though the bundle is gone and the git
  history is neutral: workers still run `find /`, `git log --all` and read `/tmp/*.stdout`. Those
  hits are now the platform's own captures of their own commands, so the behaviour is no longer
  rewarded with harness metadata — but it is not eliminated by isolation, and the report does not
  claim model behaviour improved.
* `ownership_violations=10` in A0 is a metric artefact: the ownership metric assumes a declared
  per-worker split, and the solo worker legitimately writes all three modules. A1 shows 2.

---

## 10. Remaining integrity risks

1. **`/tmp/<hex>.stdout|.stderr` still exist** (sandbox platform behaviour). Content is the worker's
   own command output, so they are not an answer channel; they remain a distraction and a
   platform-internal artifact. Removing them requires an OpenSandbox-side change (or running every
   command with output redirection into a per-run directory), which is out of scope here.
2. **The staging bundle is still uploaded by M5** and only deleted by the evaluation runner. Any
   other AtlasOS consumer of `ProjectSandboxManager` still leaves `/tmp/project.bundle` behind. The
   upstream fix (delete after the verified clone, inside `provision.py`) belongs to an M5 stage; it
   was not made here because the stage forbids touching M1–M7.
3. **Harness identity is still inferable** from the workspace layout (single-commit repo, stub
   `NotImplementedError` functions). The commit message no longer names the sample, but the fixture
   shape is recognisably a benchmark.
4. **The oracle still runs `pytest` on the host** with the repository's Python; a hidden test that
   shells out to the sandbox is impossible today, but the oracle has no sandbox of its own — it
   trusts the exported workspace, which is exactly what the `forbidden_paths_unchanged` and
   `only_allowed_paths_changed` checks guard.
5. **n=2 per cell** in the baseline. Directional only.

---

## 11. Final verdict

**PASS** for the stage's actual objective — trustworthy evaluation isolation — with two documented
residual risks (platform output captures, upstream staging cleanup).

Explicit answers:

1. **Who creates `/tmp/*.stdout` and `project.bundle`?** The OpenSandbox exec server creates the
   `*.stdout`/`*.stderr` pair per executed command; AtlasOS `GitProjectProvisioner.provision`
   creates `/tmp/project.bundle` (`src/atlasos/sandbox/provision.py:110`).
2. **Do they contain information a worker should not see?** No oracle content — 682 touches audited,
   0 hidden-test paths, 0 grading feedback. They did expose **harness identity** (sample id, fixture
   commit author/message) and consumed worker turns.
3. **How did the content reach the worker?** Through the container's own filesystem: the bundle was
   uploaded for the clone and never removed; the captures are written by the platform for every
   command. Nothing was mounted from the host, and the oracle never entered the sandbox.
4. **After the fix, is it really unreadable?** Yes for the private material: verified from inside a
   real container (`grep -rl <canary> /` empty, no `test_hidden*`, symlink/traversal refused,
   bundle removed). The platform's own captures remain readable but contain only the worker's own
   output.
5. **Did normal development break?** No: public files, `mktemp`/`tempfile`/pipes, `git`, `find`,
   `pytest` all still work (tests E and the live check).
6. **Is the hidden oracle unchanged and still grading?** Yes — no file under `oracle.py`,
   `stage_e_tasks.py` or any hidden test was modified; test F and all 12 baseline runs graded
   normally.
7. **New A0/A1 baseline?** A0 6/6 oracle pass at $0.002369/run; A1 2/6 at $0.007686/run, with
   e1-02 budget-bound and e1-07 failing integration while both workers report success.

Read-only `git diff --stat` (the working tree also carries the uncommitted G1–G3-3 work; no Git
write operation was performed):

```text
 .gitignore                                 |   7 +
 experiments/evaluation/runner.py           | 233 ++++++++-
 experiments/evaluation/stage_e_cli.py      | 123 +++++
 experiments/evaluation/stage_e_rounds.py   | 753 ++++++++++++++++++++++++++++-
 src/atlasos/integrations/anycode_engine.py |  93 +++-
 src/atlasos/sandbox/tools.py               |  49 +-
 src/atlasos/tools/conversation.py          |  33 ++
 src/atlasos/tools/dispatch.py              |  44 +-
 src/atlasos/tools/runtime.py               |   8 +-
 tests/integration/test_anycode_dag.py      | 705 ++++++++++++++++++++++++++++-
 10 files changed, 2029 insertions(+), 19 deletions(-)
```

New (untracked) files of this stage: `experiments/evaluation/stage_g3_4a_integrity.py`,
`experiments/evaluation/stage_g3_4a_analysis.py`,
`tests/integration/test_stage_g3_4a_isolation.py`,
`tests/integration/test_stage_g3_4a_isolation_live.py`,
`experiments/results/stage-g3-4a/g34a-20261111T060433Z/`, `docs/stage-g3-4a-report.md`.
