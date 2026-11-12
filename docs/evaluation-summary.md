# Evaluation Summary

Every number below was produced by a run that exists on a historical branch, and every number is
quoted from a report in `docs/` or `experiments/results/`. Nothing is aggregated across stages.

## 0. How to read this document

- **Do not** add percentages across rows. Task sets, models, sample sizes and runtime versions differ
  (see §8).
- "Oracle" means the independent grader. "Task success" additionally requires the harness to finish
  without a budget/turn/sandbox failure.
- Where a report did not state a number, this document says *not reported* rather than estimating.
- Deterministic (no-model) evidence is labelled as such and never presented as agent capability.

## 1. Evidence inventory

| ID | Experiment | Task set | Model | Groups / n | Runtime | Report |
|---|---|---|---|---|---|---|
| E1 | Paired single vs multi-worker | 12 tasks × 3 reps | `deepseek-chat`, T=0 | A0 n=36 / A1 n=36 | `50759ed` | `experiments/results/stage-e1-report.md` |
| E2 | Context plane Track A | 8 workflows × 3 reps | `deepseek-chat` | C0/C1/C2 n=24 each, P0/P1 n=12 | `85b3918` | `experiments/results/stage-e2-report.md` |
| G1 | Dual-channel handoff | 3 tasks × 2 modes × 2 reps | `deepseek-chat` | H0 n=6 / H1 n=6 | worktree (landed in `7bd0965`) | `docs/stage-g1-report.md` |
| G2a | Run-scoped budget | 2 tasks × 2 modes × 2 reps | `deepseek-chat` | b0 n=4 / b1 n=4 | worktree | `docs/stage-g2a-report.md` |
| G2b-0 | Token/context diagnosis | 71 archived runs | *no model* | read-only | — | `docs/stage-g2b-diagnosis.md` |
| G2b-1 | Rolling compaction | 3 tasks × 2 arms × 2 reps | `deepseek-chat` | C0 n=6 / C1 n=6 | worktree | `docs/stage-g2b1-report.md` |
| G3-0 | Failure critical-path audit | 71 archived runs | *no model* | read-only | — | `docs/stage-g3-failure-audit.md` |
| G3-1 | Tool-error semantics | 2 tasks × 2 modes × 2 reps | `deepseek-chat` | legacy n=4 / corrected n=4 | worktree | `docs/stage-g3-1-report.md` |
| G3-2 | Ingress offload | 3 tasks × 2 arms × 2 reps | `deepseek-chat` | O0 n=6 / O1 n=6 | worktree | `docs/stage-g3-2-report.md` |
| G3-3 | Worker task contract | 3 tasks × 2 arms × 2 reps | `deepseek-chat` | C0 n=6 / C1 n=6 | worktree | `docs/stage-g3-3-report.md` |
| G3-4a | Benchmark integrity + rebuilt baseline | 3 tasks × 2 modes × 2 reps | `deepseek-chat` | A0 n=6 / A1 n=6 | worktree | `docs/stage-g3-4a-report.md` |
| R0 | Delegation router | 3 tasks × 3 arms × 2 reps | `deepseek-chat` | A0/A1/A2 n=6 each | worktree | `docs/stage-r0-routing-report.md` |
| R0.5 | Positive delegation | Track A 3 families × 3 arms × 2 reps; Track B 2 tasks × 3 arms × 2 reps | `deepseek-chat` | 18 + 12 runs | `2183983` | `docs/stage-r0-5-positive-delegation.md` |
| R1-0 | Failure attribution | 30 archived runs | *no model* | read-only | `a8dde65` | `docs/stage-r1-0-failure-attribution.md` |
| R1-1 | Ownership enforcement | 2 tasks × 2 arms × 2 reps × 4 rounds | `deepseek-chat` | O0/O1, final round n=4 each | `4ad5264` | `docs/stage-r1-1-worker-ownership-integrity.md` |
| R1-2A | Deliverable verification | 58 historical exports + 8 dynamic runs + 8 live runs | `deepseek-chat` (live arm only) | observation only | `e90d53c` | `docs/stage-r1-2-deliverable-verification.md` |
| R1-2H | Oracle sandbox isolation | 28 historical workspaces | *no model* | re-grade | `facca98` | `docs/stage-r1-2h-oracle-isolation.md` |
| AB | 30-task benchmark | 30 tasks × 3 reps = 90 | requested `deepseek-chat`, resolved `deepseek-flash` | Atlas 90 / AnyCode baseline 25 | `18e37f8` | `experiments/results/eval-final-report.md` |
| CP | Control-plane suite | deterministic scenarios | *no model* (fake tools) | 1 run | `5ed1305` | `experiments/results/control-plane-results.md` |
| IV | Integrated verification | 8 scripted scenarios | scripted adapter + fake sandbox | 1 run | `51dc85f` | `experiments/results/v2-integrated-verification.md` |
| S6 | V1 context plane, final | offline + live scaling | `deepseek-chat` | 5 reps/variant | `a527796` | `experiments/results/stage6-final.md` |

