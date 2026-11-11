# Stage G3-1 — Tool Error / Side Effect Semantics Repair

Round: `g31-20261111T034129Z`
Issue under repair: **G3-I01 — Completed Tool Error Misclassified as Unknown Side Effect**
(from `docs/stage-g3-failure-audit.md`)

---

## 1. Scope

In scope:

* audit every consumer of `retry_safe` / `is_error` / `side_effect_unknown` in AnyCode 0.9.0 and
  AtlasOS;
* one minimal mapping fix in the AtlasOS-owned sandbox bash tool adapter;
* an experiment switch `--tool-error-mode legacy|corrected` wired through the existing Stage E
  runner settings;
* safety tests (fake sandbox + agent loop) and a full regression;
* 8 real runs (2 tasks × 2 modes × 2 repetitions) and this report.

Explicitly out of scope, and untouched: AnyCode source, M3 Action Safety Gate policy and its
`_failure` default (G3-I02), M4 generic side-effect recovery, scheduler / TaskQueue / checkpoint,
G1 handoff, G2a budget guard, G2b-1 HistoryStore and compaction, Stage E1 tasks / fixtures /
oracles, worker profiles and prompts, agents/supervisors/auto-retry, token/turn/tool budgets,
CLI/Web features. No Git write operations were performed.

---

## 2. `retry_safe` / `is_error` consumption audit

Every occurrence of `retry_safe` in `anycode==0.9.0` and in `src/atlasos` was read. There are
exactly six consumers:

| # | Consumer | File:line | What it does with the value | Re-executes a tool? |
|---|---|---|---|---|
| 1 | `_normalize_result` | `.venv/.../anycode/tools/executor.py:151-155` | fills `None` with `not (side_effecting and is_error)` | no |
| 2a | tool-loop stop | `.venv/.../anycode/core/runner.py:639` | `next(record for record if not record.retry_safe)` → `stop_side_effect_unknown`, terminal | no |
| 2b | record projection | `.venv/.../anycode/core/runner.py:964` | `ToolCallRecord(retry_safe=result.retry_safe is not False)` — only an explicit `False` stops | no |
| 3 | idempotency store | `.venv/.../anycode/tools/idempotency.py:100,206` | `retry_safe is True` ⇒ record becomes `prunable=1` | **not by itself** — only after `prune_completed` **and** a later claim with the same key |
| 4 | M4 ledger classifier | `src/atlasos/ledger/bridge.py:234-241` | `retry_safe` is read **only** for pre-execution failures; any other side-effecting error is hard-coded `UNCERTAIN/EXECUTED_ERROR/False` | no |
| 5 | M6 settlement label | `src/atlasos/approvals/resume.py:779-781` | classifies a settlement outcome (`UNCERTAIN` vs `FAILED`) | no |
| 6 | evidence | `src/atlasos/ledger/recovery.py:131`, `trajectory/{models,recorder,reconcile}.py` | recorded verbatim | no |

Answers to the audit questions:

1. **What does `retry_safe` mean in AnyCode?** "The outcome of this tool call is known, so the run
   may continue." Its only behavioural effect is the terminal `side_effect_unknown` stop in the
   tool loop. Its *name* also implies replayability, but no runtime path acts on that implication
   except the idempotency-store `prunable` flag.
2. **Does `retry_safe=False` only stop the AgentRunner?** Yes — `runner.py:639` breaks the turn loop
   with `StopReason(code="side_effect_unknown", recoverable=False)`. Nothing else reads it for
   control flow.
3. **Is there an automatic retry path reading `retry_safe=True`?** No.
   * `anycode/providers/resilience.py` retries **LLM adapter calls only** (429/5xx/timeout); it has
     no access to tool results.
   * The only replay path in the tool layer is the idempotency claim
     (`executor.py:107-108`), which returns the **stored** `ToolResult`; it never re-invokes the
     tool, and AtlasOS derives keys as `f"{run_id}:{turn}:{index}"` (`runner.py:624,928`) so a new
     turn always gets a fresh key. The replay branch is reachable only when resuming the *same*
     suspended response.
