# Final-1.6 — Minimal Baseline Fixes and Evaluation Environment Readiness

Date: 2026-11-12. Branch: `chore/final-1-5-repository-baseline` (no new branch was created).
Worktree: `D:\Work\AtlasRuntime`. `BASE_SHA` = `0a11820b73948aa6dc4c36a65316b97feac17e5f`.

This stage did four things: ported the one authorised Recovery-Pilot fix, built a complete module
dependency inventory, replaced the historical test statistics with measured ones, and turned the
open integration questions into an explicit evaluation-readiness matrix. It designed no new runtime
mechanism.

## 1. Executive Summary

- Completed: Git audit, fix audit + cherry-pick, targeted regression, dependency inventory, extra
  installation, full offline regression, Pilot evidence review, integration-boundary audit,
  readiness matrix, publish-safety check, this report.
- The only `src/`/`tests/` change is the authorised commit `18e523c`
  (`src/atlasos/app/coding.py` +49, `tests/unit/test_coding_attribution.py` +43). Nothing else in
  `src/`, `tests/`, `experiments/`, `examples/`, `configs/` or `.github/` changed.
- No dependency constraint, lock file, CI workflow, oracle, test assertion or historical evidence was
  modified.
- One environment blocker found: the `contextpilot` extra installs a `.pth` hook that breaks 9
  unrelated child-process tests on Windows, so the offline suite cannot include it.
- Nothing was pushed, merged or rebased; `master` and all historical branches are untouched.

## 2. Git Baseline

| Item | Value |
|---|---|
| `BASE_SHA` (stage start) | `0a11820b73948aa6dc4c36a65316b97feac17e5f` |
| Branch | `chore/final-1-5-repository-baseline` |
| Working tree at start | clean (0 modified, 0 untracked) |
| Fix commit | `1b4f6be` — `git cherry-pick -x 18e523c` (clean, no conflict; original SHA recorded in the message) |
| Docs commit | the commit that adds this document (`docs: record final evaluation baseline readiness`) |
| Branch HEAD after stage | see the commit list in the terminal summary; this document cannot contain its own commit SHA |
| Recovery Pilot | `experiment/final-2-recovery-pilot` = `c5fce5c`, base `16251d0`, **unchanged** |
| Original worktree | `D:\Work\AtlasOS` at `c5fce5c`, untouched |
| `master` | local `07e18ec`, remote `3fbf3dd` — untouched |
| Push / merge / rebase | none |

## 3. General Fix Review

Source commit: `18e523c7d995e73f937c85b45cd0a2875953e3e2`, 2 files, +92 lines. It adds
`_classify_agent_outcome` (returns `MODEL_UNFINISHED` when the agent loop ends without success),
records `stop_reason`, `terminal_phase`, `model_turns`, `prompt_tokens`, `completion_tokens`, and
adds three regression tests.

| Check | Result | Evidence |
|---|---|---|
| 1. Does not modify the model prompt | PASS | diff touches only post-`run_tasks` result handling; no task text, system prompt or provider change |
| 2. Does not modify the tool runtime | PASS | `AtlasToolRuntime(...)` construction unchanged |
| 3. Does not modify sandbox lifecycle | PASS | `ProjectSandboxManager`, binding, probes, `close()` unchanged |
| 4. Does not modify agent execution policy | PASS | `AgentConfig` copy, `max_turns`, `ToolSecurityPolicy` unchanged |
| 5. Does not change oracle / deliverable scoring | PASS | `coding.py` has no oracle or scorer |
| 6. Cannot turn a failure into a success | PASS | `status = "completed" if failure is None and agent_success else "failed"` (`coding.py:743`); the patch only fills `failure` when it was `None`, so an unsuccessful agent stays `failed` |
| 7. Does not overwrite a known tool/budget/infra error | PASS with a caveat | assignment is `failure = failure or …`, and every exception path (`session`, `engine_close`, `idempotency_close`, `sandbox_cleanup`) assigns directly. See caveat A |
| 8. Token semantics match historical fields | **NO — documented** | the new `prompt_tokens` is `TokenUsage.input_tokens`, i.e. *cache-miss* input tokens (`integrations/deepseek.py:144`), not the total `prompt_tokens` recorded in `result.json` / `inference.jsonl`. See caveat B |
| 9. Existing consumers unbroken | PASS | `app/service.py` reads `session.get(...)` per key and persists the dict verbatim; the keys are additive. `test_app_service.py` 8/8 green |
| 10. Tests cover the original defect | PASS | 3 cases: success → no failure; `max_turns` → `MODEL_UNFINISHED` with `stop_reason` and `turns` in the message; `stop_reason=None` → still classified |

