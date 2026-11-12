# Stage B0-R3 — Business Completion Contract (partial: Phase 0–2)

**Status: PARTIAL.** Phase 0 (evidence containment) and Phase 1 (public input manifest) are done and
green. Phase 2 is audited but **not fixed**. Phase 3–9 (RUN3 termination audit, baseline freeze,
completion contract, feedback gate, C0/C1 experiment) are **NOT DONE** — no completion mechanism was
built and **no C0/C1 run exists**. This document does not claim otherwise.

---

## Phase 0 — raw run artifacts were public

The B0-R2 report said raw records were local-only. That was wrong: **239 raw run files were tracked**
under `experiments/evidence/stage-b0-r2/runs/` (1.51 MiB), including `state/ledger.db` ×18,
`trajectory/*.jsonl` ×36, `reward.json` ×16, `native/workspace/outputs/mock_state/*` ×19 and service
logs ×19.

Audit of the tracked bytes:

| Class | Count | Verdict |
|---|---|---|
| Real provider credential (`sk-…`, `DEEPSEEK_API_KEY=…`) | **0** | none leaked |
| Bearer values | 56 occurrences, **all** the documented `Bearer local-mock-token` | not a secret |
| Files carrying hidden-grader material (`_is_expected`, `checks_breakdown`, `_tol`, `_read_min_get_calls`) | **47** (`reward.json`, `run.json`) | **public leak of grader expectations** |
| Sanitised summaries (`real_a0_runs.json`, `scripted_smoke.json`, `runs_summary.json`) | 3 | clean (ids + pass flags only) |

Actions taken:

1. Raw runs moved to the already-ignored `experiments/runs/stage-b0-r2/runs/` (239 files preserved
   locally).
2. `git rm -r --cached experiments/evidence/stage-b0-r2/runs` — index only, nothing deleted from disk.
3. `.gitignore` gains `experiments/evidence/stage-b0-r2/runs/`; the runner's evidence root now points
   at `experiments/runs/stage-b0-r2` and a separate `PUBLIC_SUMMARY_ROOT` marks what may be published.
4. Only the three audited summaries remain tracked.

**Unresolved risk, needs your decision:** the earlier commit `31ef9fc` still contains those 47
hidden-grader files in history. A normal delete commit does not purge them. Because no real credential
leaked, I did **not** rewrite history and did not force-push. Purging requires an authorised
`git filter-repo`/BFG rewrite of the published branch plus branch protection changes; the benchmark's
own `reward.json` content is derived from its private rubric, so treat it as proprietary upstream data.

## Phase 1 — public input manifest (done)

`read_task_file` previously trusted the workspace tree and only refused `private/`, `grader/`,
`tests/`, `outputs/mock_audit`. `outputs/mock_state/slack_final.json` — created by the native service —
was therefore readable, which is a business-logic bypass: the mock state contains the thread and
supplier facts, so an agent could read the answer-bearing state instead of calling the Slack API.

New `public_inputs.PublicInputManifest`:

* frozen **before** the environment starts, from the pristine `task.workspace` (2 files for this task:
  `api_reference.md`, `state_manifest.md`), with a `sha256` digest over path+content;
* `read_task_file` resolves **only** registered entries; anything else fails closed (unregistered,
  mock state, mock audit, service logs, `private`, `grader`, other runs);
* ownership by canonical containment (`os.path.commonpath` on resolved paths) plus per-component
  `lstat`, refusing drive letters, UNC paths, `..`, absolute paths, symlinks and reparse points;
* a registered file whose bytes changed after freezing is refused (tamper detection);
* the manifest digest is recorded in the run record (`public_inputs`).

Writes keep the two-path allow-list and now use the same containment check instead of a string prefix.

Tests: 22 in `tests/unit/test_b0_r2_business_tools.py`, covering public reads, `./` normalisation,
mock state, mock audit, service log, unregistered file, traversal, absolute, drive letter, UNC, symlink
and digest recording. Scripted smoke re-run after the change: 4 tool calls, both deliverables written,
native **6/7** — the manifest does not break the chain.

**This is a controlled tool-layer boundary, not OS-level isolation.** The container still mounts the
upstream task and private directories read-only.

## Phase 2 — audit findings without fixes