4. **Does a resume / recovery path depend on the field?** `RecoveryClassifier` (`ledger/recovery.py`)
   does not read `retry_safe`; it depends on the ledger `state` and an idempotency inspection. A
   tool error leaves the ledger `UNCERTAIN` (see #4 above), so recovery stays conservative
   regardless of the runner's value.
5. **Does M4 use it to decide replayability?** Only for *pre-execution* failures
   (`_pre_execution_failure` markers: invalid input, unregistered tool, missing idempotency key,
   store unavailable, `action_denied`, `action_hard_denied`). A bash exit code is never a marker, so
   a completed bash error is always recorded `UNCERTAIN/EXECUTED_ERROR/retry_safe=False` — verified
   live: 3 such records, `m4_retry_safe_true_records = 0`.
6. **How does the sandbox bash distinguish normal completion, non-zero exit and unknown state?**
   `SandboxCommandResult.ok` is the provider's explicit "the command ran and its termination was
   observed" flag (`anycode/sandbox/models.py:113-119`). `AtlasOS.OpenSandboxProvider.execute`
   returns `ok=True` **with** the observed `exit_code`, and `ok=False` for transport/SDK failures
   (`src/atlasos/sandbox/opensandbox_provider.py:393-455`). `SandboxLostError` is raised, never
   flattened. `ok=True` + `exit_code != 0` is therefore a **known** outcome.
7. **Does the adapter layer already carry a completion marker?** Yes — `ok` is it. That is why no
   new state enum was needed: the fix reads `ok` and the observed `exit_code`, never the text.

### Safety gate (audit precondition)

Because no consumer re-executes a tool on `retry_safe=True`, the minimal field fix is admissible.
One latent caveat is recorded in §9: `retry_safe=True` also flags the AnyCode idempotency record as
`prunable`, and `prune_completed` currently has **no caller** in either repository.

---

## 3. Completed error vs unknown execution state

Three different things, deliberately kept apart:

| Concept | Meaning | Where it lives |
|---|---|---|
| `execution_completed` | the command terminated and an exit status was observed | `SandboxCommandResult.ok is True` |
| `operation_succeeded` | the command's business result was successful | `exit_code == 0` / `ToolResult.is_error` |
| `safe_to_replay` | the same operation may be re-issued without duplication risk | **never asserted by this stage**; M4 keeps `retry_safe=False` for every side-effecting error |

Mapping implemented in `src/atlasos/sandbox/tools.py`:

```text
SandboxCommandResult.ok is False            → unknown outcome → retry_safe=False (both modes)
ValueError (path validation, pre-execution)  → retry_safe=False (existing policy, unchanged)
SandboxLostError / SandboxOwnershipError    → retry_safe=False (both modes)
ok is True, exit_code == 0                   → is_error=False
ok is True, exit_code != 0                   → is_error=True, retry_safe=True (corrected only)
```

Provider-side timeout is `ok=True` with an observed termination (OpenSandbox terminates the command
server-side), so it is a *known* error; only an *unobserved* outcome stops the worker.

---

## 4. Minimal implementation and changed files

`src/atlasos/sandbox/tools.py`

* new `TOOL_ERROR_MODE_LEGACY|CORRECTED` constants and a `tool_error_mode` constructor argument
  (default `legacy`, so the pre-G3-1 behaviour stays reachable);
* `bash` now states the retry-safety of the observed-outcome branches explicitly and routes them
  through `_completed_error_retry_safe`:
  * `legacy` → `None` (AnyCode's inference is byte-identical to the pre-G3-1 behaviour);
  * `corrected` → `True` for an observed completion, `False` for `ok is False`;
* the transport-failure, path-validation and lost-sandbox branches state `retry_safe=False`
  explicitly (post-normalization value is identical to `None` in legacy mode);
* stdout/stderr/exit code/`is_error` are unchanged: nothing is masked, no exception is swallowed,
  no permission is changed, no text-based guessing is added, and the mapping is a pure function —
  it cannot re-run the command.

Experiment wiring (Stage E1 system, no second benchmark framework):

* `experiments/evaluation/runner.py` — `RunnerSettings.tool_error_mode`, part of `settings.digest()`,
  recorded in `environment`, passed to `SandboxWorkspaceTools`;
* `experiments/evaluation/stage_e_cli.py` — `--tool-error-mode legacy|corrected`, echoed in output;
* `experiments/evaluation/stage_e_rounds.py` — new `g3-1` suite (`t0`/`t1`, alternating order,
  handoff/budget/compaction pinned).

No tool schema, TaskSpec, prompt or execution parameter differs between the two modes.

---

## 5. Safety tests

`tests/unit/test_sandbox_tool_error_semantics.py` (fake provider, scripted `_execute`, execution
count asserted):

| Case | Assertion | Result |
|---|---|---|
| 1 `echo hello`, exit 0 | `is_error=False`, AnyCode-normalized `retry_safe=True`, one execution | PASS |
| 2 `ls /nonexistent`, exit 2 + stderr | `is_error=True`, `retry_safe=True` (corrected), message keeps `No such file or directory` and `(exit code 2)`, one execution | PASS |
| 3 Python traceback, exit 1 | error preserved with the traceback, one execution | PASS |
| 4 side effect then failure | mapping is pure: one execution, no cached replay consulted | PASS |
| 5 transport failure / provider timeout / `SandboxLostError` | `retry_safe=False` in **both** modes, `is_error=True` | PASS |
| 6 corrected value survives `_normalize_result` | AnyCode does not overwrite an explicit value | PASS |
| 7 M4 ledger regression | `_classify_result` still returns `UNCERTAIN/EXECUTED_ERROR/False` | PASS |
| mode validation | unknown mode rejected | PASS |

`tests/integration/test_stage_g3_1_agent_tool_errors.py` (real `ToolExecutor`, real M3 gate, real M4
bridge, real `SandboxWorkspaceTools`, real `AtlasAgentRunner` M6 seam, real G1 handoff reader, real
G2a budget hook; only the model and the sandbox provider are faked):

| Test | Key assertions | Result |
|---|---|---|
| `test_completed_error_keeps_worker_alive` | 4 model turns; Turn 2/3 inputs contain the previous error text and exit code; `stop_reason == success`; bash executed exactly twice for two model commands; the append-then-fail file contains exactly one line; budget admitted 4/4 requests with 0 rejections; handoff reader still registered in dual mode; M6 snapshot still holds the error text; repair landed in the sandbox, host repo untouched | PASS |
| `test_legacy_mode_still_stops_on_the_same_command` | control: same command under `legacy` ⇒ 1 model turn, `stop_reason == side_effect_unknown`, `success is False` | PASS |
| `test_parallel_batch_mixes_completed_error_and_success` | one failing bash + one successful `file_read` in the same batch; both results correlated by `tool_use_id`; `stop_reason == success`; bash executed once | PASS |
| `test_unknown_outcome_still_stops_the_worker` | `ok=False` transport failure ⇒ `side_effect_unknown`, `sandbox execution failed` message, no Turn 2 | PASS |

The `legacy` control test is the regression proof: the same agent script reproduces G3-I01 exactly.

---

## 6. Full regression

```text
.\.venv\Scripts\python.exe -m pytest      → 533 passed, 1 deselected in 197.28s
ruff check .                              → All checks passed!
ruff format --check .                     → 319 files already formatted
pyright                                   → 0 errors, 0 warnings, 0 informations
```

G1 handoff, G2a budget, G2b-1 compaction, M3/M4/M6 and M1/M5/M7 suites are part of the 533. No test
was deleted, relaxed or skipped. Two pre-existing type errors in the new G3-1 test files were fixed
with `cast(Any, ...)` on the stub objects; no assertion changed.

Note: `experiments/results/stage-g3-1/` was added to `.gitignore` (same convention as
`stage-g1` / `stage-g2a` / `stage-g2b1`); otherwise `ruff check .` scans model-generated workspace
copies inside run artifacts.

---

## 7. The 8 real runs

Configuration (identical in both arms except the switch):

| Item | Value |
|---|---|
| Worker mode | A1 multi-worker |
| Handoff | G1 dual |
| Budget | G2a guarded |
| Compaction | off |
| Token budget | 120,000 |
| Turn limit | 14 |
| Tool-call limit | 60 |
| Model / temperature | Stage E1 defaults (`deepseek-chat`, 0.0) |
| Sandbox | OpenSandbox (`opensandbox`, network none) |
| Fixture / Oracle | reused unchanged |
| Tasks | `e1-01-config`, `e1-12-conflict` |
| Runs | 2 tasks × 2 modes × 2 repetitions = 8, alternating `t0/t1` and `t1/t0` |

Raw data: `experiments/results/stage-g3-1/g31-20261111T034129Z/`
(`manifest.json`, per-run `result.json` / `tool_conversations.json` / `trajectory/`),
`analysis/` = `runs.csv`, `workers.csv`, `mechanism.csv`, `summary.json`.

| Task | Rep | Mode | Run ID | Oracle | Harness class | Stop reasons (worker) |
|---|---|---|---|---|---|---|
| e1-01-config | 0 | legacy | `eval-0215f6625f52` | PASS | BUDGET_EXCEEDED | merge/validate `budget_exceeded`, loader `success` |
| e1-01-config | 0 | corrected | `eval-282e519207cb` | PASS | BUDGET_EXCEEDED | loader `budget_exceeded`, a/validate `success` |
| e1-01-config | 1 | legacy | `eval-5740ff161a1e` | **FAIL** | ORACLE_FAILURE | loader **`side_effect_unknown`** (traceback, exit 1), merge/validate `success` |
| e1-01-config | 1 | corrected | `eval-a8b14c179a11` | PASS | BUDGET_EXCEEDED | loader `budget_exceeded`, merge `success` |
| e1-12-conflict | 0 | legacy | `eval-cd9be0ab403d` | PASS | TASK_SUCCESS | all `success` |
| e1-12-conflict | 0 | corrected | `eval-ee1fa31e5415` | PASS | TASK_SUCCESS | all `success` |
| e1-12-conflict | 1 | legacy | `eval-7c4c4ed8301b` | **FAIL** | ORACLE_FAILURE | worker `b` **`side_effect_unknown`** after 2 calls, a/merge `success` |
| e1-12-conflict | 1 | corrected | `eval-3d21dbdaea9f` | PASS | TASK_SUCCESS | all `success` |

Causal detail of the two legacy failures:

* `eval-5740ff161a1e` loader: `side_effect_unknown` whose stop message is a **Python traceback**
  from a completed `python3` self-test (`exit code 1`). The loader stopped at 13 tool calls; the
  module stayed incomplete and the oracle's hidden tests failed (`hidden_tests_passed = false`).
* `eval-7c4c4ed8301b` worker `b`: `side_effect_unknown` after 2 tool calls; the oracle failed with
  `all_workers_produced_output = false` — the "early stop ⇒ missing deliverable" case.

Detail of the single corrected-mode bash error (`eval-282e519207cb` loader):

```text
turn N   bash  self-test            → is_error=True, ConfigError … (exit code 1)
turn N+1 model reads the error      → file_edit loader.py: empty document returns {}
turn N+2 bash  self-test again      → success
```

No duplicate execution: the `bash` command appears once in the transcript, the M4 ledger holds one
outcome per call, and no `REPLAYED` completion kind exists in any run.

---

## 8. Oracle / stop-reason analysis

| Metric | Legacy (T0, n=4) | Corrected (T1, n=4) |
|---|---|---|
| Oracle Success | 2/4 | 4/4 |
| `side_effect_unknown` workers | 2 | 0 |
| Premature termination caused by a completed Bash error | 2 | 0 |
| Bash non-zero exits | 2 | 1 |
| Worker success | 8/12 | 10/12 |
| Worker Turns (`metrics.model_turns`) | 84 | 78 |
| Prompt Tokens | 260,245 | 258,866 |
| Completion Tokens | 16,919 | 19,021 |
| Tool Calls | 64 | 53 |
| Cost (USD) | 0.029021 | 0.031242 |
| Budget Exceeded (runs) | 1 | 2 |
| Budget admission rejections | 2 | 2 |
| `max_turns` workers | 0 | 0 |
| Failed oracle checks | `all_workers_produced_output` ×1, `hidden_tests_passed` ×1 | none |

Extra analysis:

1. **Did the repaired workers really continue?** Yes. Corrected had 1 completed bash error; that
   worker continued and repaired (`mechanism.csv`: `continued_after_error=True`,
   `repaired_after_error=True`). Legacy's 2 comparable errors both terminated the worker.
2. **Was the previously fatal error fed back to the model?** Yes — the tool result carrying
   `ConfigError … (exit code 1)` is in the transcript the next model call read. In legacy the same
   text was only used as the terminal stop message.
3. **Did the model actually repair?** Yes in the observed case: `file_edit` on `loader.py`
   (empty-document branch), followed by a successful self-test.
4. **More `max_turns` workers?** No: 0 in both arms.
5. **New Budget Exceeded caused by the extra rounds?** Partly. Corrected 2 vs legacy 1 — the delta
   is `e1-01-config rep1`, where legacy died early and corrected ran to the 120k ceiling (oracle
   still passed). Aggregate prompt tokens did not increase (258,866 vs 260,245); completion tokens
   rose by 2,102 and cost by $0.0022. `e1-01-config` exceeds the budget in 3 of 4 runs, so the
   ceiling — not this fix — is the binding constraint for that task.
6. **Did the oracle improve?** 2/4 → 4/4. Directional only (n=4/arm, single model, two tasks).
7. **Any duplicate side effect?** No. 3 `UNCERTAIN/EXECUTED_ERROR` M4 records, 0 `REPLAYED`, one
   execution per model command, no framework re-execution path keyed on `retry_safe` (§2).
8. **Is the unknown-state safety stop preserved?** Yes: `ok=False` (transport/SDK), provider timeout
   surfaced as an exception, and `SandboxLostError` all keep `retry_safe=False` in both modes
   (unit cases 5 + integration control test), and M4 keeps `UNCERTAIN/False`.

**Mechanism correctness** and **task quality improvement** are judged separately:

* Mechanism: **correct**. 2 → 0 premature terminations, error feedback delivered, no replay, M4 and
  M3 boundaries unchanged.
* Task quality: **directionally improved**, not proven. The oracle gain comes from not killing the
  worker; the same runs then hit the token ceiling, so 2 of 4 corrected runs are still labelled
  `BUDGET_EXCEEDED` even though their oracle passed. Fixing premature termination does not
  guarantee task success.

---

## 9. Known limitations

1. **n=4 per arm.** Two tasks, one model, one temperature. Directional evidence only; no statistical
   claim.
2. **Latent idempotency-prune pathway.** `retry_safe=True` also sets AnyCode's idempotency record to
   `prunable=1`. `prune_completed` has no caller today, so no re-execution is possible; if a future
   stage enables pruning, a *resumed same-key* claim could re-execute. This must be re-audited
   before any prune scheduler is introduced.
3. **Provider-side timeout** is classified as a known error (observed termination). Only unobserved
   outcomes stop the worker. This is a deliberate boundary, documented here rather than inferred
   from text.
4. **G3-I02 untouched by instruction.** `actions/gate.py::_failure` still defaults
   `retry_safe=False`, so a pre-execution M3 denial still terminates the worker as
   `side_effect_unknown`. Not observed in these 8 runs (0 denial stops); needs its own stage with a
   real case.
5. **M4 intentionally stays conservative**: a completed bash error is still `UNCERTAIN`, so an
   automatic recovery of such an action is not allowed. The runtime continuing is now independent of
   M4's bookkeeping — that separation is the point of the fix, not an oversight.
6. **`e1-01-config` is budget-bound** (3 of 4 runs hit 120k). Turn-limit/budget tuning stays a
   separate workstream; this stage changed no budget parameter.
7. **Artifact-derived counting.** `result.json`'s `tool_calls` projection truncates long outputs, so
   Bash exit-code counting reads the durable transcripts. A worker killed by `side_effect_unknown`
   never persists the offending tool result; its non-zero exit is recovered from the persisted stop
   message.

---

## 10. Final verdict

**PASS (mechanism) / directional-only (task quality).**

* **Is G3-I01 actually fixed?** Yes, for the completed-command case: an observed non-zero exit is a
  known error that no longer terminates the worker, proven by unit tests, an agent-loop integration
  test, and a live A/B where the same failure mode killed 2 legacy workers and 0 corrected workers.
* **Is there an automatic replay risk?** No new one was introduced. No consumer re-executes a tool on
  `retry_safe`; the only replay path returns a stored result under a `(run, turn, ordinal)` key,
  0 `REPLAYED` records were produced, and the mapping itself is pure. The single latent caveat is
  limitation #2.
* **Is the M4 safety boundary intact?** Yes — completed bash errors remain
  `UNCERTAIN/EXECUTED_ERROR/retry_safe=False` (3 live records, test `test_m4_ledger_still_…`).
* **Did real task results improve?** Oracle 2/4 → 4/4, with 2 → 0 `side_effect_unknown` workers and
  no cost regression in aggregate; 2 corrected runs remain `BUDGET_EXCEEDED` for budget reasons.
* **Should `corrected` become the default?** Recommended as the runtime default, because the defect
  is confirmed and the corrected classification is provably non-replayable — but with the evidence
  level stated: 8 runs, n=4/arm, one model, two tasks. Legacy must remain selectable
  (`--tool-error-mode legacy`) for ablation, and the recommendation should be re-confirmed after the
  budget/turn workstream lands for `e1-01-config`.

---

## Changed files

Modified:

* `src/atlasos/sandbox/tools.py` — completed-error mapping + `tool_error_mode`
* `experiments/evaluation/runner.py` — `RunnerSettings.tool_error_mode` → sandbox tools
* `experiments/evaluation/stage_e_cli.py` — `--tool-error-mode legacy|corrected`
* `experiments/evaluation/stage_e_rounds.py` — `g3-1` suite (`t0`/`t1`)
* `.gitignore` — ignore `experiments/results/stage-g3-1/`

Added:

* `tests/unit/test_sandbox_tool_error_semantics.py`
* `tests/integration/test_stage_g3_1_agent_tool_errors.py`
* `experiments/evaluation/stage_g3_1_analysis.py`
* `experiments/results/stage-g3-1/g31-20261111T034129Z/` (raw runs + `analysis/`)
* `docs/stage-g3-1-report.md`

Read-only `git diff --stat` (working tree also carries uncommitted G1/G2a/G2b-1 work; no Git write
operation was performed in this stage):

```text
 .gitignore                                 |   4 +
 experiments/evaluation/runner.py           |  88 +++-
 experiments/evaluation/stage_e_cli.py      |  83 ++++
 experiments/evaluation/stage_e_rounds.py   | 420 +++++++++++++++++++-
 src/atlasos/integrations/anycode_engine.py |  93 +++-
 src/atlasos/sandbox/tools.py               |  49 +-
 src/atlasos/tools/conversation.py          |  33 ++
 tests/integration/test_anycode_dag.py      | 705 ++++++++++++++++++++++++++++-
 8 files changed, 1463 insertions(+), 12 deletions(-)
```
