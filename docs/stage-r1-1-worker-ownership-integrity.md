# Stage R1-1 — Worker Ownership Guard & Execution Trace Integrity

**Project:** AtlasRuntime (repository still `Melon235/AtlasOS`, package `atlasos`).
**Scope:** two opt-in runtime mechanisms and their evidence. No scheduler, budget, router, prompt,
fixture or oracle change.

R1-0 attributed the multi-worker failures to turn count, a shared budget, and two concrete
cross-worker rollbacks (`git checkout -- ledger/money.py`, `git checkout -- consumers/report.py`).
R1-1 asks whether a Host-owned write boundary can stop the second class without changing task
semantics, and whether a worker that is stopped mid-flight still leaves complete evidence.

**Answer in one paragraph.** Ownership is enforceable for file tools and for the two observed Git
rollbacks, and the guard never fires on a legitimate write in the tests we wrote. Against a live
coding model it is only *partially* compatible: bash commands whose write set cannot be proven are
refused, and coding agents constantly run interpreters (`python3 -m pytest`, `python3 -c`). After
two narrow classifier repairs (`cd` inheritance, `python3 --version`) the enforced arm cut its
denials from 83 to 19 over four runs and completed one task end to end, but it still never beats the
baseline (oracle pass 1/16 vs 7/16 across four rounds). Trace integrity is complete: the private
payload corpus gives 100 % input coverage, and the trajectory now recovers 111 of 355 actions that
`result.json` dropped in the baseline arm. The next stage should verify deliverables (R1-2) and
reduce turn cost (R1-3); stronger isolation is only justified if a later stage shows textual
classification is not enough.

---

## 1. Source audit — the real call chain

Verified against the current working tree, not against earlier reports.

```text
ScopedWorkerFactory.create
  └── _derive_context            src/atlasos/workers/factory.py
        ExecutionContext.attributes["atlas_worker"] + flat mirrors
        (worker_id, profile_id, task_id, parent_agent, operating_scope)
WorkerInstance.tool_context      src/atlasos/workers/models.py
  └── ToolUseContext(agent=AgentInfo(name=worker_name), execution_context=...)
AtlasContextEngine.build_agent   src/atlasos/integrations/anycode_engine.py
  └── AtlasToolAgent(config, registry, executor=runtime.executor, tool_runtime=runtime)
AtlasDispatchExecutor.execute    src/atlasos/tools/dispatch.py
  ├── logical != dispatch_action → inner.execute(logical, ...)
  └── logical == dispatch_action → runtime.dispatch(tool_id, args, ctx)
        └── AtlasToolRuntime.dispatch → _inner_executor.execute(tool_id, ...)
ActionSafetyGate                 src/atlasos/actions/gate.py
  ├── check_tool_access / model_validate / catalog.resolve / _hard_precheck
  ├── external_policy.enforce   (the only Atlas-owned extension seam)
  ├── durable_bridge (M4) → anycode ToolExecutor (idempotency + tool body)
  └── record_action_decision (M7)
SandboxWorkspaceTools            src/atlasos/sandbox/tools.py → OpenSandbox provider
```

Answers to the twelve audit questions:

1. **Who builds worker identity?** The Host. `ScopedWorkerFactory._derive_context` builds
   `ExecutionContext` and its `atlas_worker` metadata; the model never sees a writable identity
   channel. `ExecutionContext.delegation` is cleared for workers.
2. **Can task_id / worker_id be changed by the model?** No. They exist only in the frozen
   `ExecutionContext` handed to the tool call; tool input schemas have no identity field, and the
   gate reads `context.execution_context` (it denies with `trusted_context_required` when absent).
3. **Which tools go through M3?** All of them. `ActionSafetyGate` wraps the executor inside
   `AtlasToolRuntime`, so every native call and every dispatched call passes it.
4. **Does `dispatch_action` share the gate?** Yes, twice: the outer control call
   (`AtlasDispatchExecutor`) and the resolved target call (`AtlasToolRuntime.dispatch` →
   `_inner_executor`, which is the gate).
5. **Does `execute_batch` authorize each call?** Yes: `ActionSafetyGate.execute_batch` gathers
   `self.execute(...)` per call, so each call runs the full evaluate → decide → audit path.