**Caveat A — classification precedence (static analysis, not executed).** The new assignment runs
inside the `try`, before the `finally` cleanup block. The existing guard
`if not retained and failure is None and not metrics.sandbox_destroyed:` (`coding.py:719`) therefore
no longer fires when the agent loop already failed, so a compound "agent unsuccessful **and** sandbox
destruction not confirmed" run now reports `MODEL_UNFINISHED` instead of
`SANDBOX_PROVIDER_FAILURE`. Both remain `status=failed`; only the class label is lost. This is a
property of the patch's position in existing code, not an extra modification inside the patch, and
fixing it would be a redesign — so it is recorded, not changed.

**Caveat B — misleading token field name.** `session["prompt_tokens"]` = cache-miss input tokens
(DeepSeek reports the split, so the miss branch of `_to_token_usage` is used). Any future aggregation
must not compare it with `result.json.prompt_tokens` or `InferenceRecord.prompt_tokens`, which include
cache hits. Recorded in `docs/project-status.md` §3.5.

Targeted regression: `tests/unit/test_coding_attribution.py` **3 passed**;
`tests/unit/test_app_service.py` **8 passed**. No other source was modified to make them pass.

## 4. Environment Inventory

| Item | Value |
|---|---|
| Python | CPython **3.12.15** (`D:\Work\AtlasRuntime\.venv\Scripts\python.exe`), satisfies `requires-python >=3.12` |
| Import origin | `D:\Work\AtlasRuntime\src\atlasos\__init__.py` (not the `AtlasOS` worktree) |
| `anycode-py` | **0.9.0** (unchanged throughout this stage) |
| `pip check` | "No broken requirements found" (before and after the extra installs) |
| Package | `atlasos 0.0.1`, editable from this worktree |

Declared extras in `pyproject.toml` (the only source of truth): `contextpilot`, `tools`, `sandbox`,
`evaluation`, `app`, `dev`. No extra was added or edited.

| Mechanism | Required package / extra | Installed | Version | Tests available |
|---|---|---|---|---|
| Context selector / store / models | core | yes | n/a | 3 + 4 + 5 unit tests |
| Stable renderer | core | yes | n/a | 7 + 5 |
| ContextPilot dedup transform | `contextpilot` extra | **no (removed)** | 0.5.0 installable | 4 tests, all skipped — see blocker |
| Keyword tool search | core | yes | n/a | 12 (`test_tools.py`) |
| Semantic tool search | `tools` extra → `sentence-transformers` | yes | 6.3.0 | **0 tests** |
| FAISS index | `tools` extra → `faiss-cpu` | yes | 1.16.0 | **0 tests** |
| Embedding model `all-MiniLM-L6-v2` | Hugging Face cache | yes (cached) | 384-dim, loads offline in 10.4 s | no test uses it |
| `torch` (transitive of sentence-transformers) | `tools` extra | yes | 2.15.0 | n/a |
| MCP | `tools` extra → `mcp` | yes | 1.30.0 | 1 (`test_tool_mcp.py`, now executed) |
| SQLite ledger / idempotency | core (stdlib `sqlite3`) + `dev` extra `anycode-py[persistence]` | yes | stdlib / 0.9.0 | 10 + 9 + 4 + 4 |
| OpenSandbox | `sandbox` extra | yes | 1.2.0 | 9 + 13 + 37 + 10 offline; live test skipped |
| Daytona | `sandbox` extra | yes | 0.231.1 | provider selection/config only; no live test |
| Inspect AI adapter | `evaluation` extra | yes | 0.3.289 | **0 tests** (imported by `experiments.evaluation.cli`) |
| Isolated oracle | `evaluation` + `sandbox` extras | yes | n/a | 26 + 7 + 7 |

