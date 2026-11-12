# Stage B0-R2 — First Native Business Agent Execution

**Project:** AtlasRuntime (repository `Melon235/AtlasOS`, package `atlasos`).
**Task:** `api-slack-supplier-quote-thread-consolidation` (Commerce Agent Bench, pinned
`d38bd17…`, image `acciolyk/accio_bench@sha256:1e9cf5c7…`).
**Scope:** one real Single-Agent run through the existing runtime, scored by the original grader.

**Answer in one paragraph.** The chain is real and it ran: DeepSeek drove one Atlas worker through
`AtlasToolRuntime` → M3 `ActionSafetyGate` → the native Slack mock service, six model turns and ten
successful tool calls, then the original `grader/run.sh` scored the workspace. **Native score: 0/7
(0.0), task failed.** The failure is not infrastructure: no denials, no tool errors, no budget stop,
runtime reported success — the agent simply never called `write_deliverable`, so `answer.json` and
`quote_consolidation.md` were never created. Attribution: `MODEL_REASONING_FAILURE` (stopped after
partial evidence gathering) compounded by `DELIVERABLE_FAILURE`. Cost: 30 931 prompt + 1 512
completion tokens, 12.8 s of agent time inside a 15.3 s end-to-end run.

---

## 1. Integration path (audited, then reused)

| Concern | Existing mechanism | Used here |
|---|---|---|
| Tool registration | `AtlasToolRuntime(external_tools=…)` | four business tools |
| Native visibility | `AllToolsAtlasRuntime.agent_tools` (evaluation ablation) | sub-classed to expose **only** the four business tools |
| Authorization | `ActionSafetyGate` + `ActionPolicy` (default DENY) | `external.read`, `filesystem.read`, `filesystem.write` only |
| Identity | `ScopedWorkerFactory` + `ExecutionContext` | one `business_worker` profile, Host-owned |
| Ledger / trajectory | `SQLiteActionLedgerStore`, `TrajectoryRecorder` | wired, files written under the run dir |
| Budget | `RunBudgetState(limit_tokens)` + `RunBudgetHook` | guarded, 220 000 tokens |
| Engine | `AtlasContextEngine` + `TeamConfig`/`TaskSpec` | one worker, one task |
| Provider | `DeepSeekAdapter` via `register_provider_factory` | `deepseek-chat` → resolved `deepseek-flash` |
| Scoring | native `grader/run.sh` | executed on the host (WSL bash) |
| Environment | `NativeEnvironment` (WSL Docker) | fresh container + mock service per run |

No second agent engine, scheduler or tool executor was created.

## 2. Tool bridge

`experiments/business_benchmarks/commerce_agent_bench/tool_bridge.py` — four tools, Host-bound:

| Tool | Action | Boundary |
|---|---|---|
| `slack_list_messages` | `external.read` | fixed `POST /slack/messages`, page size 1–50 |
| `slack_get_message` | `external.read` | fixed `POST /slack/messages/get`, 20 k char cap |
| `read_task_file` | `filesystem.read` | workspace-relative, no `..`/absolute/symlink, `private/`, `grader/`, `tests/` refused |
| `write_deliverable` | `filesystem.write` | **only** `outputs/answer.json`, `outputs/quote_consolidation.md`; `outputs/mock_audit/**` refused |

No bash, git, python, arbitrary HTTP, Docker, Slack send/draft or grader access. The mock address is
bound by the Host (`host:port` when reachable from Windows, otherwise `docker exec` inside the
container); the model never supplies a URL. In this run the published port was reachable:
`transport = host:32770`.

## 3. Hidden-material isolation (what is actually guaranteed)

The container still mounts the upstream task and private directories read-only, which is
**read-only, not unreadable**. The guarantee this stage provides is at the tool boundary, not the OS:

* No tool accepts a path outside the staged workspace; `private/`, `grader/`, `tests/` are refused by
  name as well as by traversal/symlink checks.
* The staged workspace copy contains only the task's public files (asserted by test).
* No tool can call the grader, the verifier endpoints or the mock service's internal state; the audit
  directory is not writable.
