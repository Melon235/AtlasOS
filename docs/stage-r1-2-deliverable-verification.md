# Stage R1-2A — Deterministic Deliverable & Integration Verification

**Project:** AtlasRuntime (repository still `Melon235/AtlasOS`, package `atlasos`).
**Scope:** a Host-owned, deterministic verification contract that judges *deliverables*, not runs. No
scheduler, budget, router, prompt, fixture or oracle change. R1-1 ownership stays `off` by default.

R1-1 showed that a strict write boundary is enforceable but incomplete, and that workers stop with
budget exhausted while their files exist and are wrong. R1-2A asks the next question: can the runtime
say, deterministically and safely, *what exactly is missing or inconsistent in a deliverable* — and
how much of a failing run can it explain without looking at the hidden oracle?

**Answer in one paragraph.** Yes, with two boundaries. Static verification (presence, AST structure,
signatures, stubs, frozen-document integrity, JSON business schema, arithmetic, hard limits, evidence
references) runs on the Host in about **8 ms** per deliverable and produced **zero false positives**
in 58 historical exports. Public-contract examples can only be executed inside a **disposable
sandbox**: that path detects real defects the static checks miss (3 of 3 failing deliverables in the
final R1-1 round, naming the exact published rule), costs about **5 s** and no model tokens, and again
produced **zero false positives**. It still misses defects that the published contract does not
constrain: the historical `to_epoch` epoch bug family passes the published `to_epoch("1970-01-01") == 0`
example and is only caught when the worker also breaks the origin. Verification therefore raises
deliverable trust but is not a substitute for the oracle, and one feedback round would mostly amplify
the public contract rather than the hidden one — R1-2A stops here.

---

## 1. Source audit

Verified in the current tree:

| Question | Answer |
|---|---|
| Where is `WorkerResult` formed? | `experiments/evaluation/runner.py::run_sample` builds `worker_results` from the worker store; `AnyCode` `AgentRunner` supplies the per-worker `RunResult`. |
| Where is the workspace exported? | `runner.py` → `export_patch()` then `export_workspace(destination=run_dir/"workspace")` (`src/atlasos/sandbox/export.py`). |
| Where is the patch produced? | `src/atlasos/sandbox/export.py::export_patch`; the current OpenSandbox transport flattens newlines, so the patch cannot be parsed per file (R1-1 measured this). |
| Where does the oracle run? | `experiments/evaluation/oracle.py::evaluate_oracle`, after the export, on the Host. Hidden tests are copied into `run_dir/oracle-workspace` and executed with `subprocess pytest` **on the Host**. |
| How are runtime success and oracle pass related? | `failure_class` is set by execution, then overridden to `ORACLE_FAILURE` if the oracle fails; `success = failure_class == "TASK_SUCCESS" and oracle.passed`. |
| How is `required_artifacts` used today? | Only as an oracle presence check (`all(artifact in current)`) and as the R1-1 deliverable metric. Nothing inspects the artifact's content. |
| Where are the P2/P3 public contracts? | The visible stub docstrings in `stage_r05_tasks.py` (`_ENTRIES_STUB`, `_MONEY_STUB`, `_CLOCK_STUB`, `_REPORT_STUB`) and the staged `docs/INTERFACE.md`. |
| How does R1-1's trace relate to workers? | Proposal/decision/observation events carry `worker_id` + `task_id` in `TrajectoryCorrelation`; R1-2A does not touch that path. |
| Which tasks have no public tests? | All of them: every benchmark ships `hidden_files` only. P1 publishes a report *format*, P2 publishes docstrings, P3 publishes an interface document. |
| Which steps could leak the oracle? | Only `oracle-workspace` (Host-only) and `oracle.log`; the exported `workspace/` never contains hidden files. The verifier reads `workspace/` only. |

Insertion point chosen: **after the workspace export, beside the oracle** — `runner.py` computes the
oracle first, then (only in `observe`) runs the verifier against the same exported snapshot. The two
never share state.

## 2. Responsibility boundary

| Layer | Owns | Never does |
|---|---|---|
| Oracle (`oracle.py`) | Ground truth for scoring, hidden tests, workspace scope | Run worker-visible checks, feed back to workers |
| Verifier (`atlasos/verification`) | Decidable deliverable facts: presence, structure, published examples, schema, arithmetic, hard limits, evidence registration | Decide run success, execute untrusted code on the Host, read the oracle |
| Runner | Orchestration, persistence, metrics | Interpret verification as a score |