**Environment blocker (recorded, not worked around).** Installing `contextpilot==0.5.0` writes
`.venv\contextpilot_hook.pth`, which fails at every interpreter start with
`ModuleNotFoundError: No module named 'contextpilot'` on stderr. The package itself imports and its
dedup API works (`contextpilot 0.5.0`, `dedup_chat_completions` from `contextpilot.dedup.block_dedup`),
and its 4 tests pass — but 9 previously green tests that assert child-process stderr now fail
(`test_m6_sqlite_process.py` 4, `test_sqlite_idempotency_process.py` 4,
`test_stage_g3_2_ingress_offload.py` 1). Measured state with the extra installed:
`888 passed, 9 failed, 2 skipped, 1 deselected`. The extra was therefore uninstalled to restore a
trustworthy suite; `pyproject.toml` still declares it, and ContextPilot dedup is `ENVIRONMENT_BLOCKED`
for the default offline suite on Windows. This is the same issue the CI workflow already documents.

No core version changed: `pip install -e ".[tools]"` added 19 packages (torch, sentence-transformers,
faiss-cpu, mcp, scikit-learn, …) and removed/upgraded **none**; `numpy 2.5.4`, `pydantic 2.14.3`,
`anycode-py 0.9.0`, `pytest 9.2.0`, `ruff 0.17.4`, `pyright 1.1.414`, `opensandbox 1.2.0`,
`daytona 0.231.1`, `inspect-ai 0.3.289` are unchanged. `uv.lock` was not touched (uv is not installed
in this environment; pip was used with the declared extras only).

## 5. Regression Coverage

Measured on `1b4f6be` with `ATLASOS_RUN_LIVE_TESTS=0` / `ATLASOS_RUN_LIVE_SANDBOX=0`:

| Run | Configuration | Result |
|---|---|---|
| A | `.[app,dev,evaluation,sandbox]` (Final-1.5 state) | 890 passed, 7 skipped, 1 deselected, 189.33 s |
| B | + `contextpilot` | **888 passed, 9 failed**, 2 skipped, 1 deselected |
| C | + `tools` (contextpilot removed) — current state | **891 passed, 6 skipped, 1 deselected, 213.74 s** |

898 tests are collected; 1 is deselected by the `live` marker. Run C is the current baseline.

| Skip | File | Reason | Class |
|---|---|---|---|
| 1 | `tests/integration/test_contextpilot_smoke.py:11` | `importorskip("contextpilot")` | OPTIONAL_DEPENDENCY_MISSING |
| 2 | `tests/integration/test_stage3_contextpilot.py:21` | `importorskip("contextpilot")` | OPTIONAL_DEPENDENCY_MISSING |
| 3, 4 | `tests/unit/test_context_optimizer.py:47,65` | `importorskip("contextpilot")` | OPTIONAL_DEPENDENCY_MISSING |
| 5 | `tests/integration/test_stage_g3_4a_isolation_live.py:72` | needs `ATLASOS_RUN_LIVE_SANDBOX=1` and a real OpenSandbox | LIVE_SANDBOX_DISABLED |
| 6 | `tests/unit/test_stage_r1_0_analysis.py:542` | raw Stage R0.5 run directories are not in the repository | OTHER (fixture absent) |

Historical difference vs Final-0/1 (`895 passed / 1 skipped / 1 deselected`, 897 collected):
this stage cannot reproduce that number and cannot prove its composition. The measured difference is
explained by **four** items, of which three are verified and one is inference:
(i) 4 ContextPilot tests are skipped here (+4 skips, −4 passes if installed and passing);
(ii) 1 MCP test was skipped then and runs now (measured: +1 pass after `tools`);
(iii) 3 new tests exist in this branch (`test_coding_attribution.py`);
(iv) the R1-0 raw-run test passed historically only if `experiments/runs/stage-r0-5/` existed in the
historically used workspace — plausible, **not proven**, and no per-test historical log exists to
confirm it. No historical skip ID was invented.

Core-mechanism coverage in run C:

