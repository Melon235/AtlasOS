# Stage 6 — Final Evaluation and AtlasOS V1 Freeze

Frozen 2026-10-23. E1–E4 formal records were captured at clean code commit `a527796`; E5 was
rerun at clean `35b5774`, which includes the `2233bfd` / `5cde6f9` checkpoint-ordering fixes.
All formal records have `git_dirty=false`.
Results tables: [stage6-final.md](../../experiments/results/stage6-final.md).
Generated tables: [stage6-tables.md](../../experiments/results/stage6-tables.md).
Figures: [figures](../../experiments/results/figures/).

Stage 6 adds no product or runtime feature. It evaluates the frozen Stage 0–5 system, fixes no
runtime behavior, and records limitations rather than extending the architecture.

## Research questions

| ID | Question |
|---|---|
| Q1 | Does stable context rendering remove prompt-layout dependence on concurrent completion order? |
| Q2 | How does ContextPilot dedup benefit scale with context size and exact duplicate ratio, and what CPU overhead does it add? |
| Q3 | Does context-locality-aware placement reduce logical context handoff as dependency-chain length grows? |
| Q4 | Are context-locality placement and per-request dedup complementary optimizations? |
| Q5 | Does durable recovery preserve unfinished-task logical/model-visible context? |

## Experiment setup

- **E1** — deterministic fork/join rendering. The same Review context is rendered under A-first
  and B-first orders with the naive execution-order renderer (C1) and the stable renderer (C2).
  Completion order is an explicit input to the selector; no wall-clock sleeps or concurrent
  scheduling are involved.
- **E2** — offline and live dedup matrix. Context scales target ~5 K / ~20 K / ~50 K provider
  tokens (~30 K / ~110 K / ~250 K characters). Exact duplicate ratios target 0 / 15 / 30 / 50%;
  measured ratios are 0.000 / 0.167 / 0.310 / 0.500. Offline: 3 sizes × 4 ratios × 2 transforms,
  five latency repetitions. Live: 3 sizes × 3 ratios (D0, D2, D3) × 2 transforms × 5
  repetitions (90 calls).
- **E3** — deterministic placement on P2/P3/P5 parallel-chain workloads (two chains of length
  2/3/5 plus Merge), S1 capability-only vs S2 context-locality. One clean-tree execution; the
  planner is deterministic.
- **E4** — combined medium workload (~20 K prompt tokens, ~31% exact duplicates, P3 topology):
  A0 = S1 + identity, A1 = S2 + identity, A2 = S1 + dedup, A3 = S2 + dedup. Five live repetitions
  per variant (20 calls). Placement metrics are deterministic plans on the same P3 topology;
  context/provider metrics are live provider calls under the corresponding transform.
- **E5** — frozen Stage 5 R0/R1/R2 recovery regression from the clean Stage 6 commit.

Live model: `deepseek-chat` via the existing `DeepSeekAdapter`, temperature 0, max_tokens 160.
Provider cache is **uncontrolled and warm-prone**; no cold-start control exists.

## Metrics

- Layout: rendered dependency order, selected block ids, `context_layout_hash`,
  `stable_prefix_hash`, full prompt hash, success.
- Dedup: pre/post-transform chars, block counts, deduplicated flag, transform latency
  (mean/median/min/max/std), rendered chars, prompt tokens, cache hit/miss tokens.
- Cache aggregation: `weighted_cache_hit_ratio = sum(cache_hit_tokens) / sum(prompt_tokens)`
  (never an average of per-run ratios). Paired miss reduction
  `(T0_miss - T1_miss) / T0_miss` on identical workload conditions. Warm-state claims use
  repetitions 2–5; prompt totals use all repetitions.
- Placement: dependency-agent switches, logical handoff refs, mean assignment locality,
  assignment distribution, total tasks.
- Recovery: execution counts per task, completed/in-flight re-execution counts, rebuilt context
  blocks, ContextRef/block-hash/layout/prompt/final-output equivalence.