`verification_overall` is therefore reported next to — never instead of — `execution_status`,
`budget_status` and `oracle_status`.

## 3. VerificationSpec design

`src/atlasos/verification/models.py`:

```python
VerificationSpec(spec_id, version, deliverable_type, required_artifacts,
                 checks: tuple[CheckSpec, ...], provenance, public_contract_digest,
                 notes, declared_digest)
CheckSpec(check_id, category, kind, artifact, params, authoritative)
```

* Four categories: `PRESENCE`, `STRUCTURAL`, `PUBLIC_CONTRACT`, `INTEGRATION`; four statuses:
  `PASS`, `FAIL`, `UNKNOWN`, `NOT_APPLICABLE`.
* `spec.digest` is a canonical SHA-256 over the spec payload; `declared_digest` is set once by
  `with_declared_digest()`. The engine refuses to run a spec whose digest does not match
  (`SPEC_DIGEST_MISMATCH` on every category) — a worker or a corrupted record cannot edit its own
  acceptance criteria.
* Aggregation is worst-status-wins per category (`FAIL > UNKNOWN > PASS > NOT_APPLICABLE`), and the
  categories are never averaged into one accuracy number.
* `authoritative=False` marks a rule that cannot be decided deterministically (prose semantics,
  evidence *support*). The engine reports those as `UNKNOWN`, never as `PASS`.

## 4. Trusted rule sources

| Spec | Rules derive from | Provenance label |
|---|---|---|
| P1 reports | The report line format and constant list published in the worker prompt | `public_contract_document` |
| P2 modules | The visible stub docstrings (`format_cents(123450) -> "$1,234.50"`, `split_evenly(10, 3) -> [4, 3, 3]`, …) | `visible_deliverable_docstring` |
| P3 pair | The staged `docs/INTERFACE.md` (`to_epoch("1970-01-01") == 0`, `from_epoch(0) == "1970-01-01"`, `summarize([...]) == "2023-12-31|2024-01-02|12"`) | `public_contract_document` |
| Business | Host-owned synthetic procurement spec (budget 800 000, delivery ≤ 90 days) | `trusted_host_declaration` |

No rule in `experiments/evaluation/stage_r1_2_contracts.py` reads `hidden_files`. The module does not
import `oracle.py`; a unit test enforces that the contracts module never mentions hidden fixtures.

## 5. Code deliverables

* P2: presence, AST parse, required functions, signatures, `NotImplementedError`/empty-body stubs,
  forbidden cross-module import, plus 17 published examples executed in a sandbox.
* P3: the same for `providers/clock.py` and `consumers/report.py`, the consumer's import binding to
  `providers.clock`, the frozen `docs/INTERFACE.md` digest, plus 6 published examples.
* Signature comparison ignores type annotations on purpose: the fixtures promise parameter names,
  positions and defaults, and a worker may legitimately drop hints.

## 6. Structured business deliverables

`experiments/evaluation/stage_r1_2_business.py` ships a synthetic supplier review
(`reviews/supplier_review.json`) and a spec that checks: required fields and types, `total_cost =
quoted_price + shipping_cost`, numeric ranges, `total_cost <= 800 000`, `delivery_days <= 90`,
and that every `evidence_refs` entry names a registered source (`quote_a`, `spec_a`, `spec_b`).
Whether a citation *supports* the claim is not decidable here and is reported as `UNKNOWN`
(`EVIDENCE_SEMANTICS_UNVERIFIED`). No real supplier data is used.

This path shares nothing with Python deliverables except the spec/result types, which is the point:
the same interface will serve the later business feasibility benchmark.

## 7. Integration

* Static: `integration.import_binding` proves the consumer imports the contracted provider names.
* Dynamic: the P3 public examples import `consumers.report.summarize`, which imports
  `providers.clock`, so the pair is exercised together inside the sandbox.
* Where a contract has no shared interface (single-worker tasks), the category reports
  `NOT_APPLICABLE` rather than a guessed PASS.

## 8. Sandbox safety model

`src/atlasos/verification/dynamic.py`:

* A **fresh** sandbox is created for verification (`run_id="…-verify"`, `network="none"`,
  `persistent=False`, principal `system:verification`, no secret reference). It is not the worker
  sandbox and it is destroyed in a `finally` block.
* The exported snapshot is uploaded via `write_file`; a Host-authored `test_public_contract.py`
  contains only published rules. Bounded upload: ≤400 files, ≤2 MB each.