| Core mechanism | Tests | Executed | Passed | Skipped | Reason |
|---|---|---|---|---|---|
| Context selector / store / models | 12 | yes | 12 | 0 | — |
| Stable renderer + layout hashes | 12 | yes | 12 | 0 | — |
| ContextPilot dedup (real transform) | 4 + 2 | no | 0 | 6 | dependency removed (blocker above) |
| Context transform identity path | `test_context_optimizer` (2 of 4) | yes | 2 | 0 | — |
| Keyword tool search + disclosure | 12 + 4 | yes | 16 | 0 | — |
| Semantic tool search / FAISS | 0 | — | — | — | **no test exists** |
| MCP discovery | 1 | yes | 1 | 0 | — |
| SQLite ledger / idempotency / process durability | 10 + 9 + 4 + 4 | yes | 27 | 0 | — |
| Sandbox binding / ownership / reattachment | 17 + 25 + 17 + 37 | yes | 96 | 0 | — |
| OpenSandbox provider | 9 | yes | 9 | 0 | live container test skipped |
| Worker / scoped worker / contracts | 12 + 21 + 16 | yes | 49 | 0 | — |
| Delegation router | 34 + 19 + 4 | yes | 57 | 0 | — |
| Shared budget | 15 | yes | 15 | 0 | — |
| Durable suspension / approval | 23 + 10 + 2 + 6 + 10 | yes | 51 | 0 | — |
| Trusted recovery | `test_verified_reattachment` 17 + `test_stage_d_*` 28 | yes | 45 | 0 | — |
| Trajectory / integrity | 11 + 9 + 12 | yes | 32 | 0 | — |
| Verification engine | 14 + 18 + 12 + 4 + 6 | yes | 54 | 0 | — |
| Isolated oracle | 26 + 7 + 7 | yes | 40 | 0 | real grading not run |
| Inspect AI adapter | 0 | — | — | — | **no test exists** |
| Application coding attribution (new) | 3 | yes | 3 | 0 | — |

Answers to the ten coverage questions: (1) semantic embedding retrieval never runs — no test exists;
(2) the real ContextPilot transform does not run in the suite (dependency blocker), only the identity
path; (3) all router unit tests run (57); (4) shared-budget concurrency tests run (15); (5) M4 SQLite
process-durability tests run (8 of them spawn child processes); (6) durable suspension tests run (51);
(7) trusted-recovery deterministic tests run (45); (8) OpenSandbox offline tests run, the live
container test is skipped by design; (9) verification and oracle-isolation tests run (94);
(10) yes — ContextPilot dedup and the Inspect adapter have no executed coverage, and semantic tool
search has no test at all. This is a functional-regression statement, not a generalisation claim.

## 6. Recovery Pilot Evidence

Source: `git show c5fce5c:docs/final-2-recovery-pilot.md` (read-only; branch not switched).

| # | Fact | Recorded value |
|---|---|---|
| 1 | S1/S2 real-model smoke | S1-A `max_turns=10`: `failed`, `agent_success=False`, `failure_class=null`, 11 tool calls, exported deliverable passed 10 local tests. S1-B `max_turns=20`: `completed`, 12 turns, 13 tool calls, 5 102 prompt / 3 231 completion, 10 ledger rows, 66 trajectory events, 15 local tests. S2 `bugfix-001-slugify`: oracle pass in a sandbox, 5 turns, 11 328 prompt (10 112 hit / 1 216 miss), 562 completion, USD 0.00109987, 34.9 s |
| 2 | Native AnyCode normal run | N0: oracle 1, agent success, 5 turns, 5 tool calls, 10 126 prompt, 767 completion, 8.8 s |
| 3 | AtlasRuntime normal run | R0: oracle 1, agent success, 5 turns, 4 tool calls, 11 328 prompt, 562 completion, 34.9 s |
| 4 | Fault-injection position | native: first change under `<run_dir>/workspace/**`; Atlas: first `action_proposed` trajectory event; `taskkill /PID <pid> /T /F`, grace 0.6 s / 0.4 s; confirmed fault = alive at boundary and gone after |
| 5 | Validity of the four arms | N0/R0 valid normal baselines; N1/R1 valid kills (`fault_observed=true`); one earlier R1 calibration attempt fired at 2.84 s on the first trajectory event and produced 0 records — reported, not counted |
| 6 | Any durable checkpoint | **none on disk in any arm** (`Checkpoint on disk: none / none / none / none`) |
| 7 | Any resume actually executed | **impossible in all arms** (`Resume attempted: impossible`) |
| 8 | Sandbox reattach to the original | **not attempted and not possible**; reattachment is a manual operator path (`trusted_recovery.py`) and was not used |
| 9 | Unknown side effect | one `IN_PROGRESS` ledger row for `bash`/`shell.execute` with payload + input digest in R1; the pilot states the effect is unknown, not that it did not happen. No *unexpected* side effect observed |
| 10 | Resource leak | R1 leaked one live sandbox pair (`sandbox-…` + `sandbox-egress-…`), reaped manually; runtime has no sweeper |
| 11 | Model failure vs infrastructure failure | reported separately: `failure_class`, `sandbox_destroyed`, `cleanup_error`, `host_isolation_violations`; S1-A is a model/loop outcome, S2/R0 are oracle outcomes, N1/R1 are injected kills |

