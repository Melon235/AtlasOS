# Runtime Integration Contract

Design only. **Nothing in this document is implemented.** No file under `src/`, `tests/`,
`experiments/` was modified by Final-2.

Purpose: define the smallest wiring that lets one real software-engineering DAG use the Context Plane
**and** the Tool Runtime, Scoped Workers, trusted execution and trajectory at the same time — without a
new pipeline, a new Context Pack, a new scheduler, or a rewrite of AnyCode.

---

## 1. Current facts (verified against source)

| Fact | Evidence |
|---|---|
| The Context Plane's only model-facing seam is `AtlasContextEngine(prompt_builder=…)`, which overrides `AnyCode._build_task_prompt(task, queue) -> str` | `integrations/anycode_engine.py:61-84` |
| The builder's return value becomes the **task prompt only**; the system message is still `AgentConfig.system_prompt` from `ScopedWorkerFactory` | `runtime.py:985` returns `rendered.messages[-1]["content"]`; `renderer.py:98-101` builds `[system, user]` |
| `TaskScopedSelector` synthesises the TASK block from `AtlasTaskSpec.description` and selects `DEPENDENCY_OUTPUT` blocks whose `task_id` is in `task.depends_on` | `context/selectors.py:40-54` |
| The workflow path projects each completed task into `task-output:<id>` and skips tasks already recovered from a checkpoint | `runtime.py:914-939` |
| The workflow path enforces a recovery invariant: a recovered dependency block must exist before render | `runtime.py:891-902` |
| `app/coding.py` builds `AtlasContextEngine(OrchestratorConfig(), tool_runtime=runtime)` — **no** `prompt_builder` | `app/coding.py:646` |
| `experiments/evaluation/runner.py` builds `AtlasContextEngine(OrchestratorConfig(on_progress=…), …)` — **no** `prompt_builder` | `runner.py:1003-1004` |
| `app/coding.py` does **not** pass a `RunBudgetState`, so the shared budget never covers a coding run | `coding.py:625-639` |
| `runner.py` passes `budget_state` only when `budget_mode="guarded"` | `runner.py:712-714` |
| Dependency outputs reach a benchmark worker through upstream task-result injection, or through `HandoffRuntime` when `handoff_mode="dual"` | `runner.py:866-879`; `handoff.py::HandoffRuntime` |
| `AgentConfig.tools` is set from `tool_runtime.agent_tools`; hooks are appended in a fixed order (compaction → conversation snapshot → budget) | `anycode_engine.py:104-123` |
| Trajectory already has a `message_projection` event type | `trajectory/models.py::MessageProjectionData` |

Consequence: the plane and the execution controls live in different compositions, but the *seam*
needed to join them already exists. No new abstraction is required — only a caller.

---

## 2. Integration target

One real DAG must exhibit all thirteen properties in a single run (evidence in parentheses):

1. real worker construction (`workers.json`) — 2. real task dependencies (`TaskSpec.depends_on`) —
3. prior task output written to `ContextStore` (`task-output:<id>` block) — 4. selector reading that
dependency — 5. stable renderer producing part of the real model input (`context_layout_hash` in
`inference.jsonl`) — 6. progressive tool discovery (`tool_search` event) — 7. dispatch
(`dispatch_action`) — 8. action gate decision — 9. durable ledger row — 10. sandbox execution
(`same_binding_all_project_tools=true`) — 11. shared budget admission — 12. trajectory events —
13. independent oracle verdict.

If a run cannot show all thirteen, the claim "V1 Context Plane participates in the coding runtime" is
not made (see §5).

---

## 3. Minimal wiring design

### 3.1 Answers to the ten required questions

1. **Where to wire.** At the two existing composition points that already construct
   `AtlasContextEngine`: `app/coding.py:646` and `experiments/evaluation/runner.py:1003`. Both pass a
   `prompt_builder` only when `context.policy="task_scoped"`; the default configuration is unchanged.
2. **How prior results enter the store.** Identically to the workflow path: for each task in
   `queue.list()` with `status == "completed"`, put a `ContextBlock(block_id=f"task-output:{id}",
   kind=DEPENDENCY_OUTPUT, task_id=id, version=1, owner_agent_id=assignee)` and keep the matching
   `ContextRef`. No new projection format.
3. **When to select.** Per task, immediately before the model call for that task — the builder is
   called by AnyCode once per task start. Nothing is precomputed at submit time.
4. **Trusted system prompt.** Unchanged and never routed through the renderer's user message.
   `AgentConfig.system_prompt` (from `render_worker_context`) stays the system message. The builder
   passes it to `render(stable_system_prefix=…)` **only** so `stable_prefix_hash` is computed over the
   real prefix; it returns `messages[-1]["content"]` (verified in `runtime.py:985`), so no duplication.
