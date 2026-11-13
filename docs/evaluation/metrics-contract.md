# Metrics Contract

One definition per metric, so that two runs — and two runtimes — can be compared honestly. Every
metric carries: definition, unit, source field, collection boundary, aggregation, missing-value policy,
known limitation, and the configurations in which it is comparable.

This contract is normative for Track A/B/C. If an implementation cannot produce a metric under these
rules, the value is `null` / `UNAVAILABLE` and the run is reported with the gap — never with an
invented zero.

---

## 1. Global rules

| Rule | Statement |
|---|---|
| G-1 | A run is identified by `RunIdentity` (benchmark id, dataset/scorer version, sample, repetition, run id, `git_commit`, `git_dirty`, config digest, fixture digest, model, provider, start time). A metric without a run identity is not evidence. |
| G-2 | `runtime_status` and `oracle_pass` are **independent** facts and are always reported side by side. `runtime_success` is never used as a synonym for `oracle_pass`. |
| G-3 | Missing values are `null` in JSON and `UNAVAILABLE` in human tables (the evaluation runner already uses this string: `runner.py:424`). `0` means a measured zero. |
| G-4 | Token counts come from the provider response, not from a local estimator. Estimators are labelled `estimated_*` and never aggregated with measured values. |
| G-5 | `prompt_tokens = cache_hit_tokens + cache_miss_tokens` must hold when the provider reports the split. A mismatch is reported (`cache_metrics_complete=false`), not repaired. |
| G-6 | Wall-clock timings are taken with a monotonic clock on the machine that performed the work, and are reported per phase (§4). |
| G-7 | A metric that is only available for one runtime is marked `NOT_AVAILABLE` for the other; absence is never interpreted as "zero occurrences". |
| G-8 | Aggregation is reported with its denominator (n runs, n samples, n tasks). Percentages without a denominator are rejected. |

---

## 2. Task outcome (per sample × repetition)

| Metric | Definition | Unit | Source field | Aggregation | Missing policy | Known limitation |
|---|---|---|---|---|---|---|
| `runtime_status` | Terminal status of the execution layer, independent of quality: `succeeded` / `failed` (evaluation), `completed` / `failed` / `SANDBOX_LOST` / `CLEANUP_UNRESOLVED` (app) | enum | `result.json.status`, `session.status`, app `run.json.status` | count per enum value | required | App and evaluation enumerations differ; never mix them in one table |
| `agent_success` | The agent loop reported success | bool | `result.agent_success`, `session.agent_success` | rate | required | An unsuccessful loop can still produce a passing deliverable — keep both |
| `stop_reason` | Why the loop ended (`success`, `max_turns`, `budget_exceeded`, …) | enum | `session.stop_reason`, per-agent `stop_reason.code` | count per value | `null` when the provider/loop did not supply it | Free-form in older runs |
| `terminal_phase` | Last lifecycle phase reached | enum | `session.terminal_phase` | count | `null` | Only present after the Final-1.6 port |
| `failure_class` | Attributed failure category (`TASK_SUCCESS`, `MODEL_UNFINISHED`, `SANDBOX_PROVIDER_FAILURE`, …) | enum | `result.failure_class`, `session.failure_class` | count per class | `null` = unattributed; must be reported as a defect count | Current precedence can mask a sandbox failure behind `MODEL_UNFINISHED` (M-1) |
| `deliverable_present` | The expected artifact exists in the exported workspace | bool | `required_artifacts` vs exported tree | rate | required | Does not judge content |
| `oracle_status` | Oracle verdict kind (`HIDDEN_TESTS_PASSED`, `HIDDEN_TESTS_FAILED`, `HIDDEN_TEST_ERROR`, `ENVIRONMENT_FAILURE`, …) | enum | `oracle.failure_kind`, `oracle.environment_failure` | count per kind | required | An `ENVIRONMENT_FAILURE` is not a model failure |
| `oracle_score` | Fraction of oracle checks passed | ratio 0–1 | `oracle.checks` (bool map) | mean, per task | `null` if checks are unavailable | Not a continuous quality score |
| `oracle_pass` | All required checks passed | bool | `oracle.passed` | **primary rate** | required | Sample-level; a task-family rate needs its own denominator |
| `task_quality` | Human-readable summary: oracle pass + artifact + verification findings | composite | derived | per run | `null` for any missing component | Never collapses the components into one number without stating the rule |

