# Research History Index

The final candidate branch does not carry the code of every historical stage. This file records
where each stage still exists, so nothing was "deleted because it failed".

Rules used here:

- Historical material stays on its own branch and commit; it is never force-deleted from history.
- Reports that were published stay in `experiments/results/` and `docs/stage-*.md`.
- Analysis scripts that produced those reports live on the branch named below, not on the candidate
  branch. Reproducing a historical number means checking out that branch.
- `docs/stages/**` is a frozen archive of stage specifications and verification notes. It is kept
  for provenance and is not maintained.

## Branch map

```
master (local 07e18ec, 22 commits ahead of origin/master 3fbf3dd)
 └─ feat/stage-g1-r0-prerequisites        7bd0965   G1 handoff, G2a budget, R0 router
    └─ experiment/r0-5-positive-delegation 2183983   R0.5 positive delegation
       └─ analysis/r1-0-failure-attribution a8dde65  R1-0 failure attribution
          └─ fix/r1-1-worker-ownership-integrity 4ad5264  R1-1 ownership + trace integrity
             └─ feat/r1-2-deliverable-verification e90d53c  R1-2A verification
                └─ fix/r1-2h-oracle-sandbox-isolation facca98  R1-2H oracle isolation
                   │  ← base of refactor/final-runtime-consolidation
                   ├─ experiment/b0-commerce-agent-bench a8ce550
                   │  └─ fix/b0-r2-first-native-agent 31ef9fc
                   │     └─ fix/b0-r3-business-completion-contract 25afded
```

## Stage index

| Stage | Branch / commit | Report | Headline result | Why it is not on the default path |
|---|---|---|---|---|
| V1 Stages 0–6 | `master` (`a527796`, `35b5774` for the E5 rerun) | `experiments/results/stage1..6-*.md`, `docs/stages/00..06-*.md` | Explicit context blocks + stable rendering; dedup cuts prompt tokens up to 44 % but does not cut warm cache miss | V1 context plane is retained only as prompt rendering; the cache-aware claim was not supported |
| Stage E1 | `master` (`50759ed` harness) | `experiments/results/stage-e1-report.md` | 12 tasks × 3 reps: single 28/36 vs static DAG 3/36, cost 4.12× | Static decomposition lost on every family measured |
| Stage E2 | `master` (`85b3918`) | `experiments/results/stage-e2-report.md` | 8 workflows: no token/cost/cache win; dedup 0/72 calls | Negative result |
| Stage G1 | `feat/stage-g1-r0-prerequisites` (`7bd0965`) | `docs/stage-g1-report.md` | Dual handoff mechanism PASS, success 2/6 vs 1/6 | No demonstrated benefit; kept as opt-in |
| Stage G2a | `7bd0965` | `docs/stage-g2a-report.md` | 0/4 vs 2/4 budget overruns; total tokens −24 %; both arms 0/4 success | Capability kept (shared budget), benefit unproven |
| Stage G2b-0 | read-only analysis | `docs/stage-g2b-diagnosis.md` | Prompt 6.59× but cost only 4.12×; historical-compaction upper bound 31.7 % | Diagnosis only; the prediction was later disproved |
| Stage G2b-1 | `7bd0965` | `docs/stage-g2b1-report.md` | Prompt +9.3 %, cache miss +88 %, cost +43 % | Compaction defaults off |
| Stage G3-0 | read-only analysis | `docs/stage-g3-failure-audit.md` | 16/36 turns were oracle probes (44 %); offload upper bound 19.9 % | Diagnosis only |
| Stage G3-1 | `7bd0965` | `docs/stage-g3-1-report.md` | Oracle 2/4 → 4/4, `side_effect_unknown` 2 → 0 | The fix itself **is** on the default path |
| Stage G3-2 | `7bd0965` | `docs/stage-g3-2-report.md` | Prompt +3.7 %, cost −0.7 %, `max_turns` workers 0 → 4 | Ingress offload defaults off |
| Stage G3-3 | `7bd0965` | `docs/stage-g3-3-report.md` | Oracle 2/6 vs 2/6, prompt +15 %, turns +27 % | Worker contract stays an evaluation mode |
| Stage G3-4a | `7bd0965` | `docs/stage-g3-4a-report.md` | Isolation PASS; single worker 6/6 vs multi 2/6 | Confirms the single-worker default |
| Stage R0 | `7bd0965` (worktree) | `docs/stage-r0-routing-report.md` | 12/12 tasks rejected for delegation; 0 live parallel admissions | Router is opt-in |
| Stage R0.5 | `experiment/r0-5-positive-delegation` (`2183983`) | `docs/stage-r0-5-positive-delegation.md` | Parallelism real (20/20 overlaps) but cost +40 %/+269 % | Delegation defaults off |
| Stage R1-0 | `analysis/r1-0-failure-attribution` (`a8dde65`) | `docs/stage-r1-0-failure-attribution.md` | `result.json` lost 38 % of actions; context sufficiency rejected as a hypothesis | Analysis kept (`stage_r1_0_analysis.py`) |
| Stage R1-1 | `fix/r1-1-worker-ownership-integrity` (`4ad5264`) | `docs/stage-r1-1-worker-ownership-integrity.md` | Enforce 1/16 vs baseline 7/16 oracle | Ownership defaults off |
| Stage R1-2A | `feat/r1-2-deliverable-verification` (`e90d53c`) | `docs/stage-r1-2-deliverable-verification.md` | 66 deliverables, 0 false positives, 0 model tokens | Verification is evaluation-only |
| Stage R1-2H | `fix/r1-2h-oracle-sandbox-isolation` (`facca98`) | `docs/stage-r1-2h-oracle-isolation.md` | 28/28 score agreement, ~6 s and 0 tokens per grading | **Merged into the candidate branch** |
| Stage B0 | `experiment/b0-commerce-agent-bench` (`a8ce550`) | `docs/stage-b0-commerce-agent-bench.md`, `docs/stage-b0-environment-resolution.md` | Official image, Slack mock service and native grader verified | Business-domain track stopped |
| Stage B0-R2 | `fix/b0-r2-first-native-agent` (`31ef9fc`) | `docs/stage-b0-r2-first-native-agent.md` | First real agent run on a native business task failed | Not part of the coding runtime |
| Stage B0-R3 | `fix/b0-r3-business-completion-contract` (`25afded`) | `docs/stage-b0-r3-business-completion-contract.md` | Frozen public task inputs, raw artifacts untracked, completion contract designed but **not implemented** | Track closed by decision, not by result |