Consequence for evaluation: a recovery *comparison* between native and Atlas is
**NOT_APPLICABLE** on this candidate — neither arm has a checkpoint to resume from, so there is no
resume success rate to measure. The only measured difference is post-crash accountability
(0 durable records vs 1 in-flight ledger row + 1 binding + 1 idempotency claim + 4 trajectory events).
No recovery advantage may be claimed from these runs.

## 7. Runtime Integration Boundaries

### 7.1 Context Plane

1. **Interfaces.** Input: `InMemoryContextStore` of `ContextBlock`/`ContextRef`, an `AtlasTaskSpec`
   (`task_id`, `title`, `description`, `depends_on`), the agent id and the available refs.
   `TaskScopedSelector.select(...) -> Sequence[ContextBlock]`;
   `PromptRenderer.render(*, stable_system_prefix, agent_role, task, context_blocks, dynamic_tail=None)
   -> RenderedPrompt` (`messages`, `context_layout`, three hashes, three token splits).
2. **How coding builds a request.** `ScopedWorkerFactory.create` renders the worker `system_prompt`
   (`render_worker_context`) and returns an `AgentConfig`; `AtlasContextEngine.build_agent` builds an
   `AtlasToolAgent`; the prompt for the single task is the `TaskSpec.description`; the conversation
   grows inside `AtlasAgentRunner`.
3. **Reusable wiring points.** `AtlasContextEngine.__init__(prompt_builder=…)` overrides
   `_build_task_prompt(task, queue)` — the only prompt seam Atlas already subclasses; `build_agent`
   registers `before_turn` hooks in a deliberate order (compaction → conversation snapshot → budget);
   `AtlasToolAgent` owns conversation persistence.
4. **Workflow-level context capabilities** (not applicable to a single task): dependency-output
   projection as `task-output:<id>` blocks, selection by `depends_on`, placement of tasks to agents,
   durable rebuild of the store from a checkpoint.
5. **Model-call-level capabilities**: deterministic ordering, layout/prompt hashes, token splits,
   dedup transform, budget admission.
6. **Not applicable to single-worker coding**: placement/context-locality, dependency projection,
   cross-worker handoff refs.
7. **Useful in a multi-worker DAG**: placement, dependency projection, handoff refs, per-worker
   context scoping.

Minimal candidate wiring (not implemented, not authorised): synthesise one `AtlasTaskSpec` for the
coding task and pass a `prompt_builder` to `AtlasContextEngine` that calls the selector + renderer
instead of `AgentConfig.system_prompt` + `TaskSpec.description`. Open decision for the owner: whether
the "context" of a single coding task is the task text only (then the plane adds hashes/accounting,
not selection) or also files/tool results (then it overlaps with history compaction and tool-result
offload, which already exist).

### 7.2 Coding checkpoint and recovery

| Capability | Status |
|---|---|
| Workflow checkpoint + `AtlasRuntime.resume` (manifest verify + context rebuild) | CURRENTLY_SUPPORTED (library API only; no CLI) |
| Durable context rebuild from checkpoint | CURRENTLY_SUPPORTED |
| Persistent approval / suspension resume | CURRENTLY_SUPPORTED (app coding path, stores configured) |
| Sandbox binding + explicit reattachment | CURRENTLY_SUPPORTED (operator harness only) |
| Uncertain-side-effect classification | CURRENTLY_SUPPORTED (both coding paths) |
| Ordinary coding crash recovery | **CURRENTLY_UNSUPPORTED** |

`app/coding.py:646`, `evaluation/runner.py:1003-1004` and `evaluation/baseline.py:143` build a bare
`OrchestratorConfig()` → `checkpoint=None` in **all three** compositions, so no checkpoint file is
ever written for a coding run, on either arm. Enabling a checkpoint would still not equal crash
recovery; MINIMAL_INTEGRATION_REQUIRED (all of it is design input for Final-2, none of it is done
here):

