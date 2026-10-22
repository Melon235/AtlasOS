# Stage 0/1 Specification — Repository Bootstrap and Runtime Baseline

## Overview

This document defines the first two implementation stages of AtlasOS V1.

The goal is to reach a clean, reproducible baseline before implementing any cache-aware optimization.

After Stage 1, the repository must be able to run a small multi-agent workload on the selected runtime base, call DeepSeek through a typed adapter, and record provider cache metrics — but it must **not yet** perform AtlasOS context optimization or cache-aware task scheduling.

---

# Stage 0 — Repository Bootstrap and Upstream Verification

## 0.1 Objective

Create a minimal AtlasOS repository with:
- architecture documents,
- reproducible Python environment,
- explicit upstream dependency/provenance records,
- a verified AnyCode integration path,
- a verified ContextPilot installation path,
- a verified DeepSeek adapter spike,
- deterministic fake-provider tests.

No research optimization is implemented in Stage 0.

---

## 0.2 Questions Stage 0 Must Answer

Before implementation proceeds, verify:

1. Which AnyCode version is pinned?
2. Which extension point will AtlasOS use for:
   - LLM adapter,
   - scheduler policy,
   - context policy/AgentRunner hook,
   - checkpoint metadata?
3. Can ContextPilot be used as a normal Python dependency in the chosen environment?
4. Can a minimal `optimize()` call operate on text context blocks without self-hosted vLLM/SGLang?
5. Can DeepSeek be called through an OpenAI-compatible/custom AnyCode `LLMAdapter`?
6. Does the DeepSeek response expose:
   - `prompt_cache_hit_tokens`,
   - `prompt_cache_miss_tokens`?
7. Can live DeepSeek tests be made opt-in so normal tests are offline and deterministic?

Do not write permanent architecture around assumptions that have not been verified.

---

## 0.3 Repository Layout

Start with:

```text
atlasos/
├── AGENTS.md
├── README.md
├── pyproject.toml
├── uv.lock
├── .env.example
├── THIRD_PARTY.md
│
├── docs/
│   ├── architecture.md
│   ├── decisions/
│   └── stages/
│       └── 00-01-bootstrap-and-baseline.md
│
├── src/
│   └── atlasos/
│       ├── __init__.py
│       ├── integrations/
│       │   ├── anycode_runtime.py
│       │   ├── deepseek.py
│       │   └── contextpilot.py
│       ├── context/
│       ├── scheduler/
│       ├── metrics/
│       └── config.py
│
├── tests/
│   ├── unit/
│   └── integration/
│
├── examples/
│   └── baseline_team.py
│
└── experiments/
    └── configs/
```

`context/` and `scheduler/` may initially contain only package stubs.

Do not prebuild abstractions before Stage 1 needs them.

---

## 0.4 Dependency Strategy

### Required
- Python >= 3.12
- AnyCode pinned to a known version
- Pydantic if not already transitively available
- pytest
- ruff
- pyright or equivalent type checker used consistently
- HTTP/OpenAI-compatible client needed by the chosen DeepSeek adapter

### ContextPilot
Install/pin the official package/repository version.

Do not vendor its implementation into AtlasOS.

### Optional extras
Do not install all AnyCode optional extras by default.
Install only what the baseline requires.

MCP, sandbox, Redis, Chroma, telemetry exporters are not Stage 0 requirements.

---

## 0.5 Provenance File

Create `THIRD_PARTY.md` recording at least:

```text
AnyCode
- repository
- pinned version/commit
- license: MIT
- AtlasOS usage: runtime/orchestration base

ContextPilot
- repository
- pinned version/commit
- license: Apache-2.0
- AtlasOS usage: context reorder/dedup/cache-aware optimization backend

DeepSeek
- API service
- AtlasOS usage: inference backend and provider-reported cache metrics
```

Do not wait until release to add provenance.

---

## 0.6 AnyCode Capability Spike

Create a temporary or test-only spike proving:

- construct two or more agents,
- define at least two tasks with a dependency,
- run through the existing scheduler,
- receive a result,
- inspect task lifecycle/result metadata.

Use a deterministic fake LLM adapter if available.

This spike exists to identify real extension points.

Do not customize scheduling yet.

### Stage 0 acceptance
A test or script demonstrates:

```text
Task A ----\
           -> Task C
Task B ----/
```

and verifies C runs only after dependencies succeed.

---

## 0.7 DeepSeek Adapter Spike

Implement the smallest adapter required to call DeepSeek through the runtime's provider boundary.

The adapter must normalize usage into an AtlasOS metrics object.