---

## 2. Baseline task execution (E1, AB)

### E1 — paired single vs multi-worker (72 runs)

| Metric | A0 single | A1 static DAG |
|---|---|---|
| Task success | **28/36 (77.8 %)** | 3/36 (8.3 %) |
| Prompt tokens | 757,828 | 4,992,381 (**6.59×**) |
| Completion tokens | 61,598 | 171,656 (2.79×) |
| Cost (USD) | 0.0987 | 0.4063 (**4.12×**) |
| Cost per successful task | $0.0035 | $0.135 |
| Cache hit ratio | 0.9094 | 0.8839 |
| p50 latency | 37.5 s | 72.7 s (1.94×) |

Paired outcome over 36 samples: A1-only success 2, A0-only 27, both 7, neither 0;
mean paired difference −0.694 (descriptive 95 % CI [−0.883, −0.506]).
96 worker executions reported 48 self-reported successes; an oracle failure was recorded in 8 runs
where every worker claimed success.

**Reading.** Static decomposition multiplies context work faster than it removes it. The one
recurring positive case is `e1-12-conflict` (A1 2/3 vs A0 0/3), a task that requires two
independent edits to conflicting regions.

### AB — 30-task benchmark (90 runs)

| Metric | Value |
|---|---|
| Task success | 84/90 = 0.933 |
| Failures | 6, all `ORACLE_FAILURE` (0 runtime/sandbox/timeout/cleanup failures) |
| Unauthorized side effects | 0/90 |
| Host isolation violations | 0/90 |
| Forbidden workspace changes | 0/90 |
| Tool Recall@1 / @3 | 1.0 / 1.0 (15/15 tool tasks) |
| E2E latency | P50 29,630 ms / P95 42,697 ms |
| Prompt / completion tokens | 1,702,051 / 109,093 |
| Cache hit / miss | 1,531,889 / 170,162 (hit ratio 0.900) |
| Cost | $0.19115 (peak-rate upper bound) |

Matched AnyCode baseline (25 runs, rep0): **20/25 (0.80)** vs Atlas **21/25 (0.84)**.
The report itself states this is **not** evidence of a capability advantage: one task, one
repetition, ~22 s more latency.

---

## 3. Multi-worker design trade-offs (E1, G3-4a, R0, R0.5)

| Experiment | Single worker | Multi-worker | Cost | Latency |
|---|---|---|---|---|
| E1 | 28/36 | 3/36 | 4.12× | 1.94× |
| G3-4a | 6/6 | 2/6 | 0.0142 vs 0.0461 USD | not reported |
| R0 | 5/6 | 2/6 | 0.0133 vs 0.0456 USD | not reported |
| R0.5 Track A | 6/6 | 2/6 | **+40 % per run** | **+27 %** |
| R0.5 Track B | 4/4 | 2/4 | **+269 % per run** | **+59 %** |

R0.5 also proved that real parallelism happens: 20/20 multi-worker runs showed wall-clock overlap,
and three workers started within 10 ms of each other on `e1-01-config`. The parallelism is real; the
net benefit is not.

Selective delegation (R0): the router rejected **12/12** candidate tasks, so no live delegation
occurred. R0.5 forced positive admissions (6/6 admitted, 0 false admissions, 3/3 negative
rejections) and still lost on correctness and cost.

Fixed per-worker overhead (R1-0): 1,988–2,085 tokens per worker instance, i.e. **4.5–5.2 %** of
spend. Repeated system prompts are not the cost driver.

---

## 4. Execution integrity (R1-0, R1-1, G3-1)

**Why the evidence model changed.** Stage R1-0 compared two records of the same 30 runs:

| Record | Actions captured |
|---|---|
| `result.json` (upstream task result) | 365 |
| Trajectory (Atlas recorder) | 591 |
| Missing from `result.json` | **226 (38 %)**, concentrated in failed workers |

Any analysis that trusts `result.json` concludes that failed workers "did nothing". This single
measurement is the reason `trajectory/` is a core component rather than a nice-to-have.

**Trace coverage after the fix (R1-1).** `input_digest_coverage = 1.00` over 32 runs, trace status
`TRACE_COMPLETE`, 111 actions recovered that `result.json` had dropped.