* No shell exists, so nothing can `cat` the mounted private directory from inside the runtime.

Test matrix (`tests/unit/test_b0_r2_business_tools.py`, 13 tests): public read, traversal/absolute/
symlink/private/grader refusal, deliverable allow-list, `mock_audit` refusal, oversize refusal,
fixed endpoints, page-size validation, absence of shell/http/slack-write tools, schema digest
stability, Host-bound transport.

**Explicit limitation:** this is a controlled business-tool surface, not OS-level isolation of the
native task container.

## 4. Scripted smoke (no model)

`ScriptedBusinessProvider` walks list → read → write → write → finish against the real container:

| Field | Value |
|---|---|
| tool calls | 4 (`slack_list_messages`, `read_task_file`, `write_deliverable` ×2) |
| deliverables | `answer.json`, `quote_consolidation.md` **and** the native audit/state files |
| native score | **6/7 (0.857)** — only `setup_gate` fails, because it requires ≥19 message reads and the script reads one page |

That smoke proves the runtime chain and the grader agreement without a single model token.

## 5. The real A0 run

Frozen configuration (recorded in the run record):

```text
runtime_commit      e00… (B0 branch head at run time; see the run record)
upstream_commit     d38bd1711aa3926653452458a1be02c1efd12930
image_digest        acciolyk/accio_bench@sha256:1e9cf5c7…
task_digest         see experiments/evidence/stage-b0/upstream_audit.json
tool_schema_digest  recorded per run (sha256 over the four tool schemas)
model               deepseek-chat → resolved deepseek-flash
budget              220 000 tokens, guarded
timeouts            native 1800 s, provider 90 s
action_limit        60 tool calls, max_turns 40
verification        observe = off
```

Result:

| Field | Value |
|---|---|
| execution status | `EXECUTION_OK` (runtime success) |
| stop reason | none |
| model turns | 6 |
| tool calls | 10 — `slack_list_messages` ×1, `slack_get_message` ×8, `read_task_file` ×1 |
| denied actions | 0 |
| prompt / completion tokens | 30 931 / 1 512 |
| cache hit / miss | 29 696 / 1 235 |
| budget exceeded | false |
| duplicates | repeated `slack_get_message` on the same ids was observed; not deduplicated |
| deliverables | none written (`answer.json`, `quote_consolidation.md` missing) |
| native score | **0/7, score 0.0, passed=false** |
| grader status | `GRADER_OK` (7 checks evaluated) |
| environment | startup 1 754 ms, agent 12 849 ms, grader 157 ms, cleanup 510 ms, end-to-end 15 270 ms |
| cleanup | no failure |

## 6. Failure attribution

| Candidate | Evidence | Verdict |
|---|---|---|
| Tool integration | 10/10 calls succeeded; no denials; transport reachable | **not the cause** |
| Environment | container + mock service healthy, audit written, grader ran | **not the cause** |
| Budget / turns | 6 of 40 turns, 32 443 of 220 000 tokens | **not the cause** |
| Grader | `GRADER_OK`, evaluated all 7 checks | **not the cause** |
| Deliverable | both required files absent | **proximate cause** |
| Model reasoning | after 8 message reads the worker ended its turn without writing anything | **root cause** |

Classification: `MODEL_REASONING_FAILURE` + `DELIVERABLE_FAILURE`. The worker behaved like an
analyst that reports findings in chat and never files the paperwork — the prompt asks for files, the
model stopped early.

Runs 1 and 2 are kept in the evidence but marked **invalidated**: run 1 exposed the coding core tools
(`bash` was attempted and correctly denied by M3), and both runs hit a bridge defect where the tool
closures took `**kwargs` while the runtime calls `execute(validated, context)`, so every business
call failed. The bridge was fixed and the configuration re-frozen before run 3; no result from runs
1–2 is used as a benchmark score.

## 7. What the runtime mechanisms proved here