* Host code never imports or calls the artifact. `code_checks.py` only parses ASTs.
* Timeout, provider error or an unexpected exit code maps to `UNKNOWN`
  (`DYNAMIC_VERIFICATION_TIMEOUT` / `DYNAMIC_VERIFICATION_ERROR`) — never to a functional `FAIL`.
* If no runner is configured (the evaluation runner's observe mode), dynamic checks report
  `DYNAMIC_VERIFICATION_UNAVAILABLE` and the category stays `UNKNOWN`.

## 9. Oracle isolation

* The verifier receives only: the spec, the exported `workspace/` and the fixture digests derived
  from the *task definition* (not from the run under judgement).
* `experiments/evaluation/stage_r1_2_verify.py` reads `manifest.json` (oracle pass/fail) **after**
  verification, purely to build the post-hoc confusion matrix. Nothing flows the other way.
* No rule was edited after seeing an oracle result; the only post-hoc changes were the mechanical
  fixes listed in §12, all re-run from scratch.
* Two tests enforce the boundary: the contracts module is parsed and asserted to contain no import
  of the oracle and no `hidden_files` access, and verifying a historical run directory is proven to
  leave every file byte-identical.

## 10. Detection results

### Static sweep — every exported workspace from R1-1 and R0.5 (58 verified, 2 unavailable)

| Area | true_positive | true_negative | false_positive | false_negative | unknown | not_applicable |
|---|---|---|---|---|---|---|
| deliverable_presence | 0 | 12 | 0 | 46 | 0 | 0 |
| structural_validity | 23 | 12 | 0 | 23 | 0 | 0 |
| public_contract_status | 0 | 0 | 0 | 0 | 58 | 0 |
| integration_status | 7 | 6 | 0 | 11 | 0 | 34 |
| verification_overall | 23 | 0 | 0 | 0 | 35 | 0 |

Reading: presence alone never explains a failure (46 misses) — that is exactly the "file exists ≠
correct" rule. Structure explains 23 of the 58 failures with no false alarm. Public contract is
`UNKNOWN` by construction because the sweep ran with `--dynamic off`.

### Dynamic sweep — final R1-1 round, all 8 runs verified with a real sandbox (≈5 s each)

| Area | true_positive | true_negative | false_positive | false_negative | unknown |
|---|---|---|---|---|---|
| deliverable_presence | 0 | 3 | 0 | 5 | 0 |
| structural_validity | 1 | 3 | 0 | 4 | 0 |
| public_contract_status | 3 | 3 | 0 | 2 | 0 |
| integration_status | 0 | 1 | 0 | 3 | 4 |
| verification_overall | 3 | 3 | 0 | 2 | 0 |

Detected defects (verbatim from the findings):

| Run | Verdict | Published rule that failed |
|---|---|---|
| p2 rep1 O0 | FAIL | stubs `parse_entry`/`total_for`/`top_memos`; 8 of 17 examples |
| p2 rep1 O1 | FAIL | `parse_cents.bare_integer`, `split_evenly.negative` |
| p3 rep1 O1 | FAIL | `to_epoch.epoch_origin`, `from_epoch.epoch_origin` — the historical `306` bug |

Missed defects (false negatives): both p3 **rep0** runs, where the worker's `to_epoch` is wrong only
for mid-range dates. The published document pins `to_epoch("1970-01-01") == 0` and the rejection
cases; it says nothing about `2000-03-01`. Verification is honest here instead of guessing.

## 11. False positives / false negatives

* **False positives: 0** in all 66 verified deliverables (58 static + 8 dynamic). No correct
  deliverable was marked FAIL in any category.
* **False negatives**: 46 presence, 23 structural/static, 2 public-contract/dynamic. Presence misses
  are structural by definition; the structural misses are artifacts that parse and expose the right
  signatures but compute the wrong values; the 2 dynamic misses are the P3 mid-range date family.
* No UNKNOWN was excluded from the tables. UNKNOWN is a reported class, never merged into PASS.

## 12. Fixes made during the stage (recorded, not hidden)

1. `p2.entries.implemented` false positive: `top_memos` written as a single-expression body was
   classified as a stub while `ast` saw a non-trivial body. The stub rule now requires an actual
   `raise NotImplementedError` or an empty/pass/`...`/docstring-only body.
2. `parse_pytest_failure` could not read failures because the sandbox transport returns stdout as a
   single line; the marker is now searched with a regex.
3. `verification_cpu_ms` used the wall clock; it now uses `time.process_time()`.
4. P3 first attempt treated `frozen_contracts` as write grants — corrected in R1-1 and re-used here
   as read-only exclusions.

## 13. Verifier cost

| Mode | Wall time per deliverable | CPU time | Model tokens | Sandbox |
|---|---|---|---|---|
| static (offline sweep) | mean 8.1 ms, max 29.9 ms | < 20 ms | 0 | none |
| dynamic (real sandbox) | mean 4.99 s, max 5.29 s | < 20 ms | 0 | one fresh sandbox per run |
| observe inside a live run | 6.0 – 12.8 ms | 0 – 15.6 ms | 0 | none |

Verification has no token cost but it is not free: the dynamic path pays a container round trip
(create + upload + exec + destroy), which is why it stays an explicit opt-in.

## 14. Observe-mode non-interference

`experiments/evaluation/stage_r1_2_rounds.py` runs `v0` (`verification_mode=off`) and `obs`
(`verification_mode=observe`) on the same two tasks with identical fixed conditions
(DeepSeek Chat, T=0, OpenSandbox, budget 120 000, 14 turns, 60 tool calls, handoff dual, guarded
budget, tool-error corrected, compaction off, offload off, ownership off, integrity record).

* The two `RunnerSettings` objects are byte-identical except `verification_mode`
  (checked field-by-field; digests `b18bd8e4…` vs `b10f6837…`).
* Off runs produced no verification files. Every observe run wrote `verification.json` +
  `verification_summary.json`.
* Verification never altered `failure_class`, `success`, `worker_results`, tool calls or the
  workspace: a dedicated test compares the export before/after verification.

Live round (4 tasks × 2 arms, 1 repetition):

| Arm | Oracle pass | Cost (USD) | Model turns | Verification |
|---|---|---|---|---|
| v0 (off) | 2 / 4 | 0.03810 | 12, 20, 7, 21 | none |
| obs (observe) | 3 / 4 | 0.03559 | 14, 24, 7, 22 | `UNKNOWN` × 4 (dynamic not wired in the runner) |

The 1-of-4 oracle difference is model variance at n=4; this round is a compatibility check, not an
effect measurement. One observe run recorded the legitimate mixed state
`oracle=False` + `verification=FAIL` (structural), and one baseline run recorded
`BUDGET_EXCEEDED` + `oracle=True` — both persist side by side.

## 15. Budget / recovery compatibility

* Verification runs after the export, so a `BUDGET_EXCEEDED` worker still gets verified. A test
  covers exactly this case (`test_observe_mode_verifies_a_budget_stopped_delivery`).
* The verifier never writes into the workspace, never touches M4/M5/M6/M7 state and never changes
  `stop_reason`; no recovery path was modified.
* Concurrency: each run gets its own snapshot and its own spec instance; no shared mutable state
  exists between two concurrent `verify_async` calls (`artifact_digest` is per snapshot).

## 16. R1-2B readiness

| Precondition | State |
|---|---|
| Detects real historical defects | yes (3/3 failing deliverables in the dynamic round; 23 structural) |
| No severe false positives | yes (0 in 66) |
| Separates interface defects from environment defects | yes (binding vs `DYNAMIC_*` codes) |
| Dynamic verification can run isolated | yes (fresh network-none sandbox, destroyed) |
| Public, worker-actionable failure text | yes (`failing public rule(s): parse_cents.bare_integer, …`) |

Every precondition is met *except* one that matters for value: the published contracts are weaker
than the hidden ones (P3 mid-range dates), so a single feedback round would mostly restate examples
the worker already had. **Recommendation: do not start R1-2B yet.** If it is started, restrict it to
tasks whose public contract has been strengthened by a trusted Host (e.g. publishing a mid-range
example), and account for the extra turn and tokens explicitly.

## 17. Regression

| Check | Result |
|---|---|
| `pytest -q` | exit 0 — full suite green (0 failed, 1 skipped live marker) |
| `ruff check .` | All checks passed |
| `ruff format --check .` | 385 files already formatted |
| `npx pyright` | 0 errors, 0 warnings |
| R1-2 tests | 54 new tests (engine, code, business, modes, isolation) all green |
| M3/M4/M5/M6/M7, G1, G2a, R0, R0.5, R1-0, R1-1 suites | unchanged and green |

The suite is run with `--basetemp` outside the tree because this host's shell wrapper intercepts
pytest's own bulk temp deletion; that is a host artifact, not a test result.

## 18. CI status

**Unknown.** The branch is pushed; no CI run was observed because the pull request was not created
from this environment (`gh` and no GitHub token). Nothing here claims a green CI.

## 19. GitHub

Branch `feat/r1-2-deliverable-verification`, based on the R1-1 head
`fix/r1-1-worker-ownership-integrity` (`4ad5264`). Four commits: verification engine, observation
integration, tests, docs. No force push, no `git add .`, no secrets, no provider payloads, no
historical result rewritten. **R1-1's PR still does not exist** — see the R1-1 report; the stack is
therefore two branches deep with one open PR missing.

Open this one at
<https://github.com/Melon235/AtlasOS/pull/new/feat/r1-2-deliverable-verification> with base
`fix/r1-1-worker-ownership-integrity`.

## 20. Next stage

1. **R1-3 Budget Economics** — the dominant failure class across R0.5/R1-1 is still
   `BUDGET_EXCEEDED`; verification now gives a cheap way to tell "stopped but complete" from
   "stopped and broken".
2. **R1-2B (conditional)** — only after `docs/INTERFACE.md` and the P2 docstrings carry enough
   published examples to make a single feedback round meaningful.
3. **Workspace isolation** — unchanged from R1-1: still not justified by data.

---

## Verdicts

| Area | Verdict | Basis |
|---|---|---|
| A. Verification contract integrity | **PASS** | digested spec, tamper → FAIL, duplicate check ids rejected, `authoritative=False` never passes |
| B. Deliverable presence detection | **PASS** | missing file/empty file/wrong type/digest mismatch all FAIL; 0 FP; presence alone explains no defect by design |
| C. Structural validation | **PASS** | AST parse, function set, signatures, stubs, forbidden imports, frozen document; 23 TP, 0 FP |
| D. Public contract validation | **PARTIAL** | real detection in a sandbox (3 TP), but limited to published examples and not wired into the runner |
| E. Integration validation | **PASS** | static import binding + dynamic provider/consumer execution; `NOT_APPLICABLE` when no shared interface exists |
| F. Business artifact compatibility | **PASS** | schema, arithmetic, ranges, budget/delivery limits, registered evidence refs; semantics stay UNKNOWN |
| G. Sandbox safety | **PASS** | fresh network-none sandbox, no host execution, bounded upload, destroy in `finally` |
| H. Oracle isolation | **PASS** | no import, no hidden fixture, contract from visible sources; oracle read only after verification |
| I. Runtime noninterference | **PASS** | arms differ by one setting; off adds nothing; observe only adds files; verdicts and workspace untouched |
| J. Detection quality | **PARTIAL** | 0 false positives, 3/5 explainable failures in the dynamic round; the P3 mid-range family stays undetected by design |
| K. Execution overhead | **PASS** | 8 ms static, ~5 s dynamic, no tokens; measured, not estimated |
| L. GitHub readiness | **PARTIAL** | clean branch and scoped commits; PR not created, CI unknown |

## The seven questions

1. **Can AtlasRuntime reliably identify an incomplete worker deliverable?** Yes for the decidable part:
   missing/empty artifacts, missing functions, stubs, signature drift, forbidden imports, changed
   frozen documents and invalid business artifacts are all detected deterministically with no false
   positives.
2. **Can it detect public interface and integration errors?** Yes — 3 of 3 failing deliverables in the
   sandbox round were attributed to named published rules, including the consumer/provider binding.
3. **Can it verify a structured business deliverable without code tests?** Yes — schema, arithmetic,
   hard limits and evidence registration are checked on a synthetic procurement artifact; only
   semantic evidence support stays `UNKNOWN`.
4. **Are there severe false positives?** No. Zero in 66 verified deliverables. The one internal false
   positive found while building the engine (a single-expression `top_memos`) was fixed and recorded.
5. **What does verification cost?** 8 ms and no tokens statically; ~5 s and no tokens per deliverable
   when published examples are executed in a sandbox; 6–13 ms inside a live run in observe mode.
6. **Is one feedback round worth it?** Not yet. Detection is precise but the published contracts are
   thinner than the hidden ones, so feedback would mostly repeat what the worker already read.
7. **Does this raise deliverable trust?** Yes, but as an explanation layer: it converts "the oracle
   failed" into "these published rules are violated", while never replacing the oracle, the execution
   status or the budget status.
