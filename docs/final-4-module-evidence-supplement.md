# Final-4 — Module Evidence Supplement

Date: 2026-11-13. `BASE_SHA` = `603e9743b0680ac85def47d919c2241aa960beec`.
Frozen runtime = `93cbd98f9407e7358e997878238520dcac15827a`. `PILOT_EVAL_SHA` =
`28428e8d3b6c167a99736aac2dc17d1b61aa3658`.

Machine-readable: `experiments/results/final4/module_supplement.json`.

This supplement does not replace the Final-3 matrix. It adds evidence levels, separates current from
historical evidence, and records the offline studies that were actually executed.

---

## 1. Evidence-level correction to Final-3

Final-3 reported 59/59 functional PASS, which is correct but easy to misread. Every row now carries:

| Field | Meaning |
|---|---|
| `functional_evidence_level` | `E1_REGRESSION` (existing tests re-ran), `E2_INDEPENDENT_FUNCTIONAL` (new inputs), `E3_CONTROLLED_BENEFIT` (new workload + fair baseline), `E4_LIVE_INTEGRATED` (real model + real tools) |
| `benefit_evidence_level` | `NONE` / `E3_CONTROLLED_BENEFIT` / `HISTORICAL_ONLY` |
| `evidence_epoch` | `final3@93cbd98`, `final4@offline`, `final4@e2`, `HISTORICAL` |
| `baseline_type` | `ablation`, `keyword_backend`, `identity_transform`, `historical`, `none` |
| `current_independent_result` / `historical_result` / `live_result` | the three columns that must never be merged |

Distribution after the supplement: 59 rows; **4** now carry `E3_CONTROLLED_BENEFIT` from this stage
(A1-03, A1-04, A1-05, A2-04/A2-05), 5 carry a Final-3 controlled benefit, 6 are explicitly
`historical`, and **0** are `E4_LIVE_INTEGRATED` because no live run was possible.

Correction that matters most: **Final-3's "A1-05 NEUTRAL" was a workload artifact, not an adapter
defect** (§3). Correctness metrics (0 unauthorised effects, 0 duplicate effects, 100 % durable rows)
remain absolute invariants; they are **not** percentage improvements over native AnyCode and are not
presented as such.

## 2. Historical metrics kept separate

`A3-09` ownership 7/16 → 1/16, `A4-18` tool-error 2/4 → 4/4, `A5-01` trajectory 365 → 591,
`A5-03` digest coverage 1.00, `A5-08` verification 0 FP, `A5-09` oracle 28/28 and the historical
ContextPilot 44 % are all marked `HISTORICAL` in `module_supplement.json`. None of them was re-measured
in this stage and none may be quoted as a Final-4 result.

## 3. ContextPilot bounded diagnosis (D0–D3)

Environment: `D:\Work\AtlasRuntime-e2`, CPython 3.12.15, `contextpilot==0.5.0`, source =
`93cbd98`. The `.pth` startup error is reproduced and preserved; it does not prevent the transform from
running.

| Cell | Input | Deduped | Chars before → after | Reduction |
|---|---|---|---|---|
| **D0** raw API, multi-line payload | 12 blocks | **12 of 21 chunks** | 6 576 → 4 482 | **31.84 %** |
| **D1** Atlas adapter, same payload | 12 blocks | **yes** | 6 576 → 4 482 | **31.84 %** (0.49 ms) |
| **D2** raw API, Final-3 single-line payload | 12 blocks | 0 | 10 680 → 10 680 | 0 % |
| **D3** Atlas adapter, same payload | 12 blocks | 0 | 10 680 → 10 680 | 0 % |

D1 preserved all six distinct source headers after chunk replacement.

**Root cause of the Final-3 result** (read from the installed 0.5.0 source, unmodified):
`_content_defined_chunking` splits each *message* by lines; a message with fewer than two chunks takes
a record-only path, and only chunks of `>= MIN_BLOCK_CHARS = 40` are hashed. A block that is one very
long line can therefore never be deduplicated, no matter how many copies exist. Final-3 used 880-char
single-line blocks, so zero dedup was the only possible outcome — and the adapter's 1:1 zip is
**correct** for this API, because dedup replaces duplicated chunks with references instead of removing
messages.

**Classification: `PAYLOAD_DEPENDENT_BENEFIT`** (with an adapter limitation only for single-line
blocks). A1-05 is updated to functional PASS / benefit POSITIVE under the stated condition, and the
Final-3 NEUTRAL is marked superseded.

## 4. Tool retrieval at 64 and 128 tools

New catalog authored for this stage, 32 positive + 6 negative queries, labels frozen before the run
(`query_digest = e4982115…9d22e6b`). Negative queries never enter the Recall denominator.

