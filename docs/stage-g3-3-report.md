# Stage G3-3 — Worker Task Contract & Execution Discipline

Round: `g33-20261111T051039Z`
Arms: **C0** `worker_contract_mode=legacy` vs **C1** `worker_contract_mode=structured`
Unchanged in both arms: `handoff=dual`, `budget=guarded`, `tool_error=corrected`,
`compaction=off`, `tool_result_mode=off`.

---

## 1. Audit of the original worker task assembly

| # | Question | Answer (verified in code) |
|---|---|---|
| 1 | Where does the user task enter? | `experiments/evaluation/stage_e_tasks.py` → `StageEDefinition.overall_prompt` + per-step `WorkerStep.prompt` (public text, plus the shared `ENV` sentence for module tasks). |
| 2 | How are DAG subtasks formed? | `variants()` builds one `BenchmarkTask` per arm; A1 keeps `definition.workers` (task_id, title, prompt, `depends_on`). `runner.py:785-793` maps each step to one `TaskSpec` with `title`, `description`, `assignee`, `depends_on`. |
| 3 | What text does each worker receive? | **Two copies**: `ScopedWorkerFactory.create(assignment.task=…)` → `render_worker_context()` embeds it as JSON inside the *system prompt* (`ASSIGNED TASK — UNTRUSTED DATA`); and `TaskSpec.description = step.prompt` becomes the tool-loop's opening user turn (`Task: <description>`). |
| 4 | Does a worker know its files? | Only implicitly, from prose: "Implement report/stats.py". No ownership field exists anywhere in the DAG. |
| 5 | Dependencies / deliverables? | `depends_on` exists in the DAG and reaches AnyCode, but the worker text is not told about it (only a prose hint such as "using parser and stats"). Deliverables exist only as `required_artifacts` on the *benchmark*, never shown to the worker. |
| 6 | What decides worker success? | `AgentRunResult.success` from the tool loop (stop reason `success`), independent of the oracle. Task success = `failure_class == TASK_SUCCESS`, which is the oracle + harness gates. |
| 7 | Does the text already carry delivery constraints? | Only the shared `ENV` line ("Work in /workspace/project. Do not modify tests/ or files outside the allowed module paths.") and, for `e1-12`, explicit "Do not create final.json" clauses. Nothing about ownership boundaries, verification, or completion reporting. |
| 8 | Minimal way to add the contract? | Rewrite **only** `TaskSpec.description` in `runner.py`; leave the assignment (system prompt), profile, tools, DAG and budget untouched. No AnyCode change, no new agent, no scheduler change. |

That is exactly what this stage implements: `step.prompt` → `worker_task_text(step, …)` for the task
turn only.

---

## 2. Contract design and data sources

`experiments/evaluation/stage_g3_worker_contracts.py` (pure, deterministic, no I/O):

```
[WORKER TASK CONTRACT]
SAMPLE: <sample_id> | STEP: <task_id>

TASK GOAL            ← the step's public prompt, verbatim (ENV sentence folded into SCOPE)
WORKSPACE SCOPE      ← fixed text: /workspace/project, read freely, write only what you own
FILE OWNERSHIP       ← paths extracted from the step's own public text
DEPENDENCIES         ← step.depends_on + sibling modules named in the text
DELIVERABLES         ← owned files, or an explicit "not specified" marker
VERIFICATION         ← public self-checks only (import check, smoke test, task examples)
COMPLETION REPORT    ← what changed, which checks ran, observed result, limitations
```

Data sources, per field:

| Field | Source | Guard |
|---|---|---|
| goal | `step.prompt` | original text preserved; only the trailing shared `ENV` sentence is folded into SCOPE so it is not stated twice |
| owned files | paths named by a write verb in the step's own text | nearest-verb rule: "read X then implement Y" owns `Y` only; a negated clause ("Do not create final.json") becomes an **exclusion**; no path ⇒ `not specified in the task text` |
| dependencies | `step.depends_on` | declared graph edges only |
| referenced siblings | sibling task ids/`<stem>` mentioned in the text | ≥ 3 chars and word-boundary, so single-letter steps `a`/`b` can never be inferred from prose |
| deliverables | owned files | no file ⇒ "produce the artifacts named in your task description"; never `required_artifacts` |
| verification / completion | fixed text | no hidden tests, no expected values, no oracle strings |

