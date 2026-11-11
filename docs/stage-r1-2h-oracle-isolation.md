# Stage R1-2H — Evaluation Oracle Isolation Hardening

**Project:** AtlasRuntime (repository still `Melon235/AtlasOS`, package `atlasos`).
**Scope:** move hidden-test execution off the Host. No agent capability, no scheduler, no prompt, no
budget, no test content, no scoring rule changed.

R1-2A isolated *public-contract* checks. It did not isolate the **hidden** oracle:
`experiments/evaluation/oracle.py::_run_hidden_tests` ran `pytest` on the Host, inside
`run_dir/oracle-workspace`, which contains the agent's exported Python, with `env={**os.environ}`
and `PYTHONPATH` pointing at that tree. R1-2H closes that path.

**Answer in one paragraph.** Yes, the Host no longer executes agent-generated code: the hidden tests
now run in a fresh, network-disabled, throwaway sandbox with no Host environment and no Host secret
references, created after the workspace export and destroyed before the verdict is returned. Scoring
is unchanged — 28 historical workspaces re-scored with the isolated oracle agreed with the saved
Host verdict 28/28, including both pass and fail cases, and the hidden-test failure wording is
byte-identical. Environment defects (dead sandbox, timeout, missing pytest session) are now reported
as `ENVIRONMENT_FAILURE` instead of being charged to the model. The price is time: about **6 s** and
one container per evaluation, ~3.1 s of which is sandbox creation.

---

## 1. Risk that existed before this stage

| Aspect | Pre-R1-2H behaviour | Consequence |
|---|---|---|
| Where pytest ran | `subprocess.run([sys.executable, "-m", "pytest", ...], cwd=run_dir/"oracle-workspace")` | Agent-authored modules were imported by a Host process |
| Environment | `env={**os.environ}` | `DEEPSEEK_API_KEY` and any other Host credential were readable by agent code |
| Filesystem | Host working directory | Agent code could read/write the repository and the user profile |
| Network | Unrestricted | Agent code could reach the internet |
| Timeout | `subprocess.run(timeout=120)` raises `TimeoutExpired` | The run crashed with `RUNTIME_FAILURE`, not an environment verdict |
| Resource limits | none | A fork bomb or a memory hog would hit the Host |

The oracle workspace overlay (`copy_workspace` + hidden files) was correct about *visibility* — the
agent never saw the tests — but wrong about *execution isolation*.

## 2. New call chain

```text
export_workspace()            (worker sandbox is already destroyed at this point)
        ↓
runner: oracle_mode == "sandbox"  (default)
        ↓
sandboxed_oracle.evaluate_oracle_isolated()
        ├── oracle.static_oracle_checks()          same rules, no execution
        ├── oracle_digests()                       contract / fixture / execution digests
        └── run_hidden_tests_in_sandbox()
                ├── host reads export + hidden files into memory (bounded upload)
                ├── provider.create(SandboxSpec(run_id="…-oracle", network="none",
                │                               persistent=False, secret_references={}))
                ├── provider.write_file() per file → /workspace/oracle/…
                ├── provider.execute("python3 -m pytest -q --tb=short",
                │                    cwd=/workspace/oracle,
                │                    environment={PYTHONPATH, PYTHONDONTWRITEBYTECODE})
                ├── provider.destroy()                 always, in a finally-equivalent path
                └── classify pytest exit → HiddenTestRun(kind, timings, cleanup_failure)
        ↓
OracleOutcome(passed, checks, details, failure_reasons,
              execution_mode="sandbox", failure_kind, environment_failure,
              cleanup_failure, digests, timings_ms)
```

`oracle_mode = "host"` still exists and still calls the old function — it is the only way to
reproduce pre-R1-2H numbers, it is off by default, and unknown values fall back to `sandbox`
(never to host).

## 3. Sandbox permissions and isolation boundary