| Catalog | Backend | Recall@1 | Recall@3 | Recall@5 | MRR | Index build | Warm query |
|---|---|---|---|---|---|---|---|
| 64 | keyword | 0.6875 | 0.7812 | 0.8438 | 0.7484 | 0.16 ms | 0.125 ms |
| 64 | semantic | **0.8125** | **0.9375** | **0.9375** | **0.8646** | 11.2 s (incl. model load) | 6.81 ms |
| 128 | keyword | 0.7812 | 0.8750 | 0.8750 | 0.8125 | 0.31 ms | 0.276 ms |
| 128 | semantic | **0.9062** | **0.9375** | **1.0000** | **0.9292** | 236 ms (post-load) | 7.30 ms |

Three honest qualifications:

1. Exact canonical-id hits are low (0.31 / 0.34 at 64; 0.31 / 0.28 at 128) because the catalog holds
   near-duplicate variants of the same capability. Retrieving *a* variant counts as a capability hit;
   the canonical id rarely wins.
2. All 6 negative queries return some tool in **both** backends. The system has **no unknown-tool
   rejection**; that must not be claimed.
3. Keyword improves from 64 to 128 tools (0.69 → 0.78) because more variants of the right capability
   exist. Scale alone does not favour embeddings.

## 5. New context workloads (five topologies)

| Topology | Required deps | Present | Irrelevant injected | Layout equality (stable / naive) | Select+render |
|---|---|---|---|---|---|
| chain4 | 3 (transitive) | 1 | 0 / 0 | true / true | 0.057 ms |
| fork_join | 2 | 2 | 0 / 0 | true / **false** | 0.053 ms |
| multi_dep | 3 | 3 | 0 / 0 | true / **false** | 0.056 ms |
| no_dep | 0 | — | 0 / 0 | true / true | 0.037 ms |
| with_irrelevant | 1 | 1 | 0 / 2 available | true / true | 0.060 ms |

Aggregate: dependency coverage 0.7778 (7/9), irrelevant blocks injected **0**, stable equality 5/5,
naive equality 3/5.

The one gap is a **semantic, not a bug**: `TaskScopedSelector` includes *direct* dependencies only, so a
4-node chain's leaf receives `c` but not `a`/`b`. Multi-stage DAGs must therefore carry forward what
downstream needs, or the plane needs transitive closure (a design change, not made here).

Dual handoff, trajectory-gap and verification workloads were **not** re-run in this stage; they stay
`INSUFFICIENT_EVIDENCE` with the Final-3 evidence only.

## 6. Neutral backend (R-10) — adapter proven, container blocked

`experiments/evaluation/final4/neutral_adapter.py` runs the six baseline tools through an external
backend inside unmodified AnyCode 0.9.0 (`NeutralEngine.build_agent` → upstream `Agent` +
`ToolRegistry` + `ToolExecutor`). Ten tests prove: each tool really writes/reads/edits/greps/lists/execs
inside the backend root, a `../` escape is refused, and the agent is built with exactly the six tools.

No Atlas gate, ledger, context plane, disclosure, router or recovery is involved. Container parity is
still **`BASELINE_PARITY_BLOCKED`**: `opensandbox` raises `SandboxCredentialError` here, so no real
sandbox can be created and no native-in-container run exists.

## 7. Live model ablations

**Not executed.** No `DEEPSEEK_API_KEY` in this workspace and no `.env`; credentials were not read from
the sibling worktree. Tool-disclosure, context-on/off and budget/handoff ablations are therefore
`LIVE_PROVIDER_BLOCKED`, not "no effect".

## 8. Applicability conditions (current)

| Mechanism | Applies when | Does not apply when |
|---|---|---|
| Context Plane (`task_scoped`) | multi-task DAG with declared direct dependencies | single-task runs; transitive-only needs |
| ContextPilot dedup | multi-line blocks that chunk into ≥2 segments ≥40 chars | single-line blocks; low duplicate ratio |
| Semantic search | ≥64 tools, paraphrased queries, cold-start cost acceptable | latency-critical loops; when unknown tools must be rejected |
| Progressive disclosure | large searchable catalog | tiny catalogs where one extra turn dominates |
| Shared budget | multi-worker runs | single-worker (no shared state) |
| Ownership enforcement | shared write surfaces | read-heavy tasks with shell invocations |

## 9. Modules still without current benefit evidence

A1-08 compaction, A1-09 ingress offload, A3-06 dual handoff, A3-10 worker contract, A4-12
reattachment, A4-16 trusted recovery session, A5-09 oracle parity: `INSUFFICIENT_EVIDENCE` (Final-3
deterministic evidence only). A3-09 ownership remains NEGATIVE. All live-dependent claims remain
`BLOCKED`.
