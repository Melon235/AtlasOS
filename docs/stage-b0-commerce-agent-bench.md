# Stage B0 — Commerce Agent Bench Integration (third-party native benchmark)

**Project:** AtlasRuntime (repository still `Melon235/AtlasOS`, package `atlasos`).
**Benchmark:** [Commerce Agent Bench](https://github.com/Accio-org/CommerceAgentBench) by the Accio
team at Alibaba International — release `v1.3.1`, commit
`d38bd1711aa3926653452458a1be02c1efd12930`.
**Scope:** bring AtlasRuntime onto the native benchmark environment, tools and grader. No agent
capability, scheduler, budget or prompt change.

**Answer in one paragraph.** The benchmark is real, the pinning is real, and the adapter is
implemented and tested — but **the native environment could not be started on this host**, so no
business run exists and this stage answers none of the business-performance questions. The pinned
container image (`acciolyk/accio_bench@sha256:1e9cf5…`) downloads every layer and then never
finalises: two pull attempts produced zero `Pull complete` lines and zero registered images while
`docker manifest inspect` on the same digest times out against `registry-1.docker.io` (`context
deadline exceeded`) even though `docker pull alpine:3.20` succeeds. What *is* verified: the upstream
audit and per-task digests, the adapter's environment staging and teardown, the hidden-material
boundary, and the **native grader executed end-to-end on the host** (`grader/run.sh` → `reward.json`
→ 7 checks parsed, 0 passed, which is exactly right when the run-time audit artifact is absent).
A0/A1 comparisons, token/cost/latency numbers and every "does multi-worker help" claim are therefore
**NOT_RUN / INCONCLUSIVE**, and this report does not invent them.

---

## 1. Upstream version and provenance

| Item | Pinned value |
|---|---|
| Repository | `https://github.com/Accio-org/CommerceAgentBench` |
| Commit | `d38bd1711aa3926653452458a1be02c1efd12930` |
| Release | `v1.3.1` (`pyproject.toml` version, README badge) |
| Task collection | `datasets_domain_v1/domain_v1_text_only.collection.json` (65 tasks) |
| Container image | `acciolyk/accio_bench@sha256:1e9cf5c72a56794175b7d06ece036b92e296e6b7e9e9a7fa244026f6acea3859` |
| Code license | Apache-2.0 |
| Task data license | CC BY 4.0 (`LICENSE-DATA`) |

The checkout is **not vendored**. `CAB_UPSTREAM` points at a local copy; `verify_commit()` accepts it
only if it is a Git checkout at the pinned commit, or a content-verified export whose per-task
digests match `experiments/business_benchmarks/commerce_agent_bench/manifest.json`. Attribution is
recorded in `THIRD_PARTY.md`.

Evidence: `experiments/evidence/stage-b0/upstream_audit.json`.

## 2. The five native tasks

Selected from the official text-only collection; IDs are the upstream IDs.

| Task | Upstream dir | Lang | Agent budget | Verifier | Mock surface |
|---|---|---|---|---|---|
| `api-slack-supplier-quote-thread-consolidation` | `api/slack/…` | zh-CN | 1800 s / 60 actions | script, 180 s | task-local mock API on 9110 |
| `api-gmail-invoice-hold-mcp` | `api/gmail/invoice-hold-mcp` | zh-CN | 1800 s / 90 | script, 120 s | `runtime_mocks.gmail_mock` :3071 (MCP mode) |
| `cli-stripe-invoice-reconciliation` | `cli/stripe/invoice-reconciliation` | en | 3600 s / 260 | script, 180 s | `runtime_mocks.stripe_cli` :3000 |
| `cli-xfer-customs-freight-anomaly-trace` | `cli/xfer/customs-freight-anomaly-trace` | zh | 3600 s / 280 | script, 180 s | `dws_doc_cli` :3400, `stripe_cli` :3000, `box_cli` :3500 |
| `cli-xfer-customs-procurement-control-tower` | `cli/xfer/customs-procurement-control-tower` | zh | 3600 s / 280 | script, 180 s | `google_workspace_cli` :3081, `box_cli` :3500, `jira_cli` :3100 |

All five: `requires_network=false`, `requires_browser=false`, `requires_vision=false`,
`mock_services=true`, one pinned base image. Four ship a task-local `services/start_services.sh`;
`api-gmail-invoice-hold-mcp` does **not** — it relies on the upstream harness's baked mock-runtime
machinery, which is a real integration gap recorded in §6.

## 3. Adapter architecture (source audit → design)

Upstream lifecycle, read from the pinned sources:

```text
bench_core/cli.py::load_task        task.toml + rubric.json + task.md + workspace/
bench_core/cli.py::start_container  docker run -d --name … \
                                      -v <run>/workspace:/task \
                                      -v <run>/workspace/outputs:/data/amt-out \
                                      -v <run>/container/host-logs:/data/logs \
                                      -p 127.0.0.1:0:<mock port> … <pinned image> \
                                      /bin/bash -lc "tail -f /dev/null"
bench_core/cli.py::start_task_mock_services   docker exec … bash services/start_services.sh
grader/run.sh                       OUTPUT_DIR=… REWARD_JSON=… python3 verify_task.py \
                                      --task-dir … --output-dir … --reward-json …
```

Adapter (this stage, `experiments/business_benchmarks/commerce_agent_bench/`):

| Module | Responsibility | Reuses |
|---|---|---|
| `upstream.py` | pinned commit/image verification, task loading, per-task digests | upstream files only |
| `container.py` | `ContainerBackend` protocol, `WslDockerBackend` (Docker lives in WSL here), `WslShell`, `BashGrader` | Docker CLI |
| `environment.py` | per-run workspace copy, container start with the upstream mount/port layout, task-native service start, health probes, teardown + cleanup reporting | upstream scripts |
| `grader.py` | runs the original `grader/run.sh`, parses `reward.json`, upstream pass rule | upstream grader |
| `plans.py` | A1 DAG for the one task whose public material was audited | public `task.md` |
| `evidence.py` | reproducible upstream audit record | — |
| `smoke.py` | `env` / `reference` / `script` driver | — |

Not implemented in this stage: the Atlas agent loop binding (`tool_bridge.py`, `runner.py`,
`metrics.py`). They are deliberately absent rather than half-built, because without a running
environment they could not be validated.

## 4. Tool surface and permissions

Design intent (documented, **not exercised live**):

* API tasks: HTTP calls to the published mock port, `Authorization: Bearer local-mock-token`, exactly
  the endpoints in the task's `workspace/api_reference.md`.
* CLI tasks: `docker exec` into the task container running the native CLI binaries.
* Every call would be a registered Atlas tool, therefore authorized by M3 and recorded by M4/M7; the
  adapter itself adds no bulk-query shortcut and no direct database access.
* The task directory and the private directory are mounted **read-only**; only `/task` (the staged
  workspace copy) and `/data/*` are writable.
* Model credentials never enter the container: `run_container` passes only `BENCH_TASK_ID` and
  `ATLAS_ADAPTER`.

## 5. A0 / A1 execution plan

A0 = one Atlas agent with the whole public tool surface; A1 = static DAG over the public deliverable
with a single synthesis step owning the graded files.

The only plan frozen from public material is the Slack RFQ task (read the thread → model landed cost
→ synthesize `answer.json` + `quote_consolidation.md`). The other four are marked
`PENDING_PUBLIC_AUDIT` in `plans.py` instead of being guessed from the private rubric.

**No A0/A1 run was executed**, so the plan is a design artifact, not a result.

## 6. Native grader integration

`NativeGrader` runs the task's own `grader/run.sh` with `OUTPUT_DIR` and `REWARD_JSON`, parses the
reward file, and applies the upstream pass rule ("every required check passes", cross-checked against
`score == 1.0`).

Executed for real against the pinned task on this host:

| Field | Value |
|---|---|
| status | `GRADER_OK` |
| score | 0.0 |
| checks | 0 / 7 |
| environment_failure | false |

That is the correct verdict for a hand-made outputs directory: the `setup_gate` check requires a
`mock_audit/slack_audit.json` produced by a real run, so 0/7 is the honest outcome and proves the
grader chain (bash → python3 → reward JSON → parser) works. The private reference answer was used
only for this self-check; it was **not** committed and no expected value was copied into the
repository (`experiments/evidence/stage-b0/grader-selftest/grader_selftest.json` holds counts only).

## 7. Environment state: BLOCKED

| Probe | Result |
|---|---|
| `docker pull --platform linux/amd64 <pinned>` (attempt 1, 270 s) | 10 layers `Download complete`, **0** `Pull complete`, image not registered, pull process alive and idle |
| `docker pull` (attempt 2) | layers report `Already exists`, still **0** `Pull complete`, image not registered |
| `docker manifest inspect <pinned>` | `failed to configure transport: error pinging v2 registry: Get "https://registry-1.docker.io/v2/": context deadline exceeded` |
| `docker pull alpine:3.20` | succeeded — the daemon and ordinary registry access work |
| `wsl.exe -d Ubuntu -- python3 -V` | 3.12.3 — the host side of the grader is available |

Conclusion: the pinned image cannot be finalised on this host. Substituting a different image (or a
tag) would break the pin and the benchmark contract, so the stage stops here rather than fabricating
an environment. Recorded in `experiments/evidence/stage-b0/environment_blocker.json`.

## 8–10. Business results, tokens/cost, parallel benefit

**Not available.** No run was executed, so:

| Metric family | Status |
|---|---|
| native_pass / native_score per task | NOT_RUN |
| prompt/completion tokens, cost, latency | NOT_RUN |
| worker_count, parallel overlap, critical path, handoff count | NOT_RUN |
| duplicate reads / duplicate business writes | NOT_RUN |
| budget stop and failure attribution | NOT_RUN |

Any number in these categories would be invented, so none is reported.

## 11. Runtime mechanism validation (deterministic)

| Mechanism | Status | Evidence |
|---|---|---|
| Worker identity isolation inside one task environment | NOT_VALIDATED | requires a live agent loop |
| Cross-run state isolation | PARTIAL | per-run directory + per-run container name are enforced by `NativeEnvironment`; not exercised against a live container |
| Unauthorized API/CLI rejection (M3) | NOT_VALIDATED | no business tool was registered live |
| Tool calls through M3/M4 | NOT_VALIDATED | as above |
| Budget stop keeps trajectory | NOT_VALIDATED | R1-1 proves it for coding runs only |
| Business deliverable not overwritten by another worker | NOT_VALIDATED | A1 plan assigns the graded files to one step |
| Environment failure vs model failure | PASS (adapter level) | `environment_failure` on start/service/health/grader failures, covered by tests |
| Grader runs independently | PASS | grader executes on the host from the native script |
| Cleanup failure observable | PASS | `teardown()` returns the error string; test covers a dead daemon |
| Hidden material isolation | PASS (adapter level) | private dir mounted read-only, never staged into the workspace; test asserts no private content and no `private/` path in the staged tree |

## 12. External harness reference (B-ref)

Not attempted. It needs the same container image plus a Judge API key; with the image blocked there
is nothing to reference.

## 13. Known limitations

1. The native environment is blocked by the image finalisation failure (§7) — the single blocker for
   every business number.
2. `api-gmail-invoice-hold-mcp` has no task-local service script; the adapter would need the upstream
   mock-runtime machinery for that task.
3. The adapter has no Atlas agent binding yet (`tool_bridge.py` / `runner.py`), so M2/M3/M4/M6/M7/G2a
   are not exercised in a business context.
4. Grader execution relies on WSL `bash` + `python3`; a Windows-only host would need a different
   backend. This is recorded, not hidden.
5. A1 plans exist for one task only; the rest await a public-material audit.

## 14. Regression

| Check | Result |
|---|---|
| `pytest -q` | exit 0 (full suite, +13 B0 adapter tests) |
| `ruff check .` | All checks passed |
| `ruff format --check .` | 402 files already formatted |
| `npx pyright` | 0 errors, 0 warnings |

No existing runtime path was modified: the adapter is additive under
`experiments/business_benchmarks/`, plus `THIRD_PARTY.md`, `.gitignore` and this report.

## 15. CI status

**Unknown.** The branch is pushed; no PR was created from this environment (`gh` and GitHub token are
unavailable), so no CI run was observed.

## 16. GitHub

Branch `experiment/b0-commerce-agent-bench`, based on the R1-2H head
`fix/r1-2h-oracle-sandbox-isolation`. Open it at
<https://github.com/Melon235/AtlasOS/pull/new/experiment/b0-commerce-agent-bench> with base
`fix/r1-2h-oracle-sandbox-isolation`.

Stack for the record: R1-0 → R1-1 → R1-2A → R1-2H → B0. **No PR exists for R1-1, R1-2A or R1-2H
either** — the same blocker reported in the previous three stages.

## 17. Recommendation for R1-3

Do not start budget economics on the strength of this stage: there is no business cost data yet.
The precondition for R1-3 (and for any B-series work) is a host that can finalise the pinned image.
Once `smoke --mode env` and `--mode reference` pass, the next step is one A0 Slack run to establish a
baseline before any A1 comparison.

---

## Verdicts

| Dimension | Verdict | Basis |
|---|---|---|
| Upstream pinning and provenance | **PASS** | commit verified, image digest pinned, per-task digests recorded and enforced on load |
| Adapter architecture and code | **PASS** | environment/grader/plans implemented, 13 deterministic tests green |
| Native environment startup | **FAIL** | pinned image cannot be finalised on this host (two attempts, registry timeout) |
| Native grader integration | **PASS** | original `grader/run.sh` executed, reward parsed, 0/7 correct for an empty run |
| Hidden/material isolation | **PASS** (adapter level) | read-only mounts, no private staging, tested |
| Business task results (A0/A1) | **NOT_RUN / INCONCLUSIVE** | no environment, no runs |
| Runtime mechanism reuse in business | **NOT_VALIDATED** | no live business tool call |
| Regression safety | **PASS** | full suite, lint, format, types clean |
| GitHub readiness | **PARTIAL** | scoped branch pushed; PR/CI not created |

## The six questions

1. **Can AtlasRuntime complete third-party native business tasks?** Not demonstrated. The environment
   could not be started, so no task was attempted.
2. **Which tasks have real multi-worker parallel benefit?** Unknown — nothing ran. The Slack task is
   structurally parallel (thread reading vs cost modelling) but that is a hypothesis, not a result.
3. **Which tasks suit a single agent?** Unknown for the same reason.
4. **Where does multi-worker cost come from?** Unknown for business tasks; the R1-0 finding (turns,
   not prompt duplication) is a coding-task result and is not transferred.
5. **Do the safety/execution/delivery/budget mechanisms create engineering value here?** Only at the
   adapter boundary: environment-failure separation, cleanup reporting and hidden-material isolation
   are implemented and tested. M3/M4/M6/M7/G2a remain unexercised in a business run.
6. **What should R1-3 optimise?** Nothing yet. First unblock the image on a host with working
   registry access, re-run the two smoke modes, then measure one A0 Slack baseline.