| Mechanism | Status | Evidence |
|---|---|---|
| M2 worker identity | **validated** | one Host-owned `business_worker` identity; name appears in ledger/trajectory |
| M3 ActionSafetyGate | **validated** | run 1 `bash` denied, 0 denials in run 3 because no forbidden call was made |
| M4 ledger | **written** | `ledger.db` created; record listing API not exposed by this store version (`records: null`) |
| M7 trajectory | **written** | `TrajectoryRecorder` produced its store for all three runs |
| G2a shared budget | **validated** | guarded budget with 32 k/220 k used, no rejection |
| Tool bridge boundary | **validated** | 13 deterministic tests |
| Native grader | **validated** | 0/7 on a real run, 6/7 on the scripted run |
| Hidden-material isolation | **validated at tool level** | private/grader/tests refused; no shell |

## 8. Cost, latency, and what is missing

* Tokens: 30 931 prompt (29 696 cached, 1 235 miss) + 1 512 completion.
* Monetary cost is **not** reported: the adapter's pricing snapshot was not applied in this run, so
  any number would be invented.
* Latency split: environment 1.75 s, agent 12.85 s, grader 0.16 s, cleanup 0.51 s.
* Missing: a second and third repetition (the stage asks for one run), A1 (blocked until A0 closes),
  and the five-task matrix.
* The real A0 ran with tools returning `str`; the bridge now returns `ToolResult` (the executor contract)
  and was re-validated by the scripted smoke (6/7), but not re-run with the model.

## 9. Regression

| Check | Result |
|---|---|
| `pytest -q` | exit 0 — full suite green (13 new B0-R2 tests) |
| `ruff check .` | All checks passed |
| `ruff format --check .` | 405 files already formatted |
| `npx pyright` | 0 errors, 0 warnings |

## 10. CI / PR

Branch `fix/b0-r2-first-native-agent`, based on `experiment/b0-commerce-agent-bench`.
<https://github.com/Melon235/AtlasOS/pull/new/fix/b0-r2-first-native-agent> with base
`experiment/b0-commerce-agent-bench`. The PR was not created from this environment (`gh` and token
unavailable) and CI status is **unknown** — no claim of a green build is made.

---

## Verdicts

| Dimension | Verdict | Basis |
|---|---|---|
| Business tool bridge | **PASS** | four scoped tools, 13 tests, fixed endpoints, allow-listed writes |
| Runtime permission chain (M3) | **PASS** | denial observed in run 1, zero unintended denials in run 3 |
| Hidden-material isolation | **PASS (tool boundary)** / **PARTIAL (container)** | OS-level container isolation not implemented by this stage |
| Native environment + grader | **PASS** | environment healthy, `GRADER_OK`, 6/7 on scripted run |
| First real A0 execution | **PASS (executed)** | 6 turns, 10 tool calls, real score |
| First real A0 task success | **FAIL** | 0/7, no deliverables written |
| Failure attribution | **PASS** | model reasoning + missing deliverable, infrastructure excluded |
| Cost transparency | **PARTIAL** | tokens exact; monetary estimate not applied |
| Regression | **PASS** | full suite, lint, format, types clean |
| GitHub readiness | **PARTIAL** | branch pushed; PR/CI not created |

## The six questions

1. **Did AtlasRuntime really complete a third-party native business task?** It executed one — the
   worker read the native data through the native API and the native grader scored it. It did not
   *pass* it: 0/7.
2. **Do the business tools go through the existing permission and execution chain?** Yes —
   `AtlasToolRuntime` → M3 → native mock service, plus ledger/trajectory/budget.
3. **What did the native grader give?** 0/7, score 0.0, `passed=false`, `GRADER_OK`; the scripted
   control reached 6/7, which shows the grader is reachable and discriminating.
4. **Which engineering mechanisms are validated in a business task?** Worker identity, M3
   authorization, budget accounting, tool bridge boundaries, native grader integration, trajectory
   and ledger writes.
5. **What did the first A0 cost and why did it fail?** 30 931 + 1 512 tokens, 15.3 s end-to-end; it
   failed because the agent stopped after partial evidence gathering and never wrote the two
   deliverables.
6. **Can we start the five-scenario Pilot?** **No.** One A0 run exists and it did not close the
   deliverable loop; A1 is explicitly blocked by the stage's stop condition until a first run closes
   end to end.