Never read by this module: `hidden_files`, the oracle, previous run trajectories. The builder's
parameters are `(step, steps, sample_id)` — nothing else is reachable.

**Deliberately not asserted:** the contract is task guidance, not a sandbox policy. It does not
claim enforcement, does not restrict reads, and does not block commands. Ownership is a writing
convention; M3 remains the only authority over what a worker may execute.

---

## 3. Changed files and behavioural difference

| File | Change |
|---|---|
| `experiments/evaluation/stage_g3_worker_contracts.py` | **new** — contract model, path classifier, `worker_task_text`, `contract_overhead` |
| `experiments/evaluation/runner.py` | `RunnerSettings.worker_contract_mode` (in `settings.digest()` and `environment`); task text built per step; `metrics.worker_contract` records mode, per-worker chars, overhead, `task_text_digest` |
| `experiments/evaluation/stage_e_cli.py` | `--worker-contract-mode legacy\|structured` + echo |
| `experiments/evaluation/stage_e_rounds.py` | `g3-3` suite (c0/c1, alternating order, everything else pinned) |
| `tests/unit/test_worker_contract.py` | **new** — 21 tests |
| `tests/integration/test_stage_g3_3_contract_delivery.py` | **new** — 2 tests on the messages the model actually receives |
| `.gitignore` | ignore `experiments/results/stage-g3-3/` |

Behaviour difference is confined to one string: the worker's task turn. The system prompt (verified
byte-identical in the integration test), the tool list, the execution context, the DAG edges and the
budget are unchanged. `legacy` returns the original prompt object itself (no copy, no normalisation).

Contract cost (exact, tiktoken over the rendered block):

| Task | workers | chars | tokens per run |
|---|---|---|---|
| `e1-02-report` | 3 | 4,221 | 890 |
| `e1-07-export` | 2 | 2,833 | 594 |
| `e1-12-conflict` | 3 | 4,492 | 988 |

Total over the six C1 runs: 23,092 chars ≈ 9,888 prompt tokens.

---

## 4. Unit and integration tests

`tests/unit/test_worker_contract.py` (21 tests) covers the required list directly:

1. legacy mode returns the identical object for every step of every definition;
2. the structured block contains all seven sections;
3. the original goal is preserved (`step.prompt in text`, ENV appears exactly once);
4. declared dependencies are rendered (`Declared upstream steps: formatter.`), and an empty graph
   says so instead of inventing one;
5. ownership is never invented (a path-free step owns nothing);
6. deliverables are not fabricated (`formatter.py` is read-only for the exporter; a path-free step
   gets the "not specified" marker);
7. no hidden/oracle content leaks (parametrised over `test_hidden_`, `assert `, the exact hidden
   expected strings, `pytest.raises`, `hidden_files`, `oracle`);
8. building contracts does not mutate the original `WorkerStep`s (prompt/depends_on/identity);
9. contracts do not cross workers (`parser` never mentions `report/stats.py` and vice versa);
10. the mode switch only appends (`structured.startswith(legacy)`);
11. the contract is deterministic;
12. the DAG is untouched — covered by the integration test below in addition to the unit assertions.

`tests/integration/test_stage_g3_3_contract_delivery.py` drives the real stack
(`ScopedWorkerFactory` → `AgentConfig` → `TaskSpec` → `AtlasContextEngine`) with a scripted provider
and asserts on the captured `messages` **and** `options.system_prompt`:

* the system prompt is byte-identical in C0 and C1 for all three workers, and never contains the
  contract marker;
* the opening user turn differs: C1 is `legacy + contract`, C0 has no marker;
* the C1 task text the model saw equals `worker_task_text(...)` exactly;
* DAG titles/`depends_on`, worker names, tool lists and execution contexts are identical;
* both runs succeed and the G2a budget state is present (no rejections).

---

## 5. Full regression