| Property | Value | Verified by |
|---|---|---|
| Network | `network="none"` | fake-provider test asserts the spec field; real run created a sandbox successfully with that spec |
| Persistence | `persistent=False` | same test |
| Run identity | `run_id="<run>-oracle"`, `task_id="oracle"`, `labels={"atlas_oracle":"1"}` | test asserts a distinct id from the worker sandbox |
| Secrets | `secret_references={}`; command env limited to `PYTHONPATH`/`PYTHONDONTWRITEBYTECODE`; no `os.environ` passthrough | test sets `DEEPSEEK_API_KEY` and asserts it never appears in the spec or the command |
| Host directories | none mounted; upload is a byte copy of the exported tree | code path only calls `write_file` |
| Worker sandbox | already destroyed before the oracle runs | runner ordering (`manager.close` precedes the oracle call) |
| Time limits | `SandboxCommand.timeout_seconds` (default 180 s) | timeout test |
| CPU/memory | inherited from the deployment sandbox image/config; not overridden here | see §13 limitation |
| Cleanup | `provider.destroy()` after every outcome, including failures | tests cover exception, refusal and success |
| Cleanup failure | `OracleOutcome.cleanup_failure` + `metrics.extra["oracle"]["cleanup_failure"]` | test `test_destroy_failure_is_reported_and_never_silent` |

Upload bounds: ≤600 files, ≤2 MB per file. Exceeding either produces
`SANDBOX_PROVISIONING_FAILURE` — an environment verdict, not a functional one.

## 4. Hidden-test confidentiality

* Hidden files are read from `task.hidden_files` into memory on the Host and uploaded **only** into
  the throwaway sandbox. No file is written into the exported workspace: a test asserts the export
  tree is byte-identical and that `workspace/tests/` does not exist afterwards.
* The worker can no longer reach the tests through any channel that existed before: not the
  workspace, not the trajectory, not the recovery context, not the shared volume (there is no shared
  volume), not verification feedback (the verifier never reads `hidden_files`; R1-2A's isolation test
  still passes).
* `oracle.log` (pytest stdout/stderr) and `oracle_sandbox.json` (kind, returncode, sandbox id,
  timings, digests) are written to the run directory. They contain test *names* and failure text, as
  before; they contain no hidden source and no secret, and run directories stay untracked.
* Digests are additive and do not change `fixture_digest`:

```text
oracle_contract_digest   hidden files + required artifacts + allowed/forbidden paths + expectations
oracle_fixture_digest    == task.fixture_digest (unchanged algorithm, pinned by a test)
oracle_execution_digest  contract + workspace snapshot + execution mode + image
oracle_workspace_digest  the exported tree the verdict was computed on
```

## 5. Scoring compatibility

`OracleOutcome` keeps `passed`, `checks`, `details`, `failure_reasons` with the same meaning; the new
fields have defaults (`execution_mode="host"`, `environment_failure=False`, `digests={}`,
`timings_ms={}`), so old call sites and old records stay valid. `static_oracle_checks` is now shared
by both modes, so every non-execution rule (scope, forbidden paths, required artifacts, tool
expectations, worker contract) is literally the same code.

Failure classification:

| pytest result | `failure_kind` | `environment_failure` | Run class |
|---|---|---|---|
| exit 0 | `HIDDEN_TESTS_PASSED` | false | unchanged |
| exit 1 / assertion failure | `HIDDEN_TESTS_FAILED` | false | `ORACLE_FAILURE` |
| collection / import error | `TEST_COLLECTION_FAILED` | false | `ORACLE_FAILURE` |
| exit 5 (no tests) | `NO_TESTS_COLLECTED` | true | `ENVIRONMENT_FAILURE` |
| provider timeout | `ORACLE_TIMEOUT` | true | `ENVIRONMENT_FAILURE` |
| create/upload/execute failure | `SANDBOX_PROVISIONING_FAILURE` / `SANDBOX_EXECUTION_FAILURE` | true | `ENVIRONMENT_FAILURE` |

Functional reasons keep the historical wording (`hidden tests failed with exit code N`) so a failure
string from a new run is comparable with an old one.

## 6. Historical workspace comparison

Method: read-only re-scoring of exported workspaces with the isolated oracle, feeding the saved
`tool_calls` / `worker_results` so the static rules see the same evidence. Nothing was re-executed on
the Host, no historical directory was modified (oracle evidence is written to
`experiments/results/stage-r1-2h/<batch>/runs/<run>`).