Suggested normalized model:

```python
class InferenceUsage:
    prompt_tokens: int
    completion_tokens: int
    cache_hit_tokens: int | None
    cache_miss_tokens: int | None
    latency_ms: float
```

Derived property:

```python
cache_hit_ratio =
    cache_hit_tokens / (cache_hit_tokens + cache_miss_tokens)
```

Return `None` when provider fields are unavailable.

Never infer/fabricate cache metrics.

### Live test
Guard with environment variable, for example:

```text
ATLASOS_RUN_LIVE_TESTS=1
DEEPSEEK_API_KEY=...
```

Default test suite must not make network calls.

---

## 0.8 ContextPilot Spike

Verify the official package can:
- accept a set/list of text contexts,
- optimize them for a query,
- return a prompt/messages suitable for an OpenAI-compatible inference request.

The Stage 0 spike is not yet integrated into AnyCode.

Record:
- exact API used,
- whether proxy mode or in-process mode is preferred,
- any behavior specific to cloud APIs,
- limitations of cache-index synchronization when the provider is remote.

Preferred V1 approach:
- in-process Python adapter if practical,
- proxy only if required by official integration.

Do not build custom reorder/dedup logic.

---

## 0.9 Architecture Decision Records

Create ADRs only for decisions that are actually settled.

Recommended initial ADRs:

```text
0001-use-anycode-as-runtime-base.md
0002-contextpilot-as-external-backend.md
0003-deepseek-cache-metrics-are-performance-hints.md
0004-langgraph-not-in-v1-core.md
```

Each ADR should contain:
- decision,
- context,
- alternatives,
- consequences.

Keep each short.

---

## 0.10 Stage 0 Tests

Required:
- dependency import smoke test,
- deterministic AnyCode DAG test,
- DeepSeek usage parser unit test with fixture JSON,
- ContextPilot adapter smoke test where possible without live LLM,
- configuration validation test.

Recommended commands:

```bash
uv run ruff check .
uv run pyright
uv run pytest -q
```

Adapt commands only if repository tooling differs.

---

## 0.11 Stage 0 Exit Criteria

Stage 0 is complete when:

- [ ] repository skeleton exists,
- [ ] dependencies are pinned,
- [ ] upstream licenses/provenance are recorded,
- [ ] AnyCode DAG baseline works offline,
- [ ] DeepSeek usage parsing is tested,
- [ ] optional live DeepSeek call works,
- [ ] ContextPilot official package/API has been verified,
- [ ] no AtlasOS optimization has been implemented yet,
- [ ] `ruff`, type check, and tests pass.

Do not begin Stage 1 until these are satisfied.

---

# Stage 1 — Runtime Baseline

## 1.1 Objective

Build the first real AtlasOS baseline using the existing AnyCode runtime.

The Stage 1 baseline must execute a small multi-agent task graph and collect metrics without performing:
- context selection optimization,
- ContextPilot reordering,
- cache-aware scheduling,
- custom compaction.

This baseline becomes the reference for every later ablation.

---

## 1.2 Baseline Workload

Use a small but non-trivial workflow.

Recommended:

```text
Planner
  |
  +--> Research Task A -->\
  |                        \
  +--> Research Task B -----> Review/Compare --> Final Response
```

Alternative roles may include Coder/Reviewer if tools are easier to validate.

Constraints:
- at least two parallel-ready tasks,
- at least one dependent merge task,
- at least two distinct agent roles,
- bounded task count,
- deterministic fake-provider version for tests,
- live DeepSeek version for metrics experiments.

Do not make the workload itself the project.

---

## 1.3 AtlasOS Runtime Facade

Create a thin AtlasOS entry point.

Example conceptual API:

```python
runtime = AtlasRuntime(config)

result = await runtime.run(
    workflow=workflow,
    input=user_request,
)
```

`AtlasRuntime` should coordinate existing AnyCode components.
It must not duplicate the AnyCode scheduler/task queue internally.

Responsibilities:
- construct/configure upstream runtime,
- install provider adapter,
- install metrics hooks,
- expose experiment configuration,
- capture run metadata.

Non-responsibilities:
- implement its own DAG executor,
- implement its own agent lifecycle,
- implement cache optimization.

---

## 1.4 Baseline Scheduler

Use upstream/default AnyCode task scheduling.

Expose policy name as:

```text
scheduler_policy = "upstream_default"
```

Record assignment events if available.

Do not modify task-to-agent assignment in Stage 1 unless required only to make the baseline runnable.

---

## 1.5 Baseline Context Behavior