| Piece | Existing asset | What is missing |
|---|---|---|
| Checkpoint | `CheckpointConfig` (workflow path) | pass it in the coding composition; task-level granularity only |
| Run metadata | `durability/manifest.py` | a manifest is written only by `AtlasRuntime`, not by `run_coding_session` |
| Tool disclosure state | `run_dir/tool_state.json` | reload path into a resumed run |
| Worker identity | `workers.json` + `ScopedWorkerFactory.resume` (hash-verified) | caller outside `trusted_recovery.py` |
| Action ledger | `ledger.db` + `IN_PROGRESS` classification | reconciliation at start of a resumed run |
| Idempotency | `idempotency.db` (SQLite) | reuse across the resumed process |
| Sandbox binding | `sandbox_bindings.db` | automatic (or operator-approved) reattach decision |
| Budget | none in the coding composition | a run-scoped budget would have to be introduced first |
| Recovery cleanup | none | orphan-sandbox sweep after an abrupt kill |

### 7.3 Router

Router = admission policy for a candidate decomposition, not a planner. `DIRECT` replaces the worker
list with the A0 single-worker plan and forces single-worker grading; `DELEGATE_PARALLEL` runs the
candidate DAG unchanged. `DELEGATE_ONE` exists in the vocabulary and is only reachable through the
single-read-only-task gate; the runner branches on `is_direct` only, so a `DELEGATE_ONE` decision
executes as the candidate plan. The candidate DAG comes from the benchmark's frozen `WorkerStep`
list (`stage_r0_routing.py::build_proposal`). Default `delegation_route_mode="off"`; no `src/` module
imports the router; it is not wired into app coding; it never reads oracle data — its evidence is
task text, declared paths and static analysis.

Two future layers, recorded only:

- **C1 — Provided-proposal routing** (feasible now): give always-single, always-multi and selective
  the same externally provided candidate DAG and the same public task information; measure oracle
  pass, cost, latency, delegation rate, harmful delegation and routing regret. Validates the policy,
  not decomposition.
- **C2 — Runtime entry integration** (feasible later, needs design): connect the existing router to
  a real entry point, reusing `build_proposal` + `compile_workflow`. No LLM planner is introduced by
  default; that is an owner decision.

## 8. Evaluation Readiness Matrix

| Issue | Current Evidence | Blocks Track | Recommended Decision | Requires Runtime Change |
|---|---|---|---|---|
| Failure attribution fix | ported as `1b4f6be`; 3 targeted tests + 8 service tests green | none (removes a blocker for A/B attribution) | READY | no (done) |
| Classification precedence (caveat A) | `coding.py:719` guard loses `SANDBOX_PROVIDER_FAILURE` when the agent also failed | A/B attribution fidelity, narrow | DEFERRED_TECHNICAL_DEBT (decide in Final-2) | yes, if fixed |
| `session.prompt_tokens` = cache-miss tokens | `integrations/deepseek.py:144` | A metrics if mis-read | DEFERRED_TECHNICAL_DEBT (documented) | no |
| Semantic tool search dependency | installed (`sentence-transformers` 6.3.0, `faiss-cpu` 1.16.0), model cached, **0 tests** | A (tool module) | REQUIRED_BEFORE_FORMAL_EVALUATION — a real semantic-retrieval check must exist before any claim | no runtime change; test/evidence gap |
| ContextPilot dependency | `.pth` hook breaks 9 unrelated tests; 4 tests skip | A (context module) | MODULE_ONLY_VALIDATION (measure dedup in an isolated env, not in the default suite) | no |
| Context Plane not wired into coding | `docs/runtime-entrypoints.md` §6 matrix | B (integration benefit claim) | MODULE_ONLY_VALIDATION + NEEDS_OWNER_DECISION for any wiring | yes, if wired |
| Coding checkpoint not enabled | `coding.py:646`, `runner.py:1003`, `baseline.py:143` | A (reliability module), B | NEEDS_OWNER_DECISION — a product change, not an experiment detail | yes |
| Router needs a candidate DAG | `build_proposal` from benchmark metadata | C | READY for C1 as designed | no |
| Router not in app coding | no `src/` importer | none for C1 | OPTIONAL_DEMO_INTEGRATION (C2) | yes, if integrated |
| App coding needs a UAT workspace | `service.py::_resolve_workspace` requires `csv_parser.py` | none for evaluation; limits demo scope | DEFERRED_TECHNICAL_DEBT | yes, if generalised |
| App vs evaluation metrics differ | no `input_digest_coverage` in the app path; different compositions | B (comparability) | MODULE_ONLY_VALIDATION — never present app numbers as benchmark numbers | yes |
| `patch_changed_files` miscount | counts `git status --porcelain` lines (1 vs 2 files) | none (labelling) | DEFERRED_TECHNICAL_DEBT | yes, trivial |
| Sandbox leak after an abrupt kill | pilot: 1 orphaned pair, reaped manually | A (recovery module), safety of any kill test | REQUIRED_BEFORE_FORMAL_EVALUATION for any fault-injection track (a sweeper or a documented reap step) | yes |
| Verification recall limit | 23 TP / 23 FN; observation only | A (observability module) | MODULE_ONLY_VALIDATION | no |
| Live sandbox test disabled | `ATLASOS_RUN_LIVE_SANDBOX=0` | B (sandbox evidence) | REQUIRED_BEFORE_FORMAL_EVALUATION to run at least one live sandbox acceptance | no |