**Comparable configurations:** within one dataset version and one Oracle mode. `oracle_pass` is not
comparable across `oracle_mode=host` and `sandbox`, across dataset versions, or between a native arm
without sandbox and an Atlas arm with sandbox unless §3.2 of `final-evaluation-protocol.md` holds.

---

## 3. Model resources (per run, per request)

| Metric | Definition | Unit | Source field | Aggregation | Missing policy | Known limitation |
|---|---|---|---|---|---|---|
| `prompt_tokens` | Total input tokens billed by the provider, including cached | tokens | `inference.jsonl.prompt_tokens`, `RuntimeMetrics.prompt_tokens` | sum per run; mean per arm | `null` (not 0) | `TokenUsage.input_tokens` is **not** this value (it excludes cache reads) |
| `cache_hit_tokens` | Input tokens served from the provider cache | tokens | `cache_hit_tokens` (DeepSeek `prompt_cache_hit_tokens`) | sum | `null` | Provider-side, best-effort |
| `cache_miss_tokens` | Input tokens that required prefill | tokens | `cache_miss_tokens` | sum | `null` | Equals `input_tokens` in `TokenUsage` |
| `cache_hit_ratio` | `hit / prompt_tokens` | ratio | derived | token-weighted over the window | `null` | Never aggregated as a mean of ratios; always weighted |
| `cache_metrics_complete` | The provider reported both hit and miss | bool | `cache_metrics_complete` | count | required | A run with `false` cannot enter a cache comparison |
| `completion_tokens` | Output tokens | tokens | `completion_tokens` | sum | `null` | — |
| `model_turns` | Assistant messages in the loop | count | `session.model_turns`, `RuntimeMetrics.model_turns` | sum | `null` on native if unavailable | Native may not expose it ⇒ `NOT_AVAILABLE` |
| `model_requests` | Provider calls issued | count | `inference.jsonl` line count / `provider_calls.jsonl` | sum | `null` | Retries are separate requests |
| `tool_calls` | Tool invocations executed | count | `result.tool_calls`, trajectory | sum | `null` | Includes `tool_search`/`dispatch_action` wrappers — state the filter |
| `total_cost` | `hit·p_hit + miss·p_miss + completion·p_out` for the frozen price snapshot | USD | `estimated_cost_usd` | sum | `null` if pricing incomplete | A price snapshot, not an invoice |
| `cost_per_oracle_pass` | `total_cost / oracle_pass_count` | USD/task | derived | arm level | `null` if 0 passes | Sensitive to a single task |

**Comparable configurations:** same provider, same requested model, same resolved model (record both),
same temperatures, same `max_tokens`, same price snapshot. A run whose requested `deepseek-chat`
resolved to a different model is reported separately.

---

## 4. Time

| Metric | Definition | Unit | Source | Boundary | Missing policy |
|---|---|---|---|---|---|
| `environment_setup_ms` | Workspace materialisation + sandbox provision + binding | ms | `provision_latency_ms`, sandbox timings | before the first model request | `null` |
| `agent_execution_ms` | First model request → last tool observation | ms | trajectory / `latency_ms` sum | excludes setup and grading | `null` |
| `oracle_execution_ms` | Hidden-test execution | ms | `oracle.timings_ms` | inside the disposable oracle container | `null` |
| `cleanup_ms` | Export + destroy + close | ms | session sandbox metrics | after terminal | `null` |
| `end_to_end_wall_time_ms` | `started_at → finished_at` of the whole sample | ms | `RunIdentity.started_at`, `result.finished_at` | the process, inclusive | required |
| `reattach_latency_ms` | Time to reattach an existing sandbox | ms | `reattach_latency_ms` | only in reattachment runs | `null` elsewhere |

**Rule:** do not present `end_to_end_wall_time_ms` as "runtime overhead". For the native arm it can
include host execution; for Atlas it includes sandbox provision and export. Report the phases
separately, and compare only like phases.

---

## 5. Reliability