**Tool error semantics (G3-1).**

| Metric | Legacy | Corrected |
|---|---|---|
| Oracle pass | 2/4 | 4/4 |
| `side_effect_unknown` workers | 2 | 0 |
| Premature stop after a completed bash error | 2 | 0 |
| Prompt tokens | 260,245 | 258,866 |
| Cost (USD) | 0.029021 | 0.031242 |
| Ledger `UNCERTAIN` records | — | 3 (0 replayed) |

n=4 per arm, two tasks, one model. Mechanism PASS; task-quality effect is directional only.

---

## 5. Verification (R1-2A) and oracle isolation (R1-2H)

### Deterministic deliverable verification

| Population | Findings |
|---|---|
| 58 historical exports (static) | TP 23 / TN 0 / **FP 0** / UNKNOWN 35 |
| 8 dynamic runs | TP 3 / TN 3 / **FP 0** / FN 2 |
| Combined verified deliverables | **66 with 0 false positives** |

Cost: static mean **8.1 ms** (max 29.9 ms); dynamic mean **4.99 s** (max 5.29 s); observe mode
6.0–12.8 ms; **0 model tokens**.

Weakness recorded by the report: `public_contract` = UNKNOWN 58/58 in the static population and
TP 3 / FN 2 in the dynamic one, so contract coverage is thin. One date family
(`2000-03-01`-style mid-month) was not detected.

### Oracle sandbox isolation

| Metric | Value |
|---|---|
| Re-graded workspaces | 28 |
| Score agreement | **28/28 MATCH** |
| `TEST_ENVIRONMENT_DIFFERENCE` | 0 |
| `DEPENDENCY_DIFFERENCE` | 0 |
| `ACTUAL_SCORING_DIFFERENCE` | 0 |
| Creation / upload / execution / cleanup | 3,120–3,399 / 376–415 / 1,419–1,633 / 990–992 ms |
| Total per grading | 5,950–6,400 ms (max 9,429) |
| Model tokens | **0** |

Two earlier batches were discarded because the harness rebuilt the wrong fixture variant, producing
two spurious `ACTUAL_SCORING_DIFFERENCE` results. The accepted result is from the corrected harness.

---

## 6. Ownership enforcement (R1-1)

Final round (4 of 4), two tasks, n=4 per arm:

| Arm | Oracle | Cost USD | Prompt | Turns | Denials | Actions lost by `result.json` |
|---|---|---|---|---|---|---|
| O0 off | **2/4** | 0.0458 | 396,237 | 86 | 0 | 25 |
| O1 enforce | 1/4 | 0.0372 | 240,526 | 63 | 19 | 0 |

Across all four rounds: O0 oracle **7/16**, O1 oracle **1/16**; denials fell 83 → 76 → 66 → 19 as
the classifier improved. Denial mix in the final round: 17 `OWNERSHIP_UNCLASSIFIABLE_SHELL`,
1 `OWNERSHIP_PATH_ESCAPE`, 1 `OWNERSHIP_DENIED_UNDECLARED_PATH`.

**The enforce arm never beat the baseline.** The main compatibility loss was rejecting
`python3 -m pytest`. This is why enforcement defaults off.

---

## 7. Context engineering (E2, S6)

### E2 — task-scoped context + stable rendering + ContextPilot dedup

| Metric | C0 upstream | C1 task-scoped + stable | C2 + dedup |
|---|---|---|---|
| Correctness | 23/24 | 23/24 | 22/24 |
| Prompt tokens | 17,367 | 17,505 (+0.8 %) | 17,505 |
| Cache hit tokens | 8,448 | 8,448 | 8,448 |
| Cache miss tokens | 8,919 | 9,057 | 9,057 |
| Hit ratio | 0.4864 | 0.4826 | 0.4826 |
| Cost USD | 0.003540 | 0.003581 | 0.003581 |

Dedup fired **0 times in 0/72 calls** — dependency outputs were too short. Transform cost was
0.63 ms per call. Placement (P0/P1) did not change prompt tokens but halved agent switches
(48 → 24) and raised locality 0.000 → 0.667.

### S6 — live scaling of dedup (V1 context plane, 5 reps per variant)

| Duplicate ratio | Prompt reduction | Warm cache-miss reduction |
|---|---|---|
| 0 (small) | 0.0 % | 0 / −55.7 % / −18.8 % |
| 0.31 (medium) | 6.6 % / 6.8 % | −28.4 % / −42.5 % |
| 0.50 (large) | 27.1 % / **44.0 %** | **+2.5 % / +11.1 %** |