Not everything is a must-fix: the absence of ordinary coding crash recovery does not block
module-level validation of workflow resume, durable approval, ledger classification or sandbox
reattachment; the absence of a planner does not block C1; the missing Context-Plane integration does
not block its module evaluation, but it does forbid an "integrated context benefit" statement.

## 9. Publish Readiness

| Check | Result |
|---|---|
| Pending commits since `BASE_SHA` | `1b4f6be` (2 files) + the docs commit (report + status/limitations edits) |
| Core scope | exactly `src/atlasos/app/coding.py`, `tests/unit/test_coding_attribution.py` |
| Dependency scope | `pyproject.toml`, `uv.lock`, `configs/`, `.github/` unchanged |
| Deletions | none |
| New raw artifacts in the commits | none — `git status` is clean; every scratch file produced by this stage (`*.log`, `freeze-*.txt`, `collect.txt`) was deleted before committing |
| Secret scan of the pending commits | pattern scan for `sk-…`, `ghp_…`, private-key headers, and `DEEPSEEK_API_KEY=<value>`: **0 hits**. A pattern scan cannot prove the repository is secret-free; it only bounds the obvious cases |
| `.env` / `.venv` / run directories | ignored (`.gitignore:16,7,22`), none tracked |
| Historical risk (B0) | raw business runs exist in Git history on `fix/b0-r2-first-native-agent` and were removed from the index by `fix/b0-r3-business-completion-contract`; they remain recoverable from history. No history rewrite is proposed here |
| Recovery-Pilot raw data | stays in `D:\Work\AtlasOS\experiments\runs\final2\…`, local and ignored; not copied |

## 10. Final Verdict

| Dimension | Verdict | Basis |
|---|---|---|
| Stable code baseline | **PASS** | one authorised commit cherry-picked cleanly; no other source change; lock file and CI untouched |
| Offline regression | **PASS** | 891 passed / 6 skipped / 1 deselected; ruff, format, pyright clean; CLI, 30-task inventory and offline demo verified |
| Full module dependency readiness | **PARTIAL** | `tools`/`sandbox`/`evaluation`/`app`/`dev` installed and exercised; ContextPilot blocked by its own `.pth`; semantic retrieval and the Inspect adapter have **no tests** despite installed dependencies |
| Integrated runtime readiness | **PARTIAL** | no live provider or sandbox run was performed this stage; the last real execution evidence is the pilot at `c5fce5c` |
| Router evaluation readiness | **PARTIAL** | policy and tests are ready for C1; no candidate-DAG generator, no C2 wiring, no positive live admission |
| Recovery evaluation readiness | **BLOCKED** (completion-rate) / **PARTIAL** (accountability) | no composition writes a checkpoint, so no arm can resume; post-crash accounting is measurable |
| Public branch readiness | **PARTIAL** | local-only branch; no CI run, no push, no demo recording |

## 11. Recommended Next Step

1. Project owner reviews this report and `docs/final-1-5-repository-baseline.md`.
2. Enter **Final-2 — Evaluation Protocol** and treat the following as design decisions, not code:
   checkpoint wiring for the coding composition, Context-Plane integration (if any), router C1
   protocol and its candidate-DAG source, a semantic-retrieval evidence requirement, and the
   ContextPilot isolated-environment procedure.
3. Nothing in this report is implemented beyond `1b4f6be` and the documentation.

Do not push, merge or start Final-2 from this stage.