## Formal results

Full tables and per-condition numbers are in
[stage6-final.md](../../experiments/results/stage6-final.md).

### Q1 — stable layout (E1)

| renderer | A-first layout | B-first layout | invariant |
|---|---|---|---|
| C1 naive execution order | `ded6ce…` | `a97dff…` | no |
| C2 stable deterministic | `ded6ce…` | `ded6ce…` | yes |

C1's rendered order follows input/completion order and its layout hash changes. C2 renders
`research-a, research-b` in both cases with one layout hash and one full prompt hash. Q1: yes.

### Q2 — dedup scaling (E2)

Prompt reduction grows with scale and duplicate ratio: **0% at D0; 6.6–6.8% at small;
20.2–31.2% at medium; 27.1–44.0% at large** (live). Maximum observed: 44.0% at ~50 K prompt
tokens with ~50% exact duplicates.

Offline transform CPU: ~0.9–1.2 ms at ~30 K chars, ~3.1–3.3 ms at ~110 K, ~6.7–7.8 ms at
~250 K. It scales roughly linearly with context size.

Warm-state cache-miss reduction does **not** follow prompt reduction: negative at small and
medium scale, 2.5% at large/D2 and 11.1% at large/D3. Weighted warm hit ratios are 0.962–0.997
for T1 versus 0.972–0.997 for T0 depending on condition. Dedup shrinks the prompt but changes
the uncached tail; under a warm provider prefix it does not reduce cache-miss prefill in this
workload. The first repetition of each variant is colder; all-repetition totals mix that
transient and are not used for the miss claim.

### Q3 — placement scaling (E3)

| topology | S1 handoff | S2 handoff | reduction |
|---|---:|---:|---:|
| P2 | 4 | 2 | 50.0% |
| P3 | 6 | 2 | 66.7% |
| P5 | 10 | 2 | 80.0% |

Switches equal handoff refs in these workloads. Mean assignment locality rises 0.667 → 0.800 →
0.889. The absolute reduction stays 2 because only the two cross-chain Merge inputs still cross
an agent boundary. A load-weight diagnostic at P3 shows locality loses one chain handoff when
the load weight reaches 1.0 (handoff 2 → 4; largest assignment share 0.429 → 0.286): a real
locality-versus-load trade-off.

### Q4 — placement × dedup (E4)

| variant | placement | transform | handoff | prompt | warm miss |
|---|---|---|---:|---:|---:|
| A0 | S1 | T0 | 6 | 23,195 | 155 |
| A1 | S2 | T0 | 2 | 23,195 | 155 |
| A2 | S1 | T1 | 6 | 18,503 | 199 |
| A3 | S2 | T1 | 2 | 18,503 | 199 |

The dimensions are complementary: S2 retains its 6 → 2 handoff reduction while T1 retains its
20.2% prompt reduction. They are not additive for warm cache miss (199 vs 155). No statistical
interaction is claimed from five repetitions.

### Q5 — recovery (E5)

R1 execution counts are `research-a=1, research-b=2, review=1`; R2 is
`research-a=1, research-b=1, review=1`. No durably completed task was re-executed. R2 rebuilds
two blocks before Review executes; R1 rebuilds one durable root and re-executes the in-flight
root. R0/R2 agree on ContextRefs, block content hashes, `context_layout_hash`, full prompt hash,
and final deterministic output. R1 also matches on those checks. Q5: yes.

During full-suite verification, same-tick checkpoint ordering surfaced: UUIDv7 filenames can
sort out of order, and upstream `resume_from="latest"` sorts filenames. AtlasOS now selects the
most advanced checkpoint by durable execution state (completed-task count, then wave index,
`created_at`, and file mtime) and passes that exact checkpoint id to AnyCode (`2233bfd`,
hardened in `5cde6f9`, with regression tests). E5 was rerun after the fix; counts and equivalence
are unchanged.

## Interpretation