```text
.\.venv\Scripts\python.exe -m pytest   → 587 passed, 1 deselected in 200.81s
ruff check .                            → All checks passed!
ruff format --check .                   → 327 files already formatted
pyright                                 → 0 errors, 0 warnings, 0 informations
```

No test was removed, skipped or relaxed.

---

## 6. The 12 real runs

| Task | Rep | C0 run | C1 run | C0 oracle | C1 oracle | C0 class | C1 class |
|---|---|---|---|---|---|---|---|
| e1-02-report | 0 | `eval-59581d807e8b` | `eval-8c06712dc2db` | FAIL | FAIL | BUDGET_EXCEEDED | BUDGET_EXCEEDED |
| e1-02-report | 1 | `eval-8417b5a70458` | `eval-d0f112145fcc` | FAIL | FAIL | BUDGET_EXCEEDED | BUDGET_EXCEEDED |
| e1-07-export | 0 | `eval-b9dfe86e260d` | `eval-b62f5b1f8fb0` | FAIL | FAIL | ORACLE_FAILURE | ORACLE_FAILURE |
| e1-07-export | 1 | `eval-47ff3e960815` | `eval-20ce103d6555` | FAIL | FAIL | ORACLE_FAILURE | ORACLE_FAILURE |
| e1-12-conflict | 0 | `eval-c5db193e8a2e` | `eval-103db874b79b` | PASS | PASS | TASK_SUCCESS | TASK_SUCCESS |
| e1-12-conflict | 1 | `eval-d733100178ad` | `eval-932ca2b86345` | PASS | PASS | TASK_SUCCESS | TASK_SUCCESS |

Exact run ids, worker ids, configs and per-worker texts are in
`experiments/results/stage-g3-3/g33-20261111T051039Z/` (`manifest.json`, per-run `result.json`,
`tool_conversations.json`, `analysis/runs.csv`, `analysis/workers.csv`). Each `result.json` carries
`metrics.worker_contract.task_text_digest`, so the two arms are provably different only in the task
text.

---

## 7. Worker execution behaviour

| Metric | C0 legacy | C1 structured |
|---|---|---|
| Tool turns (all workers) | 101 | 128 |
| EXPLORE turns | 54 | 55 |
| **Exploration ratio** | **0.5347** | **0.4297** |
| IMPLEMENT turns | 6 | 6 |
| VERIFY turns | 8 | 8 |
| **Unrelated probe turns** | **31** | **22** |
| Workers with ≥1 probe | 9 / 16 | 5 / 16 |
| Avg first implementation turn (workers that wrote) | 6.33 (n=6) | 6.40 (n=5) |
| Ownership violations | 0 | 0 |
| Deliverables: incomplete / unverified / verified | 4 / 10 / 2 | 5 / 9 / 2 |
| Workers that claimed a passing check | 1 | 1 |
| …of which ran no check | 1 | 1 |
| Workers that never wrote a file | 10 / 16 | 11 / 16 |

Probe evidence (`analysis/probes.csv`, 36 rows in C0 vs 22 in C1) is dominated by the G3-0 pattern
the contract is meant to stop:

```text
cd /tmp && for f in *.stdout *.stderr; do ... cat "$f" ... done        # grader stdout
cd /tmp && git bundle list-heads project.bundle; git bundle verify …   # packaged git history
find / -maxdepth 3 \( -name "*test*" -o -name "*.md" \) …              # hidden tests
```

Per task:

* **e1-02-report** — discipline improved: explore ratio 0.647 → 0.436, probe turns 26 → 11, first
  implementation 7.67 → 6.00, worker success 0/6 → 2/6, cost $0.0232 → $0.0174.
* **e1-07-export** — discipline *regressed*: probe turns 5 → 11, first implementation 5.0 → 7.0,
  worker success 3/4 → 2/4. In both C1 runs the **formatter** worker ran out of turns
  (`max_turns`, `doom_loop`) after 5–6 probe turns and left `NotImplementedError` in place.
* **e1-12-conflict** — collaboration intact (all 6 workers succeed in both arms, `state.json` /
  `final.json` written); contract cost 988 tokens per run and turns went 19 → 26 without changing
  the outcome.

