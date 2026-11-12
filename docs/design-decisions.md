# Design Decisions

Each entry states the decision, the alternative that was rejected, and the evidence that drove it.
Quantitative sources are listed in `docs/evaluation-summary.md`.

---

## D1. AnyCode is the runtime base; AtlasRuntime adds durable execution control

**Decision.** Depend on `anycode-py==0.9.0` for the agent loop, DAG scheduling, checkpoints,
provider adapters, tool execution and the run-store transcript.

**Rejected.** Writing an agent engine, or forking AnyCode into the repository.

**Why.** The project question is about execution reliability around an agent, not about
re-implementing an agent. Forking would have destroyed provenance and made every upstream fix a
merge conflict. Every reused capability is documented in `docs/anycode-delta.md` so that no
upstream mechanism is presented as original work.

**Cost of the decision.** The reliability seams must be expressed as subclass overrides
(`AtlasContextEngine.build_agent`, `AtlasAgentRunner._execute_tool_blocks`,
`AtlasToolAgent._apply_budget_outcome`). These are narrow, documented and test-covered, but they
are coupled to upstream signatures.

---

## D2. Software engineering is the only application domain

**Decision.** The runtime targets autonomous coding work: multi-step code changes, multi-module
changes, tool execution, interruption/resume and deliverable verification.

**Rejected.** Generalising to procurement, CRM, email or ticket workflows.

**Why.** The domain reuses what already exists: a coding tool surface, a real sandbox, a coding
benchmark with an independent oracle, and real single/multi-worker results. Adding business
adapters would not test the runtime; it would test the adapter. A commerce-agent-bench adapter was
built and reached a first real agent run, then the track was stopped; the work stays on its own
branches (`docs/research-history.md`).

---

## D3. Multi-worker is not the default

**Decision.** The default path is one worker. Multi-worker DAGs remain supported and
configurable, and the README never claims they are better.

**Rejected.** "Default to a multi-agent DAG because it sounds more advanced."

**Why.** Four independent benchmark comparisons agree:

| Comparison | Single worker | Static multi-worker |
|---|---|---|
| Stage E1 (12 tasks × 3 reps, n=36/arm) | **28/36 (77.8 %)** | 3/36 (8.3 %) |
| Stage G3-4a (3 tasks × 2 reps, n=6/arm) | **6/6** | 2/6 |
| Stage R0 (3 tasks × 2 reps, n=6/arm) | **5/6** | 2/6 |
| Stage R0.5 Track A (n=6/arm) | **6/6** | 2/6 |

Cost and latency move the same way: Stage E1 showed 6.59× prompt tokens, **4.12× cost**,
1.94× p50 latency. Stage R0.5 Track A showed **+40 % cost per run** and **+27 %** end-to-end
latency; Track B **+269 %** and **+59 %**.

The mechanism is not broken — 20/20 multi-worker runs showed real wall-clock overlap, and one
task family (`e1-12-conflict`) consistently favoured the multi-worker arm. The conclusion is that
static decomposition multiplies context and turns faster than it removes work.

---

## D4. Selective delegation exists but stays off

**Decision.** Keep `workers/routing.py` (DIRECT vs DELEGATE_PARALLEL, with explicit rejection
reasons) as an opt-in policy, default `direct`.

**Why.** Stage R0 rejected **12/12** candidate tasks for delegation, so no live parallel admission
ever happened. Stage R0.5 forced positive admissions and got real parallelism — and still lost on
correctness, cost and latency. A router whose admission quality cannot be demonstrated must not be
the default.

**What would change the decision.** A task family where the parallel arm wins on oracle score
*with* flat cost. That is the subject of the proposed main experiment (`docs/final-consolidation-report.md` §17).

---

## D5. Context engineering is kept, its benefit claim is withdrawn

**Decision.** Keep the explicit context model and the stable-prefix renderer, because prompt
construction needs them. Withdraw the claim that cache-aware layout reduces cost.

**Why.**
- Stage E2: the task-scoped + stable configuration moved prompt tokens **+0.8 %**, cost +1.2 %,
  correctness unchanged (23/24 → 23/24). ContextPilot dedup removed **0 blocks in 0/72 calls**
  because dependency outputs are short.
- Stage 6 (live DeepSeek): dedup reduced prompt tokens by up to **44 %** at high duplicate ratio,
  while warm cache-miss reduction was **−55.7 % / −18.8 %** (small), **−28.4 % / −42.5 %**
  (medium) and **+2.5 % / +11.1 %** (large) — i.e. it sometimes made prefill *worse*.

A shorter prompt is not a cheaper prompt. The measurement contract (hit tokens, miss tokens,
latency, cost per call) is kept, because it is what exposed the mistake.

---

## D6. History compaction and ingress offload are off by default

**Decision.** `compaction=off`, `tool_result_mode=off`.

**Why.** Both mechanisms are safe (0 archive failures, 0 digest mismatches) and both failed as
optimisations:

| Stage | Mechanism | Effect on total prompt | Effect on cost |
|---|---|---|---|
| G2b-1 | rolling history compaction | **+9.3 %** | **+43 %** (cache miss +88 %) |
| G3-2 | ingress offload | +3.7 % | −0.7 % (noise) |

The predicted 31.7 % upper bound from the G2b-0 diagnosis did not materialise, because rewriting
history breaks the provider cache prefix. In both stages the model never called `history.read`,
so the recovery path is proven only by deterministic tests.

---

## D7. Structured worker contracts stay in the evaluation layer

**Decision.** `worker_contract` is an evaluation mode, not a runtime feature.

**Why.** Stage G3-3: oracle 2/6 vs 2/6, prompt +15 %, turns +27 %. Execution discipline improved
on one task and regressed on another. Encoding an unproven prompt template into the product would
be a hypothesis shipped as architecture.

---

## D8. Ownership enforcement is off by default

**Decision.** `ownership_mode="off"`; the manifest and the enforcement gate stay available.

**Why.** Stage R1-1 (4 rounds, 32 runs): the enforce arm scored **1/16** oracle passes against
**7/16** for the baseline. The damage came from denying interpreter invocations
(`python3 -m pytest`) classified as unclassifiable shell. The mechanism did prevent cross-worker
writes in deterministic tests, but the compatibility cost is not acceptable as a default.

**Honest scope.** Shell coverage is `PARTIAL`; there is no OS-level isolation. The manifest is a
declaration mechanism plus a text-level gate, not a security boundary.

---

## D9. The benchmark oracle runs in its own sandbox

**Decision.** Grade hidden tests inside a disposable, network-disabled sandbox
(`experiments/evaluation/sandboxed_oracle.py`); keep `oracle_mode="host"` only as a comparison
path.

**Why.** The earlier design executed benchmark code in the same process tree that produced the
artifacts. That makes a failing run ambiguous and lets a worker reach host paths. After the change,
28/28 historical workspaces produced **identical** scores, with 0 environment and 0 dependency
differences, at 5.9–6.4 s and **0 model tokens** per grading. The grading is now reproducible and
separable from model behaviour.

**Cost.** One container per grading (~3.1 s of the ~6 s budget). CPU/memory quotas are not pinned.

---

## D10. The application service refuses to reattach sandboxes

**Decision.** `atlas resume` reports `RECOVERY_REQUIRED` and refuses to continue when the only way
forward would be to reattach or destroy a remote sandbox without M5/M6 authority.

**Rejected.** Making the CLI silently reattach, which would produce an unreviewed remote side
effect.

**Why.** Durability is only worth something if recovery respects the same authorization as the
original execution. The operator harness (`trusted_recovery.py`) rebuilds the exact worker and tool
runtime, revalidates the binding digest, and then delegates to the existing suspension resume.

**Cost.** Recovery is not a one-command operation from the CLI. This is a limitation, not a bug —
see `docs/limitations.md`.

---

## D11. SQLite, one host, no distributed state

**Decision.** Ledger, approvals, suspensions, idempotency, sandbox bindings and worker registry are
local SQLite files.

**Why.** V1 scope is a single-node runtime. Distributed consensus, queueing and shared storage are
explicit non-goals; adding them would not be verified by any experiment in this repository.

**Cost.** `RunBudgetState` uses an in-process lock, so the budget is shared per process, not per
cluster. Two processes on the same run directory are not a supported configuration.

---

## D12. Bash classification is a last resort, not an isolation boundary

**Decision.** Keep the shell classifier for the ownership gate, and default enforcement off.

**Why.** There is no way to correctly attribute an arbitrary shell command to a path declaration
without OS-level isolation. The classifier produced 17 `OWNERSHIP_UNCLASSIFIABLE_SHELL` denials in
the final round — correct behaviour for an unclassifiable command, unacceptable as a default for a
coding agent.

---

## D13. Evaluation imports the runtime, never the reverse

**Decision.** The runtime package must not import `experiments/`.

**Why.** Evaluation-only code in the production path is how benchmark-specific behaviour leaks into
a product. The only violation found during consolidation was the application's coding mode, which
shelled out to `experiments.uat_cli`; that path now lives in the runtime package
(`docs/final-consolidation-report.md` §12).

---

## D14. One verification engine, observation only

**Decision.** `verification/` runs on produced artifacts and returns `PASS/FAIL/UNKNOWN`. It never
gates, retries or repairs.

**Why.** Stage R1-2A produced **0 false positives across 66 verified deliverables** and cost
8.1 ms per static check with 0 model tokens. That is a trustworthy detector. A *gate* would need
far better recall: public-contract coverage was 23 true positives against 23 false negatives, so a
blocking verdict would have blocked correct work.

**Consequence.** No completion gate, no reviewer loop, no repair loop exists in this project.