## Moved files (old paths still appear in historical reports)

| Old path | New path | Reason |
|---|---|---|
| `experiments/uat_cli.py` | `src/atlasos/app/coding.py` | The coding composition is runtime code; the application calls it in-process instead of spawning a subprocess into the evaluation tree |

Historical reports that reference `experiments/uat_cli.py` (for example
`experiments/results/v2-user-acceptance.md` and `docs/verification/v2-system-verification.md`) still
describe the behaviour correctly; only the path is stale.

## Why the business-benchmark track stopped

The Commerce Agent Bench adapter worked: the upstream image built, the native Slack mock service ran,
the native grader reproduced its own self-test, and one real Atlas worker executed a native task. The
run failed, and no formal A0/A1 experiment was ever run.

The track was stopped because it does not test the runtime. A second industry domain needs new
adapters, new fixtures and new graders, and the result would still be "one agent, one task". The
project's remaining question — whether the runtime is *reliable* — is testable with the coding
benchmark that already exists, an independent oracle, and a real single/multi-worker dataset.

The adapter code and its evidence are preserved on the B0 branches.

## Data handling in the historical branches

`fix/b0-r2-first-native-agent` tracks 239 raw run files under
`experiments/evidence/stage-b0-r2/runs/` (ledger, transcript, mock state, tmp logs). A secret scan
of those blobs found no credentials, API keys or private keys, but they are still raw execution
records and should not have been published.

`fix/b0-r3-business-completion-contract` removes them from tracking (`1f8125e`) and adds ignore
rules. **The blobs remain in the B0-R2 history**, so anyone publishing that branch should treat the
raw directories as internal material. Removing them from history would require a rewrite and is
deliberately not done here.

The candidate branch never contained those files.
