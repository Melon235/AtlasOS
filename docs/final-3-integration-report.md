# Final-3 — Minimal Runtime Integration Report

Date: 2026-11-13. Branch: `chore/final-1-5-repository-baseline`.
`BASE_SHA` = `06ae521b615e989da35b548b3ce3d58cec17b36c`.
`INTEGRATED_SHA` = `93cbd98f9407e7358e997878238520dcac15827a` (runtime frozen here).
Acceptance work and evidence live at `97fa2d4`; the report commit is on top.

This document reports what was implemented and measured. It does not restate the design.

---

## 1. What was integrated

| Item | Result | Commit |
|---|---|---|
| R-4 Context Plane in the coding compositions | **implemented** | `2c31b0b` |
| R-5 shared run budget in the application coding path | **implemented (opt-in)** | `2c31b0b` |
| R-6 failure-attribution precedence | **implemented** | `2c31b0b` |
| R-7 prompt/cache token semantics | **implemented** | `2c31b0b` |
| R-10 neutral sandbox environment for native AnyCode | **capability map only; environment BLOCKED** | `93cbd98` |
| R-1 semantic tool search / FAISS coverage | **implemented, real embeddings** | `93cbd98` |
| R-2 MCP stdio coverage | **already existed; now executed** (mcp extra installed) | — |
| R-3 Inspect AI adapter coverage | **implemented** | `93cbd98` |
| R-8 / R-9 | not implemented (deferred by decision) | — |

Files changed (source): `src/atlasos/context/task_prompt.py` (new), `src/atlasos/runtime.py`,
`src/atlasos/app/coding.py`, `src/atlasos/app/service.py`, `experiments/evaluation/runner.py`,
`experiments/evaluation/neutral_env.py` (new).
Tests added: `tests/unit/test_final3_task_prompt.py` (7),
`tests/unit/test_final3_coding_metrics.py` (10), `tests/integration/test_final3_context_dag.py` (1),
`tests/integration/test_final3_semantic_search.py` (6),
`tests/integration/test_final3_inspect_adapter.py` (6),
`tests/integration/test_final3_neutral_env.py` (4).

---

## 2. R-4 — Context Plane wiring

### 2.1 Actual call chain

```text
run_coding_session / runner.run_sample
  └─ AtlasContextEngine(OrchestratorConfig(), tool_runtime=runtime,
                        prompt_builder=AtlasTaskPromptBuilder(...))     ← only when
                                                                        context.policy == "task_scoped"
        └─ AnyCode orchestrator → _build_task_prompt(task, queue)
              └─ AtlasTaskPromptBuilder.__call__
                    ├─ _assert_recovered_dependencies   (resume invariant)
                    ├─ _project                         (completed task → task-output:<id> block)
                    ├─ TaskScopedSelector.select        (task block + dependency blocks)
                    ├─ ContextTransform.transform       (identity by default)
                    ├─ PromptRenderer.render            (stable order, layout hash)
                    └─ return messages[-1]["content"]   → the task prompt AnyCode sends
```

`AtlasRuntime._build_atlas_prompt` now delegates to the same builder and keeps its evidence capture,
so the workflow path and the coding paths render through one implementation. The workflow behaviour is
unchanged: `tests/integration/test_context_runtime.py`, `test_resume.py`, `test_runtime.py` and
`tests/unit/test_prompt_renderer.py` (43 tests) pass without modification.

### 2.2 Evidence that the model really received the context

`tests/integration/test_final3_context_dag.py` runs two dependent tasks through the real engine and a
recording fake provider, then asserts on the captured request:

- task A's prompt contains no `<dependency` marker;
- task B's prompt contains `<dependency task_id="a" ...>` and A's output **exactly once**;
- the system prompt travels in `LLMChatOptions.system_prompt` and is not duplicated in the task body;
- two renders are recorded, `b.rendered_dependency_order == ["a"]`, and the layout hashes differ.

That is a real prompt seam, not a renderer unit test.

### 2.3 Trust boundary

Dependency output is task data. The renderer puts it in the **user** message; the worker's
`AgentConfig.system_prompt` and `ExecutionContext` are untouched, and the gate still decides every
action. The prompt builder never reads identity from text. Selection is not an access-control
boundary: excluding a block does not prevent a tool from reading that file — the gate and sandbox do.

### 2.4 Double-injection guard

`handoff_mode="dual"` and `context.policy="task_scoped"` both project dependency outputs. The
evaluation runner now refuses the combination:

```
EvaluationEnvironmentError: context.policy='task_scoped' and handoff_mode='dual' both project
dependency outputs into the task prompt; enable exactly one of them
```

The default (`upstream_default` + `handoff` builder) is unchanged.

---

## 3. R-5 — shared budget in application coding