| Metric | Definition | Unit | Source | Missing policy |
|---|---|---|---|---|
| `actual_action_attempts` | Tool calls that reached the execution boundary | count | gate decisions + ledger | `null` |
| `policy_denials` | Decisions with outcome DENY (incl. hard precheck) | count | gate audit, ledger `DENIED` | `0` is meaningful |
| `actual_unauthorised_side_effects` | Host/workspace mutations not covered by an allowed decision | count | workspace digest vs declared paths | **required**; absence is not provable when no sandbox exists |
| `false_denials` | A legal action was refused | count | human/rule review of denials | `null` if unreviewed |
| `confirmed_duplicate_effects` | The same logical action executed twice | count | ledger + idempotency store | required |
| `uncertain_actions` | Ledger rows ending `UNCERTAIN` | count | ledger state | `0` is meaningful |
| `safe_stop` | The runtime stopped without completing the task, by policy | bool | `stop_reason=budget_exceeded` / suspension | required |
| `approval_revalidations` | Resume-time re-checks of an approval | count | `resume_started`/`approval_resolved` events | `null` when no suspension |
| `sandbox_reattach` | Reattachment succeeded and continuity verified | bool | reattachment observation | `null` when not attempted |
| `sandbox_destroyed` | Terminal destroy confirmed | bool | `session.sandbox_destroyed`, `cleanup_error` | required |
| `cleanup_error` | Cleanup raised or refused | string/null | `cleanup_error` | required |
| `resource_leak` | A sandbox/container survived the run | bool | provider inventory at end of run | required for fault-injection runs |

**Rule:** never report "no unauthorised side effects" when the arm had no isolation boundary; write
`NOT_MEASURABLE (host execution)` instead.

---

## 6. Trace and deliverable

| Metric | Definition | Unit | Source | Missing policy | Known limitation |
|---|---|---|---|---|---|
| `observed_action_count` | Actions present in the trajectory | count | trajectory reader | required for Atlas | Native has no trajectory ⇒ `NOT_AVAILABLE` |
| `action_count_reference` | Independent count of executed actions (provider tool calls or ledger) | count | `provider_calls.jsonl`, ledger | `null` if none exists | Without a reference, coverage cannot be computed |
| `proposal_observation_pairing` | `action_proposed` events with a matching outcome | ratio | trajectory | required | — |
| `terminal_event_coverage` | Workers with a terminal event | ratio | trajectory | required | — |
| `input_digest_coverage` | Executed actions carrying an input digest | ratio | trajectory + ledger | required | App path does not emit it today (M-4) |
| `ledger_trace_consistency` | Ledger rows reconcilable with trajectory events | ratio | reconciler | required | Conflicts are reported, not resolved |
| `recovered_missing_records` | Actions found in the trajectory but missing from `result.json` | count | R1-0 analysis | `null` | Depends on both records existing |
| `verification_findings` | PASS/FAIL/UNKNOWN counts from the contract engine | counts | `verification.json` | `null` when disabled | Observation only; recall is weak |
| `oracle_outcome` | See §2 | enum | oracle | required | — |

**Rule:** a native arm has no Atlas trajectory. Report `NOT_AVAILABLE`, never "0 actions" — that would
claim nothing happened.

---

## 7. Cache experiments

| Metric | Definition | Notes |
|---|---|---|
| `cold_prompt_tokens` | First request of a run against an unprimed prefix | Cannot be guaranteed cold on a shared provider; record it as "first-in-run", not "cold" |
| `warm_miss_tokens` | `cache_miss_tokens` over repetitions ≥2 | The historical comparison window; state the window explicitly |
| `prefix_reuse` | Requests whose `stable_prefix_hash` equals the previous request in the run | Computable from `inference.jsonl` |
| `cross_run_contamination` | A later cell inheriting a prefix from an earlier cell | Detected by hash equality across cells; if present, the cell is re-ordered and the fact recorded |

Additional obligations: record the execution order of cells, the repetition index, and the warm-up
policy. Provider cache state is never claimed to be controlled; the protocol commits only to "warm"
vs "first-in-run".

---

## 8. Aggregation and reporting rules

1. Per-arm tables report `n`, the metric, and the dispersion or the raw values when `n < 5`.
2. Rates are pooled at the sample level, not averaged over per-run ratios (except `cache_hit_ratio`,
   which is token-weighted by definition).
3. Cost is summed, then divided by successes only when the denominator is stated.
4. A metric with a `NOT_AVAILABLE` column is shown with the gap in the table, not dropped.
5. Every table states dataset version, scorer version, runtime commit, model and Oracle mode in its
   caption.
6. Differences that were not planned before the run are labelled exploratory.
7. "No difference observed" with `n < 10` is written as "insufficient evidence", never "equivalent".