---

## 8. Oracle, cost and budget

| Metric | C0 | C1 | Δ |
|---|---|---|---|
| Oracle pass | 2/6 | 2/6 | 0 |
| `TASK_SUCCESS` | 2/6 | 2/6 | 0 |
| Worker success | 9/16 | 10/16 | +1 |
| Prompt tokens | 407,727 | 468,931 | +15.0 % |
| Completion tokens | 18,091 | 21,476 | +18.7 % |
| Cache hit tokens | 353,024 | 432,128 | +22.4 % |
| Cache miss tokens | 54,703 | 36,803 | −32.7 % |
| Cache hit ratio | 0.8658 | 0.9215 | +5.6 pp |
| Cost (USD) | 0.040238 | 0.039405 | −2.1 % |
| Model turns | 111 | 141 | +27.0 % |
| Tool calls | 122 | 142 | +16.4 % |
| Avg prompt per turn | 3,673 | 3,326 | −9.5 % |
| Budget exceeded | 2/6 | 2/6 | 0 |
| Admission rejections | 6 | 3 | −3 |
| Contract overhead | 0 | 9,888 tokens | — |

Attribution:

1. **Contract text is small**: 9,888 tokens over six runs (≈1.6 k per run), i.e. ~2 % of the C1
   prompt. It cannot explain +61 k.
2. **Turns dominate**: +30 model turns re-send the conversation; e1-12 alone went 19 → 26 turns and
   39.9 k → 72.0 k prompt tokens with no behavioural change.
3. **Per-turn prompt is down 9.5 %**, consistent with less exploratory output per turn.
4. **Cache improved 5.6 pp** and cost fell 2.1 % despite +15 % prompt tokens — the only efficiency
   signal, but n=6/arm and not separable from the different trajectories.
5. **Budget**: same 2/6 `BUDGET_EXCEEDED` (both e1-02 pairs), fewer admission rejections in C1
   (6 → 3), i.e. the extra text did not push any run over the ceiling that was not already there.

Failed-check sets are identical across arms: `e1-02` fails `hidden_tests_passed` in all four runs;
`e1-07` fails `hidden_tests_passed` everywhere and `all_workers_produced_output` in three of four.

---

## 9. Benchmark integrity check

| Check | Result |
|---|---|
| Hidden test content in any worker text | none — the builder cannot reach `hidden_files`; parametrised unit test over all definitions |
| Hidden markers in any of the 12 conversations | `test_hidden` = 0, `oracle-workspace` = 0 |
| Oracle used to shape the contract | no — `grep` of the module shows no oracle/definition-hidden access; parameters are `(step, steps, sample_id)` |
| C1 receiving information C0 lacks | only the contract block: own paths, declared upstream steps, generic verification advice. All three are public (task prose + DAG). No expected values, no hidden strings, no "how to pass" hint. |
| Oracle feedback during a run | none; scoring happens after the run in `evaluate_oracle` |
| **Risk found (not fixed here)** | the sandbox itself exposes grader artifacts: workers read `/tmp/*.stdout`, `/tmp/*.stderr` and `/tmp/project.bundle` (oracle stdout/stderr and a git bundle of the fixture). One `e1-07` legacy run used them to inspect hidden-test output. This is a **Benchmark Integrity risk of the fixture, not of the contract**, and must be fixed separately (e.g. keep oracle artifacts out of the sandbox mount). |

---

## 10. Verdicts

**A. Contract Mechanism Correctness — PASS**
The contract is rendered deterministically from public data, reaches 16/16 C1 workers
(`contract_present=16`), and touches nothing else: legacy is byte-identical, the system prompt is
byte-identical, DAG/tools/budget are identical, the original goal is preserved, ownership is derived
by a stated rule, and unspecified fields stay unspecified.

**B. Worker Execution Discipline — INCONCLUSIVE (positive on e1-02, negative on e1-07)**
Globally: exploration ratio −10.5 pp, unrelated probe turns −29 %, workers probing 9 → 5. On
`e1-02-report` the effect is clear and in the intended direction (probes 26 → 11, first
implementation turn 7.67 → 6.00, worker success 0/6 → 2/6). On `e1-07-export` it went the other way
(probes 5 → 11; the formatter worker burned its 14 turns probing and delivered a stub). First
implementation turn is unchanged on average, so "implementation starts earlier" is not established
globally.

