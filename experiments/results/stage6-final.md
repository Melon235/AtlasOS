# Stage 6 Final Results — AtlasOS V1

Frozen 2026-10-23 from clean code commit `a527796`; every formal record has
`git_dirty=false` and `provider=deepseek`, `model=deepseek-chat` for live runs.
Raw records: `experiments/runs/stage6-*.jsonl` (gitignored). Generated tables:
[stage6-tables.md](stage6-tables.md). Figures: [figures/](figures/).

## Setup

- Deterministic offline experiments use the existing Atlas context runtime, renderers,
  ContextPilot dedup transform, and placement planner. No provider is called.
- Live experiments call DeepSeek through the existing `DeepSeekAdapter`, synchronously and
  sequentially, with `temperature=0` and `max_tokens=160`.
- Provider cache is **uncontrolled and warm-prone**. Repeated conditions warm the cache; no
  strict cold-start control exists. Cache comparisons therefore use warm-state repetitions
  (2–5) for steady-state claims and also report all-repetition prompt totals.
- Dedup workload sizes are character targets (~30 K, ~110 K, ~250 K chars). Actual provider
  tokens are reported from the provider; characters are never presented as tokens.
- Exact duplicate ratio means byte-identical duplicate blocks that ContextPilot's official
  `dedup_chat_completions` API can detect. No semantic similarity is used.

## Table A — E1 stable layout

Review context with the same block contents, rendered under two explicit completion orders.

| renderer | completion order | rendered order | layout hash | invariant |
|---|---|---|---|---|
| naive_execution_order | A-first | research-a, research-b | `ded6ce5315ee51d9...` | no |
| naive_execution_order | B-first | research-b, research-a | `a97dffed69e76717...` | no |
| stable_deterministic | A-first | research-a, research-b | `ded6ce5315ee51d9...` | yes |
| stable_deterministic | B-first | research-a, research-b | `ded6ce5315ee51d9...` | yes |

C1's layout changes under completion-order inversion; C2's layout hash and rendered order are
invariant. C2 also produces the same full prompt hash for both orders.

## Table B — E2 dedup scaling

### Offline (deterministic context chars and transform CPU)

| scale | duplicate ratio | T1 char reduction | T0 latency ms | T1 latency ms |
|---|---:|---:|---:|---:|
| small | 0.000 | 0.0% | 0.00 | 1.20 |
| small | 0.167 | 9.6% | 0.00 | 0.94 |
| small | 0.310 | 7.4% | 0.00 | 0.89 |
| small | 0.500 | 7.6% | 0.00 | 1.05 |
| medium | 0.000 | 0.0% | 0.00 | 3.27 |
| medium | 0.167 | 11.2% | 0.00 | 3.23 |
| medium | 0.310 | 20.8% | 0.00 | 3.14 |
| medium | 0.500 | 32.3% | 0.00 | 3.06 |
| large | 0.000 | 0.0% | 0.00 | 7.45 |
| large | 0.167 | 14.7% | 0.00 | 7.78 |
| large | 0.310 | 27.6% | 0.00 | 7.53 |
| large | 0.500 | 44.9% | 0.00 | 6.68 |

Transform latency grows with context size (~1 ms at ~30 K chars, ~3 ms at ~110 K, ~7–8 ms at
~250 K). At the smallest scale, reference-string overhead and chunk boundaries dominate, so
the 31% and 50% duplicate workloads save slightly less than the 15% workload despite more
duplicate blocks.

### Live DeepSeek (5 repetitions per variant)

Prompt totals use all five repetitions. Cache tokens use warm repetitions 2–5.
`weighted hit = sum(cache_hit_tokens) / sum(prompt_tokens)` over the same window.

| scale | duplicate ratio | T0 prompt | T1 prompt | prompt reduction | T0 warm miss | T1 warm miss | warm miss reduction | T0 weighted hit | T1 weighted hit |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| small | 0.000 | 32,920 | 32,920 | 0.0% | 736 | 736 | 0.0% | 0.9721 | 0.9721 |
| small | 0.310 | 32,745 | 30,600 | 6.6% | 596 | 928 | -55.7% | 0.9772 | 0.9621 |
| small | 0.500 | 35,920 | 33,495 | 6.8% | 576 | 684 | -18.8% | 0.9800 | 0.9745 |
| medium | 0.000 | 116,120 | 116,120 | 0.0% | 736 | 736 | 0.0% | 0.9921 | 0.9921 |
| medium | 0.310 | 115,975 | 92,515 | 20.2% | 620 | 796 | -28.4% | 0.9933 | 0.9892 |
| medium | 0.500 | 117,920 | 81,140 | 31.2% | 640 | 912 | -42.5% | 0.9932 | 0.9860 |
| large | 0.000 | 261,220 | 261,220 | 0.0% | 592 | 592 | 0.0% | 0.9972 | 0.9972 |
| large | 0.310 | 262,135 | 191,070 | 27.1% | 812 | 792 | 2.5% | 0.9961 | 0.9948 |
| large | 0.500 | 260,920 | 146,240 | 44.0% | 864 | 768 | 11.1% | 0.9959 | 0.9934 |