6. **Does `execute_revalidated` keep the same constraints?** It refuses a `DENY`/failed evaluation
   and re-checks the tool binding digest before executing. It does not re-run the policy; the
   resume path (`src/atlasos/approvals/resume.py::_revalidate_invocation`) re-evaluates every
   invocation with `phase="resume_revalidation"` through the same gate, so an external policy is
   consulted again. A stale approval cannot skip it.
7. **Human approval with modified arguments?** `ActionSafetyGate.execute` loops back to
   `evaluate` after a `modified_input`, so the modified arguments are authorized from scratch.
8. **Sandbox binding and worker identity?** One `SandboxWorkspaceTools` per run
   (`workspace_task_id = f"{run_id}-workspace"`) serves every worker; the binding has no per-worker
   field. Ownership therefore has to be enforced above the sandbox, which is what R1-1 does.
9. **When is a tool call written to M7?** Proposal: `AtlasAgentRunner._execute_tool_blocks`
   (`src/atlasos/tools/conversation.py`). Decision: `ActionSafetyGate._record_trajectory`.
   Observation: `AtlasAgentRunner._run_single_tool_block`. Execution outcome: M4 bridge.
   Every append is synchronous and lock-guarded (`TrajectoryRecorder._append`), one flush per event.
10. **Why does a budget stop lose `WorkerResult.tool_calls`?** `RunBudgetHook.before_turn` raises
    `RunBudgetExhausted` before the provider call (`src/atlasos/budget.py`). AnyCode's
    `AgentRunner.stream` catches the exception, yields an `error` event and returns **without a
    `done` event**, so `AgentRunner.run` falls back to an empty `RunResult(tool_calls=[])`.
    `AtlasToolAgent._apply_budget_outcome` only rewrites `success`/`stop_reason`; the tool calls in
    the generator's local list are gone. `result.json` therefore records nothing for that worker.
11. **Does the recorder guarantee persistence?** It appends + flushes per event under a
    `threading.Lock`, so it is safe for concurrent worker tasks in one loop. It does **not**
    `fsync`, and it swallows write errors into `recording_failures`/`last_error` — evidence loss is
    silent unless the summary reads those fields. R1-1 makes the summary read them.
12. **Where can an external policy attach without touching AnyCode?** Exactly one place:
    `ActionSafetyGate.external_policy` (an `anycode.identity.policy.PolicyEnforcer`), constructed
    in `AtlasToolRuntime.__init__` from `action_external_policy`. Every experiment before R1-1 left
    that parameter `None`; it was the only unused seam in the M3 wiring.

---

## 2. M3 integration decision

Reused `ActionSafetyGate.external_policy` with `anycode`'s `PolicyEnforcer` wrapper and a custom
`ExternalPolicyAdapter`. No new executor, no second gate, no change to AnyCode.

* The adapter can only tighten: `_evaluate` calls the external policy only when the local decision
  is not already `DENY`, and a denial is merged as `DENY` with the adapter's reason codes.
* `fail_closed=True` on the wrapper, so a crash inside the adapter denies rather than allows.
* Denials are audited (`ActionSafetyGate._audit`) and recorded
  (`action_decision` with `external_policy_outcome="deny"`).
* New gate flag `external_denial_retry_safe` (default `False`) marks *external-policy* denials
  `retry_safe=True` **only** when configured. The tool body provably did not run, so the worker may
  continue instead of being stopped with `side_effect_unknown`. Default `False` keeps every
  pre-R1-1 caller byte-identical.
* Resume and approval paths re-run `evaluate`, so the policy is consulted again (§1 items 6–7).

---

## 3. Ownership Manifest model

`src/atlasos/ownership/models.py`, `manifest.py`.

```python
WorkspaceOwnershipManifest(
    run_id, workspace_root, policy_version,
    worker_ownership: {worker_id: (declared paths)},
    task_by_worker: {worker_id: task_id},
    excluded_by_worker: {worker_id: (read-only paths)},
    global_paths, provenance, digest,
)
```

* Declarations are normalized (`ledger/`, `ledger/**` mean a directory; anything else is one file).
* `digest` is a canonical SHA-256 over the whole table; `is_valid()` re-derives it. A manifest that
  does not match its digest is refused (`OWNERSHIP_INVALID_MANIFEST`).
* Conflicting declarations (two workers, same file) are a **Host error**: the builder raises
  `OwnershipManifestError`. The evaluation runner converts that into
  `EvaluationEnvironmentError`, so it is classified as an environment defect, not a model failure
  (this is exactly what the pre-fix p3 runs recorded before the contract handling was corrected —
  see §12).
