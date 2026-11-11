# AtlasOS Agent Benchmark Metrics

All values are computed from the machine-readable `result.json` records in the round.

## Task capability

| Metric | Value | Numerator | Denominator |
|---|---:|---:|---:|
| Task success rate | 0.9333 | 84 | 90 |
| Evaluable task success rate | 0.9333 | 84 | 90 |
| Pass@1 (marginal) | 0.9333 | 84 | 90 |
| Agent self-reported success | 0.9333 | 84 | 90 |
| Independent oracle pass | 0.9333 | 84 | 90 |

## Category success

| Category | Runs | Success rate | Numerator | Denominator |
|---|---:|---:|---:|---:|
| bugfix | 30 | 0.9000 | 27 | 30 |
| feature | 15 | 0.9333 | 14 | 15 |
| tool_discovery | 15 | 1.0000 | 15 | 15 |
| multi_worker | 15 | 0.9333 | 14 | 15 |
| long_context | 15 | 0.9333 | 14 | 15 |

## Tool / worker behavior

| Metric | Value | Numerator | Denominator |
|---|---:|---:|---:|
| Tool Recall@1 | 1.0000 | 15 | 15 |
| Tool Recall@3 | 1.0000 | 15 | 15 |
| Tool selection accuracy | 1.0000 | 15 | 15 |
| All workers successful | 0.8667 | 13 | 15 |

## Safety

| Metric | Value | Numerator | Denominator |
|---|---:|---:|---:|
| Runs with unauthorized side effects | 0.0000 | 0 | 90 |

- Host isolation violations: 0
- Approval bypass count: NOT_EVALUATED in the capability round; see reliability matrix.
- Policy bypass count: NOT_EVALUATED in the capability round; see reliability matrix.

## Performance

| Metric | Value |
|---|---:|
| e2e_latency_p50_ms | 29629.8405 |
| e2e_latency_p95_ms | 42697.019649999995 |
| e2e_latency_mean_ms | 29968.843 |
| provision_latency_p50_ms | 7761.023 |
| provision_latency_p95_ms | 8067.643599999999 |
| inference_latency_p50_ms | 9419.7865 |
| inference_latency_p95_ms | 20048.6226 |
| prompt_tokens_total | 1702051 |
| completion_tokens_total | 109093 |
| cache_hit_tokens_total | 1531889 |
| cache_miss_tokens_total | 170162 |
| cache_hit_ratio | {'numerator': 1531889, 'denominator': 1702051, 'value': 0.9000253223904572} |
| cache_metric_runs_complete | {'numerator': 90, 'denominator': 90, 'value': 1.0} |
| estimated_cost_usd_total | 0.19115155 |
| estimated_cost_runs | 90 |
| model_turns_mean | 7.011 |
| tool_calls_mean | 7.544 |
| context_transform_overhead | NOT_EVALUATED |
| sandbox_reattach_overhead | NOT_EVALUATED (no coding-run reattachment) |

## Failure classification

| Class | Count | Fraction | Numerator | Denominator |
|---|---:|---:|---:|---:|
| TASK_SUCCESS | 84 | 0.9333 | 84 | 90 |
| ORACLE_FAILURE | 6 | 0.0667 | 6 | 90 |

## Notes

- Resolved provider models: ['deepseek-flash']
- Requested model: deepseek-chat
- Context transform overhead: NOT_EVALUATED for sandbox coding runs; the coding adapter does not exercise the V1 ContextPilot/transform path.
- Sandbox reattachment overhead: NOT_EVALUATED; normal benchmark runs do not reattach.
- Estimated cost uses the DeepSeek Flash peak-rate snapshot and is an upper bound.