Use the simplest upstream/full task context that preserves normal behavior.

The baseline must not use ContextPilot.

Name it explicitly, for example:

```text
context_policy = "upstream_default"
contextpilot_enabled = false
```

The purpose is reproducibility.

If AnyCode already performs hidden summarization/pruning:
- document it,
- disable it if the public API permits,
- otherwise record it as part of the baseline.

Do not pretend a framework-default context policy is "full history" unless verified.

---

## 1.6 Metrics Hook

Every LLM call should emit a normalized inference record.

Suggested:

```python
class InferenceRecord:
    session_id: str
    task_id: str | None
    agent_id: str | None

    model: str

    prompt_tokens: int
    completion_tokens: int

    cache_hit_tokens: int | None
    cache_miss_tokens: int | None
    cache_hit_ratio: float | None

    latency_ms: float

    scheduler_policy: str
    context_policy: str
    contextpilot_enabled: bool
```

At run level aggregate:

```text
total_prompt_tokens
total_completion_tokens
total_cache_hit_tokens
total_cache_miss_tokens
weighted_cache_hit_ratio
total_latency
task_success
```

Do not average per-request cache-hit ratios without also reporting token-weighted totals.

---

## 1.7 Estimated Cost

Cost estimation must be configuration-driven.

Do not hard-code current provider pricing into core code.

Example:

```yaml
pricing:
  provider: deepseek
  cache_hit_input_per_million: ...
  cache_miss_input_per_million: ...
  output_per_million: ...
```

Experiment metadata must record the pricing snapshot/date used.

If pricing is absent:
- cost field is null,
- all token metrics still work.

---

## 1.8 Experiment Identity

Every run gets an experiment/run identifier stored outside the stable prompt prefix.

Required metadata:

```text
run_id
timestamp
git_commit
model
scheduler_policy
context_policy
contextpilot_enabled
configuration_hash
```

Do not inject this metadata into prompts.

---

## 1.9 Offline Baseline Tests

Implement deterministic tests using fake model responses.

Test:
1. task dependency order,
2. two parallel-ready tasks can execute,
3. metrics records are emitted,
4. missing cache fields are handled,
5. failed task is reflected in run result,
6. runtime facade does not alter upstream task semantics.

---

## 1.10 Live DeepSeek Baseline

Provide an opt-in example/experiment.

Purpose:
- validate real token accounting,
- establish baseline cache-hit behavior,
- produce first raw experiment record.

Run the same workload at least twice with an intentionally stable prompt prefix.

Expected observation:
- later calls may show provider-reported cache hits,
- but no specific hit ratio is guaranteed.

DeepSeek cache is best-effort, so tests must not assert a positive hit count.

---

## 1.11 Stage 1 Deliverables

Source:
```text
src/atlasos/runtime.py
src/atlasos/integrations/deepseek.py
src/atlasos/integrations/anycode_runtime.py
src/atlasos/metrics/models.py
src/atlasos/metrics/collector.py
```

Exact filenames may differ after inspecting upstream APIs, but responsibilities must remain clear.

Examples:
```text
examples/baseline_team.py
```

Experiments:
```text
experiments/configs/baseline.yaml
```

Tests:
```text
tests/unit/test_deepseek_usage.py
tests/integration/test_baseline_runtime.py
```

Docs:
- update ADRs only if implementation forced a decision change.

---

## 1.12 Stage 1 Exit Criteria

Stage 1 is complete when:

- [ ] multi-agent DAG executes through AnyCode,
- [ ] AtlasRuntime is a thin facade, not a second orchestration engine,
- [ ] DeepSeek adapter works,
- [ ] offline tests use deterministic fake inference,
- [ ] live DeepSeek experiment is opt-in,
- [ ] hit/miss token metrics are collected when exposed,
- [ ] baseline context policy is documented,
- [ ] ContextPilot remains disabled,
- [ ] no custom Atlas scheduler exists yet,
- [ ] results can be saved as machine-readable JSON/JSONL,
- [ ] lint/type/tests pass.

At this point freeze a baseline result before Stage 2.

---

# After Stage 1

Do not implement everything at once.

Recommended next stages:

```text
Stage 2: ContextBlock / ContextStore / Selector / Stable Renderer
Stage 3: ContextPilot official integration + ablations
Stage 4: Capability / Context / Cache-aware task scheduler
Stage 5: Checkpoint integration + recovery experiment
Stage 6: Full benchmark and report
```

The implementation plan for later stages must be written only after the Stage 1 baseline is running and real extension points are known.