Offline transform cost: ~1 ms at 30 K chars, ~3 ms at 110 K, ~7–8 ms at 250 K.

**Reading.** A shorter prompt is not a cheaper prompt. At the duplicate ratio where dedup saves the
most tokens (44 %), the warm cache miss rate got *worse*.

Related negative results: rolling compaction raised prompt +9.3 %, cache miss +88 % and cost +43 %
(G2b-1); ingress offload moved prompt +3.7 % and cost −0.7 % (noise) while `max_turns` workers went
0 → 4 (G3-2). Neither is enabled by default.

---

## 8. Why these numbers cannot be added up

1. **Model differs.** All live runs used `deepseek-chat` at T=0 except AB, where the request for
   `deepseek-chat` resolved to `deepseek-flash` (cost is a Flash peak-rate upper bound).
2. **Sample sizes differ by an order of magnitude** — n=2/cell (R0, R0.5, G3-3, G3-4a),
   n=4/arm (G2a, G3-1, R1-1), n=6/arm (G1, G2b-1, G3-2), 36 paired (E1), 90 (AB).
3. **Runtime version differs.** Before G3-4a the sandbox still contained a provisioning bundle and
   a fixture-identifying commit, so E1/G1/G2/G3-1..3 are not the same environment as
   G3-4a/R0/R0.5/R1-x.
4. **Mechanism configuration differs.** Handoff truncation (E1) vs dual channel (G1+); guarded
   budget (G2a+) that can stop a run early; compaction/offload/contracts on in their own stage and
   pinned off afterwards.
5. **Benchmark definitions were revised.** Two R0.5 benchmark versions were voided (6 runs), and two
   R1-2H batches were voided.
6. **Metric names are not identical.** "Success" = oracle + harness, so a budget stop can fail a run
   whose artifacts the oracle would accept. Cache-hit ratio was reported as a run aggregate in
   E1/E2 and as token-weighted / median-run / excluding-first-call in G3-2. `prompt_tokens` in G2a
   includes cache hits; `TokenUsage.input_tokens` does not.
7. **V1 and V2 paths are separate.** Stage 6/E2 measure the V1 context plane; E1/G/R measure the V2
   coding adapter, which does not use the V1 prompt path at all.

---

## 9. Deterministic evidence only

These results are real and reproducible, but they are **not** model-capability measurements:

| Suite | Result | Nature |
|---|---|---|
| Control plane (M1–M3) | 24 action requests → 15 ALLOW / 1 DENY / 8 REQUIRE_HUMAN; 0 unauthorized executions, 0 gate bypasses, 0 trusted-field mutations, 0 stale-approval reuses | fake tools, local approver, no LLM |
| Integrated verification (S1–S8) | 6 suspensions, 7 approvals, 13 resume revalidations, 7 executed / 1 replayed / 0 uncertain, 14 hard-zero invariants all 0 | scripted adapter + fake sandbox |
| Stage D recovery | 11 real OS-process scenarios + 8 deterministic injections | real OpenSandbox, no model |
| Ownership write protection | rollback of `git checkout --` blocked by tests T2–T6 | deterministic |
| `history.read` recovery | byte-exact paging, digest tamper rejection | deterministic; the model called it **0 times** in 12 real runs |

Not validated (implemented, but no live evidence):

- Suspension resume inside a real model run (never triggered).
- Provider-level concurrency attribution (`provider_calls.jsonl` has no wall clock or worker id).
- Ownership enforcement benefit (never above baseline).
- Selective delegation admission quality (no live `DELEGATE_PARALLEL` admission in a benchmark).
- CPU/memory isolation in the sandbox.

---

## 10. What this evidence supports

Supported claims:

1. The runtime executes a 30-task coding benchmark at 84/90 with zero unauthorized side effects and
   zero host-isolation violations.
2. Trajectory evidence is materially more complete than `result.json` (591 vs 365 actions) and made
   failure attribution possible.
3. Deterministic deliverable verification achieves 0 false positives on 66 deliverables at
   millisecond-to-second cost with no model tokens.
4. Sandboxed grading reproduces every historical score (28/28) at ~6 s and 0 model tokens.
5. Static multi-worker decomposition is worse than a single worker on every task family measured.

Claims this evidence does **not** support:

1. Atlas is more capable than AnyCode (21/25 vs 20/25, one repetition).
2. Progressive tool discovery, compaction, ingress offload, worker contracts, ownership
   enforcement, or delegation improve cost or quality.
3. Cache-aware context layout reduces cost.

The proposed main experiment in `docs/final-consolidation-report.md` §17 targets the one claim that
is supported but not yet isolated: reliability under failure versus an unmodified AnyCode baseline.