**C. Task Quality — FAIL (no improvement)**
Oracle 2/6 in both arms; `TASK_SUCCESS` 2/6 in both; deliverable completion slightly worse
(incomplete 4 → 5, verified 2 → 2). One worker's success flipped positive (e1-02), two flipped
negative (e1-07). No evidence that the contract helps the model produce correct code.

**D. Efficiency — INCONCLUSIVE**
Prompt tokens +15 %, completion +19 %, turns +27 % — all worse. Cost −2.1 % and cache hit ratio
+5.6 pp — slightly better, driven by cache reuse rather than by shorter prompts. The contract itself
costs only ~1.6 k tokens per run, so the regression is trajectory length, not prompt size. Net: no
demonstrated efficiency gain.

**Recommendation.** Keep `worker_contract_mode=legacy` as the default; keep the mechanism available
(`--worker-contract-mode structured`) and the tests in place. A next iteration should (a) drop the
sentence that enumerates forbidden artifacts — naming "hidden tests, scoring outputs, Git history"
plausibly *invites* the search we are trying to suppress, which is consistent with e1-07's 5 → 11
probe turns, and (b) be measured on a task family with more workers before being promoted. Do not
stack more prompt rules in this round.

---

## Known limitations

1. **n = 2 per task per arm** (12 runs). Directional only; the per-task splits are anecdotes.
2. **Trajectory variance dominates.** C1 ran 30 more turns; no attempt is made to normalise.
3. **`first_implement_turn` only sees `file_write`/`file_edit`.** `e1-12` workers write via bash
   redirection, so their value is empty rather than zero.
4. **Ownership violations are 0 in both arms**, so the metric cannot show improvement here — the
   `e1-12` overlap seen in G3-0 was a *write order* problem, not a foreign-file write, and the
   oracle allows both workers to touch `state.json`.
5. **Deliverable states are heuristic**: "verified" means the worker ran at least one successful
   command matching the VERIFY pattern after writing; it does not prove the module is correct.
6. **The contract is not enforced.** Nothing prevents a worker from editing another worker's file;
   the metric only observes it.
7. **Sandbox artifact exposure** (§9) is a real integrity risk left untouched by instruction.

---

## Changed files

Added: `experiments/evaluation/stage_g3_worker_contracts.py`,
`experiments/evaluation/stage_g3_3_analysis.py`, `tests/unit/test_worker_contract.py`,
`tests/integration/test_stage_g3_3_contract_delivery.py`,
`experiments/results/stage-g3-3/g33-20261111T051039Z/` (raw runs + `analysis/`),
`docs/stage-g3-3-report.md`.

Modified: `experiments/evaluation/runner.py`, `experiments/evaluation/stage_e_cli.py`,
`experiments/evaluation/stage_e_rounds.py`, `.gitignore`.

Read-only `git diff --stat` (the working tree also carries the uncommitted G1–G3-2 work; no Git write
operation was performed):

```text
 .gitignore                                 |   6 +
 experiments/evaluation/runner.py           | 214 +++++++-
 experiments/evaluation/stage_e_cli.py      | 123 +++++
 experiments/evaluation/stage_e_rounds.py   | 640 +++++++++++++++++++++++-
 src/atlasos/integrations/anycode_engine.py |  93 +-
 src/atlasos/sandbox/tools.py               |  49 +-
 src/atlasos/tools/conversation.py          |  33 ++
 src/atlasos/tools/dispatch.py              |  44 +-
 src/atlasos/tools/runtime.py               |   8 +-
 tests/integration/test_anycode_dag.py      | 705 ++++++++++++++++++++++++++++-
 10 files changed, 1897 insertions(+), 18 deletions(-)
```

(`src/atlasos/history_compaction.py`, `src/atlasos/tool_result_offload.py` and the G3-1/G3-2/G3-3
test and experiment modules are still untracked and therefore absent from that stat.)