Prompt reduction scales with both context size and duplicate ratio, reaching **44.0%** at
~50 K prompt tokens and ~50% exact duplicates. **Warm-state cache-miss reduction does not scale
the same way**: it is negative at small/medium scale (dedup changes the uncached tail) and only
2.5% / 11.1% at large scale. Dedup reduces prompt tokens, not warm cache-miss prefill, in this
workload. First warm repetitions are colder (the cache is warm-prone); all-repetition totals
would mix that transient into the comparison, so they are not used for the miss claim.

## Table C — E3 placement scaling

| topology | chain length | policy | switches | handoff refs | mean locality |
|---|---:|---|---:|---:|---:|
| P2 | 2 | capability_only (S1) | 4 | 4 | 0.000 |
| P2 | 2 | context_locality (S2) | 2 | 2 | 0.667 |
| P3 | 3 | capability_only (S1) | 6 | 6 | 0.000 |
| P3 | 3 | context_locality (S2) | 2 | 2 | 0.800 |
| P5 | 5 | capability_only (S1) | 10 | 10 | 0.000 |
| P5 | 5 | context_locality (S2) | 2 | 2 | 0.889 |

S2 reduces logical handoff refs by **50.0% (P2), 66.7% (P3), and 80.0% (P5)** relative to S1;
the absolute reduction stays at 2 because only the two cross-chain merge inputs still cross an
agent boundary. Mean assignment locality rises with chain length (0.667 / 0.800 / 0.889).
`total_logical_handoff_chars` remains 0 in the existing planner representation, so no character
claim is made.

Load-weight diagnostic (P3, S2): load weights 0.0 and 0.2 both give handoff 2 / switches 2;
load 1.0 raises both to 4 while lowering the largest assignment share from 0.429 to 0.286.
There is a locality-versus-load trade-off, not a free improvement.

## Table D — E4 placement × dedup

Medium workload (~20 K live prompt tokens, ~31% exact duplicates), P3 topology.

| variant | placement | transform | handoff | switches | locality | prompt | warm cache miss | transform ms |
|---|---|---|---:|---:|---:|---:|---:|---:|
| A0 | S1 capability | T0 identity | 6 | 6 | 0.000 | 23,195 | 155 | 0.00 |
| A1 | S2 locality | T0 identity | 2 | 2 | 0.800 | 23,195 | 155 | 0.00 |
| A2 | S1 capability | T1 dedup | 6 | 6 | 0.000 | 18,503 | 199 | 3.43 |
| A3 | S2 locality | T1 dedup | 2 | 2 | 0.800 | 18,503 | 199 | 3.54 |

Placement and dedup are **complementary in their own dimensions**: A3 keeps S2's lower handoff
(6 → 2) and T1's lower prompt (23,195 → 18,503, **20.2%**). They are **not additive for warm
cache miss** in this workload: T1's warm miss is 199 versus 155 for identity, a 28.4% increase.
The placement dimension and the context dimension changed independently and retained their
effects; no statistical interaction is claimed.

## Table E — E5 recovery regression

Clean-commit Stage 5 rerun; same frozen logic.

| scenario | completed reexecuted | in-flight reexecuted | rebuilt blocks | layout equivalent | final equivalent |
|---|---:|---:|---:|---|---|
| R0 | 0 | 0 | 0 | - | - |
| R1 | 0 | 1 | 1 | true | true |
| R2 | 0 | 0 | 2 | true | true |

Execution counts: R1 `research-a=1, research-b=2, review=1`; R2 `research-a=1, research-b=1,
review=1`. R0/R2 Review layout hash
`ded6ce5315ee51d98a0b05277692da0e28b1111ce2a85b1b19b7f195c07dd95b` and full prompt hash
`e38a01ae93601f552544c380416282c506808933a3581b87a7391a0566de13a4` are identical, as are the
ContextRefs, block content hashes, and final output `review-out`. R1 also matches R0 on all four
equivalence checks despite the duplicate in-flight invocation.

## Cross-experiment conclusions

1. Stable deterministic rendering removes prompt-layout dependence on completion order (C2
   invariant; C1 not).
2. ContextPilot dedup scales prompt reduction with context size and exact duplicate ratio, at
   ~1–8 ms CPU per request. It does **not** produce a matching warm cache-miss reduction in this
   workload.
3. Context-locality placement reduces logical handoff as chain length grows, with a load
   trade-off under a high load weight.
4. Placement and dedup combine without interfering with each other's primary dimension, but
   their benefits are not additive for warm cache miss.
5. Recovery preserves the unfinished task's logical/model-visible context and does not
   re-execute durably completed tasks.

## Reproduce

```bash
# deterministic offline E1/E2/E3 (clean tree)
python examples/stage6_offline.py

# live E2/E4 subset (clean tree, API key required)
ATLASOS_RUN_LIVE_TESTS=1 python examples/stage6_live.py

# Stage 5 regression (clean tree)
python examples/stage5_recovery.py

# tables, E5 check, and SVG plots
python examples/stage6_report.py
```
