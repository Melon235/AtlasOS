# Stage 2 — Inspect AI Integration

## Provenance

- Commit: `780a365` (`feat: add Inspect AI evaluation layer and 30-task agent benchmark`).
- Inspect AI version: `0.3.287` (installed in the project venv, recorded by `inspect-ai` metadata).
- Official sources checked before implementation:
  - <https://inspect.aisi.org.uk/>
  - <https://github.com/UKGovernmentBEIS/inspect_ai>
  - <https://pypi.org/project/inspect-ai/>
- AtlasOS runtime revision under test: `3fbf3dd` plus Stage 1 fix `1f2517d`.

## Design

```text
Inspect AI
  Dataset (30 memory samples, stable ids)
      |
      v
  @solver atlas_os_solver
      |
      v
  experiments/evaluation/runner.py
      |  (composes, does not reimplement)
      +-- AtlasContextEngine (AnyCode engine)
      +-- AtlasToolRuntime (M1/M3/M4)
      +-- ProjectSandboxManager (M5 / OpenSandbox)
      +-- SQLite suspension/approval/ledger/binding stores (M4/M5/M6 components)
      +-- TrajectoryRecorder (M7)
      +-- Real DeepSeek adapter (AtlasOS integration)
      |
      v
  exported workspace + durable stores + M7 trajectory
      |
      v
  independent Host oracle (hidden tests, scope checks, tool/worker contracts)
      |
      v
  @scorer atlas_oracle_scorer -> Inspect .eval log
```

The custom solver deliberately **does not call Inspect's `generate`**. Inspect's
`deepseek/deepseek-chat` model is recorded as provenance only; all inference goes through the
existing AtlasOS `DeepSeekAdapter`, and the adapter records real provider usage, cache fields, and
latency. This prevents Inspect's model configuration from bypassing AtlasOS's internal inference
or tool runtime.

## Identity and isolation

Every sample execution records:

- `benchmark_id`, `dataset_version`, `scorer_version`;
- `sample_id`, `repetition_id`, unique `run_id`;
- Git commit and dirty flag at run start;
- `config_digest` (evaluation settings + pinned YAML config);
- `fixture_digest` (visible + hidden fixture content and worker graph);
- requested model and provider-resolved model names;
- sandbox provider/image/endpoint/health and capability digest.

Each run gets a new directory containing a fresh fixture, sandbox binding, ownership root,
idempotency/ledger/binding stores, worker records, M7 trajectory, exported workspace, patch, oracle
log, and `result.json`. No run shares a sandbox or execution state with another sample.

## Persistence

- `<round>/runs/<benchmark>/<sample>/r<rep>-<run>/result.json` — full machine-readable result.
- `<round>/runs/<benchmark>/<sample>/r<rep>-<run>/run.json` — identity, task metadata, environment.
- `<round>/runs/<benchmark>/runs.jsonl` — append-only run ledger.
- `<round>/inspect-logs/*.eval` — Inspect AI logs with sample scores and metadata.
- `<round>/manifest.json` — round provenance, task list, config digest, pass/fail summary.

## Implementation issues found and fixed during integration

1. **Inspect `TaskState.store` is a read-only `Store` object**, not a dict property. Initial
   assignment raised `AttributeError` and Inspect recorded the sample as an error while still
   reporting eval status `success`. Fixed with `state.store.set("atlas_result", payload)` and the
   CLI now requires one score per sample and all scores to pass.
2. **Sandbox metadata label length.** A full benchmark/sample/repetition run id exceeded the
   OpenSandbox 63-character label limit. The durable identity is now a short `eval-<12 hex>` run id
   while sample/repetition/config/fixture digests remain in the persisted record.
3. **MCP client cleanup.** The AnyCode MCP client entered a transport context but the evaluation
   adapter did not close the Atlas tool runtime, producing an async-generator teardown error and a
   process exit code of 1 even for successful runs. The adapter now closes the Atlas tool runtime
   explicitly; tool-discovery smoke runs exit 0.
4. **Silent sample errors.** Inspect can return `status=success` with a sample-level error and no
   score. The evaluation CLI now fails the invocation unless every sample is scored and every score
   passes.
5. **Multi-worker artifact delivery.** A worker's self-reported run status can be failed while its
   artifact is correct and consumed by the next worker. The oracle now verifies distinct workers
   and artifact delivery through hidden tests; worker self-reported success remains a separate
   behavioral metric.

## Verification

```powershell
python -m pytest tests/unit/test_evaluation.py -q
# 7 passed

python -m pytest -q
# 443 passed

python -m ruff check src tests experiments/evaluation
# All checks passed!

python -m pyright
# 0 errors, 0 warnings
```

End-to-end Inspect execution was verified with real DeepSeek and OpenSandbox:

```powershell
python -m experiments.evaluation.cli inspect `
  --sample bugfix-002-median `
  --runs-root experiments/runs/evaluation-inspect-smoke `
  --log-dir experiments/results/evaluation/inspect-smoke-r1
```

The resulting `.eval` log contained one scored sample (`atlas_oracle_scorer` value `1.0`),
`failure_class=TASK_SUCCESS`, real cache-hit/miss token fields, and a recorded commit/config/fixture
digest. The first smoke round scored 4/5; the failure was a task contradiction in
`bugfix-005-date-range` (the public test asserted the old exclusive-end behavior while the prompt
required inclusive behavior), not an oracle change. The public test was made consistent and the
sample then passed; the final smoke/pilot evidence is recorded with the formal results.

## Files added

- `experiments/evaluation/inspect_adapter.py`
- `experiments/evaluation/runner.py`
- `experiments/evaluation/rounds.py`
- `experiments/evaluation/cli.py`
- `experiments/configs/eval-benchmark.yaml`
- `tests/unit/test_evaluation.py`

## Boundary

Inspect AI is an external harness. It receives no ability to approve actions, mutate M3 policy,
write M4/M6 state, or override M5 ownership. AtlasOS remains the execution and approval authority.