* The manifest is built before any worker runs and is never rebuilt from tool input.

## 4. Trusted sources

| Level | Example | Used for write authorization |
|---|---|---|
| `TRUSTED_HOST_DECLARATION` | operator YAML | yes |
| `VERIFIED_TASK_METADATA` | benchmark step prompt, frozen contract, fixture | yes |
| `PLANNER_CLAIM` | router/planner text, worker assignment | **no — builder raises** |

`WorkerAssignment` (model-facing) carries no paths at all, so a planner cannot express a grant.

The evaluation bridge (`experiments/evaluation/stage_r1_1_ownership.py`) derives one declaration per
worker from `extract_declared_paths(step.prompt)` and treats `frozen_contracts` as **excluded**
(read-only), not owned. The first p3 attempt treated a frozen interface as an owned path, which
correctly failed the conflict check; that defect is preserved in §12 rather than hidden.

## 5. Worker identity binding

Every decision reads `PolicyRequest.context.attributes["atlas_worker"]` (falling back to the flat
mirrors). The adapter refuses with `OWNERSHIP_UNKNOWN_WORKER` when the worker is absent, unknown, or
when the runtime `task_id` disagrees with the manifest's task for that worker. There is no lookup by
agent name, no `last_started_worker`, no global current-worker variable.

## 6. File-tool authorization

`file_write` and `file_edit` are checked before the tool body:

| Case | Result |
|---|---|
| worker's own declared file | ALLOW |
| another worker's file | `OWNERSHIP_DENIED_FOREIGN_PATH` |
| nobody's file | `OWNERSHIP_DENIED_UNDECLARED_PATH` |
| explicit exclusion (tests, frozen contract) | `OWNERSHIP_DENIED_UNDECLARED_PATH` |
| `../`, absolute outside root, `C:\`, backslash alias, NUL | `OWNERSHIP_PATH_ESCAPE` |
| `mkdir` container of an owned path | ALLOW (shell only) |

Denials never call the underlying tool (asserted by counting tool-body invocations in the tests).
Reads are untouched: `file_read`, `grep`, `list_files` are not ownership-checked.

## 7. Bash coverage

`src/atlasos/ownership/shell.py` classifies text, never executes it:

* `PROVEN_READ_ONLY` — closed allowlist of read-only programs, Git listing subcommands, `cd`
  (tracked), and single-flag interpreter version probes (`python3 --version`). Redirects promote a
  read-only command to a write.
* `KNOWN_WRITE_TARGETS` — `git checkout --`, `git restore`, `git restore --staged` (index only),
  `git reset` (non-hard), `rm`, `mv`, `cp`, `install`, `ln`, `touch`, `mkdir`, `chmod`, `chown`,
  `tee`, `truncate`, `sed -i`, output redirection. **Every** target is checked; a multi-path command
  cannot pass on its first path.
* `WORKSPACE_WIDE_WRITE` — `git reset --hard/--keep/--merge`, `git clean`, `git stash`,
  `git switch`, `git checkout <ref>` (no `--`), `git rebase/merge/apply/am/cherry-pick`,
  `find -delete`. Refused in enforce.
* `UNCLASSIFIABLE` — interpreters, unknown binaries, command substitution, `~`/`cd -`,
  unparseable quoting. Refused in enforce.

`cd` is tracked across segments, so `cd ledger && rm money.py` authorizes `ledger/money.py`, not
`money.py`; `cd /etc && rm passwd` keeps an absolute target that the path normalizer rejects.

**Coverage: `OWNERSHIP_ENFORCEMENT_COVERAGE = PARTIAL`.** There is no OS-level isolation: a program
that the classifier wrongly judged read-only could still write. R1-1 mitigates this by refusing
everything it cannot prove, which is why coding agents lose commands (§12).

## 8. Confirmed limits and bypasses

1. **No OS isolation.** Shell control is textual. A parser bug is a bypass; the conservative
   allowlist is the only defence.
2. **Symlinks are not resolved.** The sandbox exposes no `lstat`, so `OWNERSHIP_SYMLINK_UNVERIFIED`
   cannot be decided and is not fabricated. A symlink created inside the workspace could redirect a
   later write; the practical path is closed because `ln`, interpreters and unknown binaries are
   refused in enforce.
3. **`python3 -m pytest` is refused.** Test execution is arbitrary code, so it is unclassifiable.
   This is the dominant compatibility loss.
4. **Shared files are refused by default.** Two workers declaring the same path is a Host error;
   there is no automatic merge grant. R1-1 deliberately does not add a merge scheduler.
5. **Scratch writes are refused.** `.verify_tmp/providers`, `.verify_report_tmp.py` and `/tmp/verify`
   were denied in the live runs. Correct by the invariant, inconvenient in practice.

## 9. Concurrency

`test_t9_concurrent_workers_do_not_cross_identities` runs 20 interleaved `file_write` calls from two
contexts through one gate; exactly 10 succeed (each worker's own file) and 10 are denied
(`OWNERSHIP_DENIED_FOREIGN_PATH`). No request is attributed to the other worker, and the evidence
counter records 10 denials. Resume revalidation (`test_t14`) re-runs the policy and refuses the old
allow for a foreign path.

## 10. Resume / approval path

* `execute_revalidated` refuses a `DENY` evaluation and a changed tool binding.
* The resume coordinator re-evaluates with `phase="resume_revalidation"`, so the ownership adapter
  runs again and a changed manifest invalidates the decision.
* Human-modified arguments loop back through `evaluate`, so ownership is re-checked after editing.

## 11. Execution trace integrity

Three additive changes, all behind `execution_integrity_mode = off | record` (default `off`):

1. **Private payload corpus** (`src/atlasos/trajectory/payloads.py`): the exact tool input is
   appended to `<run_dir>/private/tool_payloads.jsonl`, keyed by
   `payload:{call_id}:{input_digest[:12]}`, flushed and fsynced. The transcript carries only
   `input_ref` + `input_state` (`RECORDED` / `UNAVAILABLE`), so a published trajectory still leaks
   no payload. A write failure is reported as `UNAVAILABLE`, never synthesized.
2. **Worker terminal event**: `WorkerLifecycleData.mode` gains `stop`, with `stop_code` and
   `stop_reason`. `AtlasToolAgent._apply_budget_outcome` emits it when the budget guard rejected a
   turn, so "stopped" is distinguishable from "still running". `TrajectoryCorrelation` also gains
   `task_id`, so an event joins a worker without relying on creation order.
3. **Run terminal + summary** (`experiments/evaluation/stage_r1_1_trace.py`): the runner records
   `run_terminal` and writes `metrics.trace` with proposed / authorized / denied / started /
   completed / failed / unknown counts, `input_digest_coverage`, unmatched outcomes, duplicate call
   ids and a `TRACE_COMPLETE` / `TRACE_INCOMPLETE` verdict that also requires
   `recorder.recording_failures == 0`.

`result.json` is no longer treated as the source of truth: `compare_with_result` reports what the
trajectory recovered.

## 12. Live controlled comparison

`experiments/evaluation/stage_r1_1_rounds.py`. Conditions frozen: DeepSeek Chat, temperature 0,
OpenSandbox (`localhost:8080`, image `atlas-uat-python:3.12`), token budget 120 000, max turns 14,
max tool calls 60, handoff dual, budget guarded, tool-error corrected, compaction off, offload off,
worker contract legacy. Arms differ only in `ownership_mode`; both run `execution_integrity_mode=record`.
Matrix: 2 tasks × 2 arms × 2 repetitions = 8 runs per round, repeated four times as the classifier was
repaired. Host: Windows 10, Python 3.12.15.

### Round D — final build (this is the reported matrix)

| Task | Rep | Arm | Oracle | Class | Ownership denials | `result.json` actions | Trajectory actions | Recovered | Input coverage | Trace |
|---|---|---|---|---|---|---|---|---|---|---|
| p2-modules | 0 | O0 | **pass** | BUDGET_EXCEEDED | 0 | 10 | 23 | 13 | 1.00 | COMPLETE |
| p2-modules | 0 | O1 | **pass** | TASK_SUCCESS | 3 | 17 | 17 | 0 | 1.00 | COMPLETE |
| p3-frozen | 0 | O0 | fail | ORACLE_FAILURE | 0 | 22 | 22 | 0 | 1.00 | COMPLETE |
| p3-frozen | 0 | O1 | fail | ORACLE_FAILURE | 10 | 30 | 30 | 0 | 1.00 | COMPLETE |
| p2-modules | 1 | O0 | fail | ORACLE_FAILURE | 0 | 20 | 20 | 0 | 1.00 | COMPLETE |
| p2-modules | 1 | O1 | fail | ORACLE_FAILURE | 2 | 14 | 14 | 0 | 1.00 | COMPLETE |
| p3-frozen | 1 | O0 | **pass** | BUDGET_EXCEEDED | 0 | 11 | 23 | 12 | 1.00 | COMPLETE |
| p3-frozen | 1 | O1 | fail | ORACLE_FAILURE | 4 | 23 | 23 | 0 | 1.00 | COMPLETE |

| Arm | Oracle | Cost (USD) | Prompt tokens | Completion tokens | Model turns | Tool calls | Ownership denials | Actions lost by `result.json` | Latency |
|---|---|---|---|---|---|---|---|---|---|
| O0 (off) | 2/4 | 0.0458 | 396 237 | 29 637 | 86 | 63 | 0 | 25 | 390.7 s |
| O1 (enforce) | 1/4 | 0.0372 | 240 526 | 25 147 | 63 | 84 | 19 | 0 | 221.8 s |

Denial composition (round D): 17 `OWNERSHIP_UNCLASSIFIABLE_SHELL` (e.g.
`python3 -c "import sys; print(sys.version)"`), 1 `OWNERSHIP_PATH_ESCAPE` (`/tmp/verify`),
1 `OWNERSHIP_DENIED_UNDECLARED_PATH` (`.verify_report_tmp.py`). **No foreign-path denial fired in a
live run**, because the live model did not replay the historical rollback. Interception of that
command is proven deterministically (§13, T4), not by this evaluation.

### All rounds (classifier evolution, same frozen conditions)

| Round | Build change under test | O0 oracle | O1 oracle | O1 denials | O0 lost actions |
|---|---|---|---|---|---|
| A `r11-…125849Z` | first working enforce | 2/4 | 0/4 | 83 | 52 |
| B `r11-…133831Z` | deliverable metric fixed | 1/4 | 0/4 | 76 | 0 |
| C `r11-…142557Z` | `cd` inheritance + `/dev/null` sink | 2/4 | 0/4 | 66 | 34 |
| D `r11-…143748Z` | interpreter version probe | 2/4 | **1/4** | 19 | 25 |
| **total** | | **7/16** | **1/16** | **244** | **111** |

Pre-fix round `r11-…125453Z` additionally recorded two `RUNTIME_FAILURE`s
(`OwnershipManifestError: path 'docs/INTERFACE.md' is declared by both …`), which is how the
frozen-contract mistake was found. Total spend for all rounds: about USD 0.29.

An honest reading: O1 never beats O0, and the oracle gap is produced by denied *interpreter*
commands, not by ownership catching anything. With n=4 per round the difference is not significant;
the direction is consistent in all four rounds.

## 13. Deterministic tests

`tests/unit/test_r1_1_ownership_manifest.py`, `…_shell.py`, `…_gate.py`,
`tests/unit/test_r1_1_trace_integrity.py` — 114 tests, scripted/fake only, no live model.

| Case | Where | Result |
|---|---|---|
| T1 own write | gate test | ALLOW, body runs once |
| T2 foreign write | gate test | DENY, body never runs |
| T3 foreign edit | gate test | DENY |
| T4 historical rollback | shell + gate + audit | enforce blocks; audit records only |
| T5 multi-target rollback | gate + shell | denied on the second path |
| T6 workspace-wide | gate + shell | `git reset --hard/clean/stash` denied |
| T7 complex shell | shell + gate | interpreters, substitution denied |
| T8 path bypass | manifest + gate | `..`, absolute, `C:\`, backslash, NUL denied |
| T9 concurrent identity | gate test | 10 allow / 10 deny, no crossing |
| T10 budget stop | trace test | proposal + observation survive |
| T11 tool/recorder failure | trace test | `TRACE_INCOMPLETE` |
| T12 cancellation/uncertain | trace test | `UNCERTAIN`, never COMPLETED |
| T13 resume | gate test | policy re-runs, old allow not reused |
| T14 approval-modified args | gate test | denied evaluation not executable |
| T15 off compatibility | gate test | no enforcer, no evidence |
| T16 audit non-interference | gate test | identical result, findings recorded |

## 14. Full regression

| Check | Result |
|---|---|
| `pytest -q` | exit 0, 806 collected, 0 failed, 1 `s` (live marker deselected by `addopts`) |
| `ruff check .` | All checks passed |
| `ruff format --check .` | 368 files already formatted |
| `npx pyright` | 0 errors, 0 warnings |

The suite was run with `--basetemp` outside the working tree because this host's shell wrapper
intercepts pytest's bulk deletion of its own temp directory; that is a host artifact, not a test
result. M2/M3/M4/M5/M6/M7, G1, G2a, G3-1, G3-4a, R0, R0.5 and R1-0 test modules all pass unchanged;
no assertion was weakened and no test was deleted.

## 15. GitHub

Branch `fix/r1-1-worker-ownership-integrity`, stacked on the PR #3 head. Four commits: ownership
policy, trace integrity, tests, docs. No force push, no `git add .`, no repository rename, no
secret or raw provider payload committed. See the PR for the CI result; nothing is claimed here that
CI has not reported.

## 16. Remaining risk

* Textual shell coverage is `PARTIAL`; a classifier bug is a write bypass.
* Symlink redirection is unverified (documented, not fabricated).
* Shared/frozen deliverables are denied by default; a Host merge grant does not exist yet.
* Enforce refuses interpreter execution, so it is not a drop-in default for coding workloads.
* `n=4` per round; statistical claims are limited to "never better".

## 17. Next-stage recommendation

1. **R1-2 Deliverable & Integration Verification** — the oracle failures show artifacts exist but
   are wrong; a deterministic deliverable check is cheaper than more routing work.
2. **R1-3 Budget Economics** — O0 spends 86 turns and still hits the budget; the trajectory now
   proves where the turns go.
3. **Workspace isolation** — only if a later stage shows that ownership must cover arbitrary
   programs. R1-1's data does not yet justify per-worker workspaces; it justifies better
   verification first.

---

## Verdicts

| Area | Verdict | Basis |
|---|---|---|
| A. Ownership decision correctness | **PASS** | 16 deterministic cases; normalizer and manifest digest tested |
| B. Cross-worker write protection | **PASS** (file tools + observed rollbacks) | foreign write/edit/rollback denied with no tool body; 2 historical commands reproduced |
| C. Bash enforcement coverage | **PARTIAL** | textual, `PARTIAL` coverage label, no OS isolation, symlinks unverified |
| D. Concurrent identity integrity | **PASS** | 20 interleaved calls, no crossing; resume re-check |
| E. Execution trace completeness | **PASS** | 100 % input coverage in 32/32 runs; 111 actions recovered; `TRACE_COMPLETE` |
| F. Runtime compatibility | **PARTIAL** | `off` is byte-identical; `enforce` blocks interpreters (19 denials, oracle 1/4) |
| G. Real evaluation impact | **PARTIAL** | O1 never beats O0 (1/16 vs 7/16); no live rollback occurred, so protection is not the cause |
| H. GitHub review readiness | **PASS** | scoped commits, no sensitive data, CI status reported in the PR; not merged |

## The five questions

1. **Can AtlasRuntime block the observed cross-worker file damage?** Yes for the two recorded
   commands and for every `file_write`/`file_edit`: the call is denied at M3 before the tool body,
   audited and recorded. Proven deterministically (T2–T6), not by the live model.
2. **Is this real isolation or partial enforcement?** Partial enforcement. One shared workspace,
   one sandbox, textual shell classification; `OWNERSHIP_ENFORCEMENT_COVERAGE = PARTIAL`.
3. **Can execution evidence be fully recovered after an abnormal stop?** Yes. Proposal, decision,
   observation, budget-stop and run-terminal events are persisted per transition; the private corpus
   restores every input (`input_digest_coverage = 1.0`), and 111 actions missing from `result.json`
   were recovered from the trajectory.
4. **Does the new mechanism break normal coding ability?** In `off` mode nothing changes (full suite
   green). In `enforce` mode it does restrict: interpreters and unprovable commands are refused, so
   the assisted arm passes less often (1/16 vs 7/16).
5. **What comes next?** Deliverable verification (R1-2) first, then budget economics (R1-3).
   Stronger workspace isolation is a later option, justified only if textual ownership proves
   insufficient for the chosen workloads.