1. Deterministic stable rendering is the strongest result: it makes prompt layout a function of
   logical selection, not of concurrent completion timing.
2. Exact dedup is a prompt-size optimization. It scales in tokens/characters and CPU, but under
   a warm provider prefix it does not deliver proportional cache-miss reduction. Reporting only
   prompt length would have overstated its cache benefit.
3. Context-locality placement reduces logical handoff increasingly with chain length, but the
   absolute residual handoff is set by the merge topology. Load pressure can trade locality away.
4. Placement (where) and dedup (what) operate independently and retain their own effects when
   combined. The combined cache-miss effect is not additive in the measured workload.
5. Durable recovery preserves the model-visible context of unfinished work while avoiding
   re-execution of durably completed tasks; in-flight work remains at-least-once.

## Final V1 architecture

```text
Workflow
   ↓
Task Placement Planner          (deterministic pre-placement, S1/S2)
   ↓
AnyCode Execution Runtime       (DAG/waves/lifecycle/checkpoint/resume)
   ↓
Atlas Context Runtime           (selection, store, refs, logical layout)
   ↓
Per-request Context Transform   (identity / ContextPilot exact dedup)
   ↓
Stable Renderer                 (stable prefix + deterministic block order)
   ↓
Provider                        (DeepSeek; cache is best-effort)
   ↓
Checkpoint                      (AnyCode execution truth)
   ↓
Context-aware Recovery          (manifest identity + rebuilt ContextStore)
```

Attribution:

- **AnyCode 0.9.0** — agent runtime, DAG/wave execution, task lifecycle, checkpointing, and
  `resume_from` semantics. AtlasOS calls these; it does not reimplement them.
- **ContextPilot 0.5.0** — official exact-block dedup backend. AtlasOS integrates it behind a
  per-request transform; it does not reimplement the algorithm or use reorder online.
- **AtlasOS** — integration architecture, task/context model, logical context store and
  references, stable prompt layout, deterministic placement policy, session manifest and
  compatibility identity, rebuild-before-resume integration, metrics/experiment harness, and
  this evaluation.

## Explicitly out of scope for V1

No new memory subsystem, scheduler, provider-cache-history placement, MCP, sandbox, vector DB,
dynamic ready-task scheduler, checkpoint system, distributed execution, SGLang/vLLM, new
ContextPilot algorithm, or ContextPilot reorder on the production path. No physical KV-cache
control, cache-ID persistence, or exactly-once side-effect claim. Multi-node execution and
self-hosted serving are not part of V1.

## Limitations

- Provider cache is warm-prone and uncontrolled; no strict cold-start or cache-reset experiment.
- Live E2/E4 measure the provider call for a selected/rendered context. E4 does not execute a
  single seven-task live workflow with synthetic 20 K dependency outputs: injecting such
  outputs would require a new runtime feature, which Stage 6 is not allowed to add. Placement
  and context dimensions are therefore evaluated on the same topology but in separate
  measurement paths.
- Duplicate ratios are approximate by construction; actual ratios are reported.
- ContextPilot's exact dedup replaces later duplicate chunks with references, so character
  savings are not equal to duplicate-ratio × size, especially at small scale.
- Transform latency is in-process CPU only; it excludes tokenization/network.
- E2/E4 live samples are five repetitions per condition; distributions are reported
  (mean/median/min/max/std in raw records), but no significance test is claimed.
- Recovery supports one resume generation and grants at-least-once semantics for in-flight work;
  it does not claim exactly-once task/provider/tool execution.
- The Stage 6 workload is side-effect-free and deterministic where required.

## Reproduce

```bash
python examples/stage6_offline.py
ATLASOS_RUN_LIVE_TESTS=1 python examples/stage6_live.py
python examples/stage5_recovery.py
python examples/stage6_report.py
```

Formal records require a clean worktree; the harnesses refuse to append results from a dirty
tree. `ATLASOS_STAGE6_DRY_RUN=1` prints without recording.