- `CodingRunOptions.token_budget` plus the explicit switch `ATLASOS_CODING_TOKEN_BUDGET`.
- When set, the session builds **one** `RunBudgetState` and passes it to `AtlasContextEngine`, so the
  existing `RunBudgetHook` admits every request against the same run budget. Unset ⇒ no budget, no
  behaviour change.
- Evidence: `tests/unit/test_final3_coding_metrics.py` covers absent/unset switch, run-scoped limit,
  admission rejection (`RunBudgetExhausted` + `admission_rejections == 1`) and the evidence block.
- The session records `budget: {limit_tokens, used_*, remaining_tokens, requests_*, admission_rejections,
  stop_reason}` so actual consumption can be compared with the estimate. The estimate is **not** an
  upper bound and the lock is process-local — unchanged limitations.

---

## 4. R-6 / R-7 — attribution and token semantics

`_merge_failures(agent_failure, cleanup_failure)` makes a resource failure the primary class and keeps
the agent outcome as `failure_secondary_*`. Four compound cases are tested: agent-only, cleanup-only,
both, neither. `status` is still `completed` **only** when `failure is None and agent_success`, so no
failed run becomes a success.

`_token_evidence(usage)` records, explicitly:

| Field | Meaning |
|---|---|
| `prompt_tokens` | **cache-miss input tokens** (historical meaning preserved for existing consumers) |
| `cache_miss_tokens` | same value under an unambiguous name |
| `cache_hit_tokens` | `cache_read_input_tokens` |
| `total_prompt_tokens` | miss + hit — comparable with benchmark `prompt_tokens` |
| `completion_tokens` | output tokens |

`app/service.py` now reports `prompt_tokens = total_prompt_tokens` (plus the cache split) for coding
runs instead of summing tool-call token fields. Missing values stay `None`.

---

## 5. R-10 — native neutral environment

Implemented: `experiments/evaluation/neutral_env.py` — a capability map asserting that the six tools
the native baseline compiles with (`bash`, `file_read`, `file_write`, `file_edit`, `grep`,
`list_files`) exist on both sides, with the limitation of each pair stated, plus the environment
fields that must match before any cost comparison.

Not implemented: the neutral sandbox lifecycle layer. Reusing `ProjectSandboxManager` would place one
of the mechanisms under comparison (binding/lifecycle) inside the baseline arm, and a second
lifecycle implementation is out of this stage's scope. Therefore:

**Status: `BASELINE_PARITY_BLOCKED`.** Tool capability is 1:1; execution location, shell and setup
cost still differ. Track B may report quality and token/cost with the confound disclosed, and must
not report latency or setup overhead as a runtime difference.

---

## 6. Coverage added

| Module | Test | What it proves |
|---|---|---|
| A2-04 / A2-05 | `test_final3_semantic_search.py` | real MiniLM (384-dim) + FAISS over a 24-tool catalog; Top-K ids; paraphrase query; unknown query; index rebuild; `tool_search` → disclosure → `dispatch` |
| A2-10 | `test_tool_mcp.py` (existing, now executed) | real stdio MCP server: discovery, hidden by default, search, dispatch, cleanup |
| A5-10 | `test_final3_inspect_adapter.py` | dataset/solver/scorer wiring, success→1.0, failure→0.0 with class, missing result→0.0, settings round-trip |
| A4-01..18, A5-01..11 | existing suites | unchanged, re-executed in Gate 1 |

---

## 7. Regression

| Check | Result |
|---|---|
| `pytest -rs` (Gate 1, `INTEGRATED_SHA`) | **925 passed, 6 skipped, 1 deselected in 241.57 s** |
| `ruff check .` | pass |
| `ruff format --check .` | pass (364 files) |
| `pyright` | 0 errors / 0 warnings |
| `atlas --help`, `experiments.evaluation.cli list` | exit 0, 30 tasks |
| Skips | 4 × contextpilot (E1 policy), 1 × live sandbox, 1 × raw-run fixture |

The 34 new tests are the only additions; no existing assertion was relaxed.

---

## 8. What is still not integrated

1. **Coding crash recovery** — out of scope (D4). No checkpoint is enabled anywhere in the coding path.
2. **X0/X1 live comparison** — the context plane is wired and unit-proven, but no live model run was
   executed (no provider credential in this workspace).
3. **R-8 / R-9** — `patch_changed_files` and app `input_digest_coverage` remain deferred.
4. **Handoff + context plane together** — refused by design; one projection only.
5. **Native-in-sandbox** — blocked as described in §5.

---

## 9. Design limitations to carry forward

- The builder is only active with `context.policy="task_scoped"`; every other configuration renders
  exactly as before.
- Single-task coding runs get determinism and hash evidence, not dependency selection (there is no
  dependency). Multi-task evidence comes from the evaluation DAG.
- `context.policy="task_scoped"` changes the model-visible prompt text (`Task: <title>` header), which
  is why X0/X1 must be compared as two configurations of the same task, never across task sets.