5. **Avoiding double injection.** `prompt_builder` replaces upstream `_build_task_prompt`, so upstream
   dependency injection is bypassed. `handoff_mode="dual"` injects its own projection, so the two
   sources must be mutually exclusive: the composition must raise
   `ValueError("context.policy='task_scoped' and handoff_mode='dual' both project dependencies")` at
   construction. Default stays `handoff_mode="legacy"`.
6. **Tool schema and ExecutionContext.** Untouched. `build_agent` still sets
   `tools=list(tool_runtime.agent_tools)`, the hook order is unchanged, `execution_context` still comes
   from `ScopedWorkerFactory`. The builder changes only the task text.
7. **Rendered-prompt evidence.** The builder keeps the last `RenderedPrompt` per task and the
   composition writes `context_layout_hash`, `stable_prefix_tokens`, `context_block_tokens`,
   `dynamic_tail_tokens` and `transform_name` into the existing `InferenceRecord` fields (workflow path
   already does this) and into a trajectory `message_projection` event. No new metric type.
8. **Missing-context check.** Before render, assert that every declared dependency of the task has a
   block in the store; raise otherwise (same invariant as `runtime.py:891`). A run must fail loudly,
   never silently drop a dependency.
9. **Data-structure changes.** None to `ContextBlock`, `ContextRef`, `RenderedPrompt`,
   `AtlasTaskSpec`, `AgentConfig`, `TaskSpec`. The only new object is a small builder class.
10. **Smallest file set.** See §3.2 — three source files, one new module, two test files.

### 3.2 Proposed file allowlist (for a future, separately approved change)

| File | Change | Why |
|---|---|---|
| `src/atlasos/context/task_prompt.py` (new, ~90 lines) | `AtlasTaskPromptBuilder(store, selector, renderer, transform)` with `__call__(task, queue) -> str`, `last_render(task_id)`, and the dependency-projection helper | one implementation shared by both paths; keeps `runtime.py` from growing |
| `src/atlasos/runtime.py` (≈−40/+10) | `_build_atlas_prompt` delegates to the new builder | behaviour-preserving; existing 12 tests are the guard |
| `src/atlasos/app/coding.py` (≈+25) | build the builder when `context.policy="task_scoped"`; pass `prompt_builder`; optionally pass `budget_state` | the actual integration |
| `experiments/evaluation/runner.py` (≈+25) | same wiring, driven by `RunnerSettings` (`context_policy` field would be new) | makes A1 measurable on real tasks |
| `tests/unit/test_context_task_prompt.py` (new) | 8–12 T1 cases | acceptance |
| `tests/unit/test_coding_attribution.py` / `tests/integration/test_runtime.py` | unchanged, must stay green | regression guard |

Explicitly **not** touched: `context/store.py`, `context/selectors.py`, `context/renderer.py`,
`context/transform.py`, `workers/*`, `actions/*`, `ledger/*`, `sandbox/*`, `tools/*`, `budget.py`,
`handoff.py`, `anycode_engine.py`, `workflow.py`.

### 3.3 Call flow (future state)

```text
run_coding_session / run_sample
  └─ builds store, selector, renderer, transform from AtlasConfig.context
  └─ AtlasContextEngine(OrchestratorConfig(), tool_runtime=runtime,
                        prompt_builder=AtlasTaskPromptBuilder(...),
                        budget_state=RunBudgetState(limit_tokens=…))   ← optional
        └─ AnyCode orchestrator → _build_task_prompt(task, queue)
              ├─ project completed tasks → task-output:<id> blocks
              ├─ assert declared dependencies present
              ├─ selector.select(...) → [task block, dependency blocks]
              ├─ transform.transform(...) (identity | contextpilot_dedup)
              ├─ renderer.render(stable_system_prefix=agent.system_prompt, ...)
              ├─ record layout hash + token splits
              └─ return messages[-1]["content"]        → AnyCode sends it as the task prompt
```

### 3.4 Required regression tests (to be authored with the change)

| Test | Assertion |
|---|---|
| `test_builder_matches_workflow_projection` | same DAG + same store ⇒ the builder's body equals `runtime._build_atlas_prompt`'s body |
| `test_dependency_block_required` | a completed dependency without a block raises instead of rendering |
| `test_no_double_injection_with_handoff` | constructing with `task_scoped` + `handoff_mode="dual"` raises |
| `test_system_prompt_not_duplicated` | rendered task text does not contain the worker system prompt |
| `test_layout_independent_of_completion_order` | two completion orders ⇒ identical `context_layout_hash` |
| `test_task_text_preserved` | the model-visible task text equals `TaskSpec.description` byte-for-byte |
| `test_hooks_and_tools_unchanged` | `AgentConfig.tools` and hook order identical to the pre-change path |
| `test_unrelated_worker_context_invisible` | a block owned by another task is never selected |