| Item | Finding |
|---|---|
| `max_tool_calls` enforcement | **NOT ENFORCED.** `RunConfig.max_tool_calls = 60` is recorded in the run config and never consulted; the executor has no run-level admission counter. Only `max_turns` and the token budget bind. |
| Token budget rejections → stop reason | not propagated: the business runner computes `budget_exceeded` after the fact from observed usage, it does not surface the hook's rejection. |
| `runtime_success` vs `execution_status` | legal divergence already observed (RUN3: runtime success, deliverables missing). They are recorded separately, which is correct. |
| Failed-worker tool calls in the final result | not recoverable in this runner: tool calls are collected from `agent_results` only, so a failed worker's calls are dropped. |
| M7 terminal event | not asserted by the business runner. |
| R1-1 private payload corpus | not exercised here (no `private/tool_payloads.jsonl` written by this runner). |
| M4 ledger evidence | `ledger.db` is written, but this store version exposes no listing API (`records: null`), so no recomputable ledger evidence is produced. |
| Trace completeness | **INVALID metric.** The runner reported `complete = (recording_failures == 0)`, which is exactly the shortcut the stage forbids. Correct status: **TRACE_INCOMPLETE / NOT_VALIDATED** for all B0-R2 runs. |

Because of the last row, none of the B0-R2 runs carries trustworthy trace evidence, and the
`proposed / completed / failed / denied / unknown / missing_payload / unmatched_event /
terminal_present / input_digest_coverage` set was never computed.

## Phases 3–9 — not started

| Phase | Status | Blocker |
|---|---|---|
| 3 RUN3 termination audit | NOT DONE | needs transcript reconstruction against `summarize_trace`; that trace layer is itself `TRACE_INCOMPLETE` |
| 4 baseline freeze | NOT DONE | baseline must wait for the Phase 2 fixes |
| 5 completion contract | NOT DONE | depends on 2 (resource limits) and 3 (why the worker stops) |
| 6 feedback gate | NOT DONE | depends on 5 |
| 7 deterministic tests | NOT DONE | depends on 5/6 |
| 8 C0/C1 experiment (6 runs) | **NOT RUN** | depends on 5/6 |
| 9 metrics | NOT DONE | no runs |

I stopped here rather than building the completion gate on top of a runner that cannot enforce its own
tool-call budget or prove trace completeness: a feedback loop on that base would produce numbers whose
provenance cannot be defended.

## Regression

| Check | Result |
|---|---|
| `pytest -q` | exit 0 (full suite, 22 tests in the business-tool module) |
| `ruff check .` | All checks passed |
| `ruff format --check .` | 409 files already formatted |
| `npx pyright` | 0 errors, 0 warnings |

## GitHub

Branch `fix/b0-r3-business-completion-contract`, based on `fix/b0-r2-first-native-agent`.
<https://github.com/Melon235/AtlasOS/pull/new/fix/b0-r3-business-completion-contract>.
No PR was created and CI status is **UNKNOWN** (no `gh`, no token).

---

## Verdicts

| Dimension | Verdict |
|---|---|
| Raw artifact containment | **PASS** (index cleaned, local copy kept) |
| Secret audit | **PASS** (no real credential) |
| Hidden-grader exposure | **FAIL for existing history** (needs authorised rewrite), **PASS for new runs** |
| Public input manifest | **PASS** |
| Internal mock-state isolation | **PASS** (tool layer) |
| Run-level tool-call budget | **FAIL** (not enforced) |
| Trace completeness | **FAIL** (invalid metric, `TRACE_INCOMPLETE`) |
| Completion contract / feedback gate | **NOT DONE** |
| C0/C1 experiment | **NOT RUN** |

## Answers to the stage's questions

1. **Does the current ToolResult baseline still end prematurely?** Unknown — no run has been executed
   since the `ToolResult` tightening; the last valid run (RUN3) predates it.
2. **Can C1 reduce missing deliverables?** Not measured; the gate does not exist yet.
3. **Can C1 improve the native score?** Not measured.
4. **What would the extra turns cost?** Not measured.
5. **Does C1 introduce budget exhaustion or duplicate reads?** Unknown.
6. **Is the gate reusable across business tasks?** Undetermined; nothing was built.

Recommendation: fix the Phase 2 gaps first (enforce a run-level tool-call counter through the existing
admission seam, surface budget rejections into `stop_reason`, reuse `summarize_trace`), then freeze the
baseline and build the completion contract on top. Until then, `completion_mode` must not exist: a gate
that cannot see a trustworthy trace would only add cost.