| Batch | Runs | Agreement | Notes |
|---|---|---|---|
| final R1-1 round (P2+P3) | 8 | 8/8 MATCH | 4 hidden-test passes, 4 failures |
| R1-1 + R0.5 mixed | 16 | 16/16 MATCH | pass and fail, both worker variants |
| R0.5 P1 analysis | 4 | 4/4 MATCH | includes one failing report |
| **total** | **28** | **28/28 MATCH** | `TEST_ENVIRONMENT_DIFFERENCE`: 0, `DEPENDENCY_DIFFERENCE`: 0, `ACTUAL_SCORING_DIFFERENCE`: 0 |

Two earlier batches are **discarded and recorded** because the harness rebuilt the wrong task
variant (`-a0` coding instead of `-a1` multi worker), which made the worker-contract checks vanish and
produced two spurious `ACTUAL_SCORING_DIFFERENCE` results. The variant is now resolved from the run's
own `task.sample_id`; both discarded batches were re-run from scratch and appear above as MATCH.
This is an internal analysis defect, not a scoring change.

## 7. Environment-error classification

The pre-R1-2H path had three unrecoverable behaviours: a timeout crashed the run, a missing pytest
became an `ORACLE_FAILURE`, and a dead sandbox was indistinguishable from a wrong deliverable. All
three now surface as `ENVIRONMENT_FAILURE` with a specific `failure_kind`, and the deliverable is not
blamed. The unit matrix covers creation failure, refused creation, timeout, no-tests-collected,
command failure and collection error (the last one deliberately stays functional, because an import
error is the agent's code).

## 8. Sandbox lifecycle

`create → upload → execute → destroy`, with `destroy` invoked on every path: success, assertion
failure, collection error, timeout, provider exception, upload failure. `cleanup_failure` is
populated when the provider raises or refuses; the verdict is *not* rewritten by a cleanup defect,
and the defect is duplicated into `metrics.extra["oracle"]["cleanup_failure"]` so it cannot be
silently dropped. No sandbox is reused, pooled or kept alive across runs.

## 9. Cost

Measured on the real OpenSandbox provider (`localhost:8080`, image `atlas-uat-python:3.12`):

| Phase | Mean | Max | Share |
|---|---|---|---|
| creation | 3 120 – 3 399 ms | 5 841 ms | ~52 % |
| upload (export + hidden files) | 376 – 415 ms | 451 ms | ~6 % |
| execution (pytest) | 1 419 – 1 633 ms | 2 252 ms | ~25 % |
| cleanup (destroy) | 990 – 992 ms | 1 092 ms | ~17 % |
| **total** | **5 950 – 6 400 ms** | **9 429 ms** | 100 % |

Live evaluation runs: 5 682 – 5 915 ms per oracle. No model tokens are involved, so the added cost is
wall time and one container per run, not inference spend. Sandbox pooling is explicitly out of scope
for this stage; the obvious later optimisation is to reuse one oracle container per *round* with the
workspace reset, which is deferred because it weakens the "fresh sandbox per verdict" property.

## 10. Regression

| Check | Result |
|---|---|
| `pytest -q` | exit 0, 0 failed (full suite, incl. 26 new R1-2H tests) |
| `ruff check .` | All checks passed |
| `ruff format --check .` | 391 files already formatted |
| `npx pyright` | 0 errors, 0 warnings |
| R1-1 trace suites | unchanged and green |
| R1-2A verification suites | unchanged and green; verifier has no oracle dependency (test asserts it) |
| Runtime success ↔ oracle pass | semantics unchanged: `success = (failure_class == "TASK_SUCCESS") and oracle.passed` |

End-to-end proof: a live R1-2A observe round after the change recorded
`oracle.execution_mode="sandbox"`, `failure_kind="HIDDEN_TESTS_PASSED"`, `cleanup_failure=null`, and
`oracle.log` + `oracle_sandbox.json` in each run, with `failure_class` still driven by budget and
oracle only. In the same runs the observation arm still wrote `verification.json` /
`verification_summary.json`, proving the two layers remain independent.

## 11. CI status

**Unknown.** The branch is pushed; no pull request was created from this environment (`gh` is not
installed and no GitHub token is available), so no CI run was observed. Nothing here claims CI passed.

## 12. Pull request

Branch `fix/r1-2h-oracle-sandbox-isolation`, based on `feat/r1-2-deliverable-verification`
(`e90d53c`). Open it at
<https://github.com/Melon235/AtlasOS/pull/new/fix/r1-2h-oracle-sandbox-isolation> with base
`feat/r1-2-deliverable-verification`.

Stack state, for the record: `analysis/r1-0-failure-attribution` →
`fix/r1-1-worker-ownership-integrity` → `feat/r1-2-deliverable-verification` → this branch. **No PR
exists for R1-1 or R1-2 either** — that blocker was reported in the previous two stage reports and
is unchanged.

## 13. Known limitations

1. **CPU/memory are not pinned by this stage.** The oracle sandbox uses the deployment image and the
   provider's default quotas; `SandboxSpec` has no CPU/memory field in the AnyCode 0.9 contract, and
   inventing one was out of scope. Time is bounded (180 s), resources are not.
2. **One container per verdict.** ~3 s of the ~6 s is creation; pooling is deferred.
3. **Symlink/`..` upload guard is path-shape based.** Exported files are copied as bytes; a symlink in
   the export is skipped, but the upload itself is not a security boundary — the sandbox is.
4. **`oracle_mode="host"` still exists.** It is the only escape hatch for reproducing pre-R1-2H data
   and it re-introduces the original risk; it is never selected implicitly.
5. **Cleanup defects do not fail the run.** They are recorded in two places, but the verdict is kept,
   because a failed `destroy` says nothing about the deliverable. A later stage may decide to mark
   such runs as `CLEANUP_FAILURE` for reporting.
6. **Comparison is limited to 28 workspaces** with preserved exports; runs whose workspace export
   failed were not re-scored (`UNAVAILABLE`).

---

## Verdicts

| Dimension | Verdict | Basis |
|---|---|---|
| Host code execution removed | **PASS** | `subprocess` path is only reachable via explicit `oracle_mode="host"`; default and unknown values use the sandbox; a test fails if the Host spawns a process |
| Hidden-test confidentiality | **PASS** | hidden files exist only in memory + throwaway sandbox; export tree proven unchanged; verifier never reads them |
| Oracle scoring compatibility | **PASS** | 28/28 agreement; shared static checks; identical failure wording |
| Environment vs model failure separation | **PASS** | six-state classification, `ENVIRONMENT_FAILURE` on every infrastructure defect |
| Sandbox lifecycle hygiene | **PASS** | destroy on every path, explicit `cleanup_failure`, no reuse |
| Sandbox resource isolation | **PARTIAL** | network none, no secrets, fresh container; CPU/memory quotas not pinned by this stage |
| Cost / latency transparency | **PASS** | per-phase measurements from real runs |
| Regression safety | **PASS** | full suite green, lint/format/types clean, R1-1 and R1-2A unaffected |
| PR / CI readiness | **PARTIAL** | branch pushed and scoped; PR not created, CI unknown |

## Five questions

1. **Does the Host still execute agent-generated code?** No, not on any default path. The isolated
   oracle uploads the export into a disposable sandbox and runs pytest there; the only Host-side
   contact with the deliverable is byte-reading it for the upload and AST/JSON inspection in the
   verifier. `oracle_mode="host"` remains as an explicit, documented legacy switch.
2. **Are the hidden tests still invisible to the worker?** Yes. They are never written to the
   exported workspace, never enter the sandbox the worker used (it is destroyed first), never appear
   in prompts, tool results, trajectory or recovery context, and the verifier does not read them.
3. **Is the score identical before and after?** Yes for all 28 re-scorable historical workspaces
   (100 % agreement, both directions). Two earlier batches disagreed only because the comparison
   harness rebuilt the wrong task variant; those results are discarded and documented.
4. **Can a sandbox failure be distinguished from a model failure?** Yes: timeout, provisioning,
   execution and "no tests collected" are `ENVIRONMENT_FAILURE` with their own `failure_kind`, while
   assertion and collection errors from the deliverable stay `ORACLE_FAILURE`.
5. **What does the isolation cost?** ~6 s and one container per evaluation (creation 3.1–3.4 s,
   upload 0.4 s, execution 1.4–1.6 s, cleanup 1.0 s), no model tokens, no change to the number of
   model turns.