### 3.5 Known risks

| Risk | Mitigation |
|---|---|
| Moving projection out of `runtime.py` regresses workflow behaviour | the delegate must be a pure refactor; the existing `test_runtime.py` / `test_prompt_renderer.py` / `test_context_runtime.py` suites are the gate |
| The builder adds a second source of dependency text | mutual-exclusion guard with `handoff_mode`; one projection function |
| Token estimates in `RunBudgetHook` drift from the rendered text | budget hook already serialises system + tools + conversation; verify with a test that the estimate is within the historical tolerance |
| Evaluation numbers change because the prompt changes | the integration is off by default; any comparison run must use the same `context.policy` on both arms |
| Silent context loss (a needed fact in a non-dependency block) | the invariant raises; the full-context baseline remains available |

### 3.6 `INTEGRATION_DESIGN_BLOCKED`?

**No.** The design reuses the existing seam, the existing selector/renderer/store, and the existing
worker/tool composition. It does not require a new Agent Loop, Scheduler, ContextStore, tool protocol
or Runner rewrite. If implementation later shows that `AnyCode._build_task_prompt` is called more than
once per task (or after the conversation has started), the design must stop and be re-reviewed — that
is the one condition that would turn this into a redesign.

---

## 4. Integration acceptance gates

A future integration is accepted only if all of these hold:

1. Same dependency set, different completion order ⇒ identical `context_layout_hash` and identical
   rendered body ordering.
2. Every declared dependency block is present; the missing-block invariant raises.
3. A block owned by another task is never rendered for the current task.
4. `AgentConfig.system_prompt` and `ExecutionContext` are byte-identical to the pre-change run.
5. `tool_search`/`dispatch_action` behaviour, `tool_not_disclosed` and `tool_unavailable` unchanged.
6. Gate, ledger and sandbox semantics unchanged (existing T1 suites green).
7. Budget admission covers every worker in the run (assert one shared `RunBudgetState` id).
8. No new failures in the existing offline suite.
9. At least one real development-set coding DAG run shows all thirteen properties in §2 with
   file-level evidence.

Only then may documentation state that the V1 Context Plane participates in the coding runtime. If the
integration cannot be completed within this scope, the fallback is explicit: validate the Context
Plane (workflow path) and the coding runtime separately, and **downgrade the architecture claim** —
no invented "unified runtime".

---

## 5. Recovery scope (explicitly excluded)

This contract does not add ordinary coding crash recovery. In scope for module validation:
workflow checkpoint resume, durable context rebuild, durable suspension, persistent approval, verified
reattachment, ledger `UNCERTAIN` classification. Out of scope: a resume engine, a resume CLI, cross-
process budget recovery, distributed recovery coordination, and any change to `OrchestratorConfig`
inside the coding compositions.

Enabling `CheckpointConfig` in a coding composition would create checkpoint files but would still not
provide recovery: no resume entry, no manifest, no ledger reconciliation at start-up. It must not be
presented as crash recovery.

---

## 6. Metric-semantics fixes (candidates, not implemented)

Four narrow candidates. Each is a separate, owner-approved change; none is part of the integration.

| ID | Problem | Location | Candidate change | Acceptance |
|---|---|---|---|---|
| M-1 | `MODEL_UNFINISHED` can mask `SANDBOX_PROVIDER_FAILURE` (assignment runs before the `finally` guard) | `src/atlasos/app/coding.py:687-724` | keep both facts: add `failure_secondary`, and evaluate the sandbox-destroyed condition before agent classification overwrites the class | compound-failure test: agent failed + destroy unconfirmed ⇒ primary class preserved, secondary recorded |
| M-2 | `session["prompt_tokens"]` is `TokenUsage.input_tokens` (cache **miss**), not total prompt | `src/atlasos/app/coding.py:687-700` | emit `prompt_tokens_total = input_tokens + cache_read_input_tokens` and keep the miss value under an explicit name | a metric test asserting `total = miss + hit` against a recorded DeepSeek payload |
| M-3 | `patch_changed_files` counts `git status --porcelain` lines, not files | `src/atlasos/sandbox/export.py` | parse porcelain entries (or count diff headers in `patch_text`) | unit test with a 2-file patch ⇒ `changed_files == 2` |
| M-4 | App coding has no `input_digest_coverage`; evaluation does | `src/atlasos/app/coding.py`, `trajectory/reader.py` | compute the same metric from the recorded trajectory at session end | app session reports the metric for a scripted run |

Rules: no missing value may be coerced to `0`; no application data model is rewritten; if a fix would
change an existing consumer, it is out of scope for this round.
