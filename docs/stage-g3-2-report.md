# Stage G3-2 — Tool Result Ingress Offloading

Round: `g32-20261111T042736Z`
Scope: archive a large tool result **once, at ingress**, before the model ever sees it; hand the
model a bounded preview plus a recoverable reference; never rewrite history that was already sent.

---

## 1. Ingress audit (where a tool result becomes a conversation message)

Verified against the installed AnyCode 0.9.0 and the local AtlasOS working tree:

| Step | Code | What happens |
|---|---|---|
| 1 | `atlasos/sandbox/tools.py` / `atlasos/tools/runtime.py` inner chain | the tool produces `ToolResult(data, is_error, retry_safe)` |
| 2 | `atlasos/actions/gate.py` (M3) | reads the raw `ToolResult` for the action decision |
| 3 | `atlasos/ledger/bridge.py` (M4) | reads the raw `ToolResult`, writes the durable record **including `result_size` of the raw text** |
| 4 | `atlasos/tools/conversation.py::AtlasAgentRunner._run_single_tool_block` (M7) | records `tool_observation.result_digest/result_size` from the block it is about to return |
| 5 | `anycode/core/runner.py:944-958` | `ToolResultBlock(tool_use_id=block.id, content=result.data, is_error=result.is_error)` |
| 6 | `anycode/core/runner.py:731-737` | `tool_msg = LLMMessage(role="user", content=result_blocks)`; `conversation.append(tool_msg)` |
| 7 | next turn, `anycode/core/runner.py:322` | `conversation = await self._hook_runner.run_before_turn(...)` → M6 snapshot hook, then G2a budget hook (`anycode_engine.py:105-123`) |

There is **no hook between step 5 and step 6**, and `AgentRunner` must not be copied or
subclassed. The only AtlasOS-owned boundary that runs after the real execution and before the block
exists is step 5's input: the executor AtlasOS hands to the runner.

**Chosen ingress point:** `AtlasDispatchExecutor.execute` (`atlasos/tools/dispatch.py`), which wraps
the whole M3 → M4 → AnyCode `ToolExecutor` chain (`runtime.py:205-235`). It receives the final
`ToolResult` and its return value is exactly what step 5 shapes into `result.data`.

```
dispatch.execute(tool, input, ctx)
  → inner.execute(...)            # M3 decision, M4 ledger, AnyCode validation + execution
  → ingress.offload_async(result) # NEW: archive + preview, or pass through
  → runner builds ToolResultBlock from the returned object
```

Rejected alternatives: a `before_turn` scan (that is Stage G2b-1 rolling compaction and rewrites
already-sent history); copying `AgentRunner`; a second `ToolExecutor`.

---

## 2. Why no already-sent history is rewritten

* The offloader is a pure function of one `ToolResult`; it is called once per tool call, before the
  block is created. The conversation list is never scanned, never re-selected, never mutated.
* The G2b-1 compaction hook is still constructed in both arms (so the tool surface and prompt prefix
  stay identical), but `compaction_mode=off` makes it a pass-through: `compact()` returns the input
  list unchanged.
* Nothing in this stage reads the current prompt-token count, so there is no "re-choose history
  because the prompt grew" behaviour, and the two mechanisms are never active together
  (`compaction_mode=off` in every run of both arms).
* `history.read` returns a normal tool result for the page; that result is itself excluded from
  offloading (§3), so paging cannot rewrite or re-archive anything.

---

## 3. HistoryStore reuse

* Same root: `<run_dir>/history`, written by the Stage G2b-1 `HistoryStore`; no second archive.
* Same identifier rule: `hc-<24hex>` derived from the trusted `(run_id, task_id, tool_use_id,
  content-digest)` tuple; `tool_use_id` is the runtime's trusted idempotency key when available.
* Same isolation: `HistoryReader` re-checks run **and** task on every read; cross-worker and
  cross-run reads are refused (`history_integrity`), the reference is pattern-validated before any
  filesystem access, and no host path is ever exposed to the model.
* Same tool: `history.read` with `ref_id / offset / limit`, ≤ 4000 chars per page, digest-verified,
  read-only, registered in **both** arms so the model-visible schema is identical.
* Archive order: payload first, then metadata, then a re-read that verifies length and digest; the
  model only receives a reference after the archive verified. On any failure the original result is
  returned unchanged (`archive_failures`, no dead reference).

**One correctness fix in the reused store.** `_atomic_write_text` wrote with the platform newline
translation, so a payload containing `\r\n` (Windows sandbox output) was stored with `\r\n\r\n`, and
the verifying read silently converted `\r\n` back to `\n` — the digest then never matched and every
such result fell back to "archive failed". Writing and reading with `newline=""` makes the archive
byte-exact; `tests/unit/test_tool_result_offload.py::test_crlf_payload_round_trips_byte_for_byte`
is the regression test. LF-only payloads are unaffected.

Offload policy: `len(result.data) > 4000` and `task_id` present and `history.read` result and not
already a reference. Head/tail previews are 200 chars each; error results additionally get a
deterministic diagnostic excerpt (≤ 400 chars, original lines only, last 6 matching lines), and the
rendered exit code stays visible in the preview.

---

## 4. Tool state and safety semantics

| Requirement | Evidence |
|---|---|
| `is_error` unchanged | `model_copy(update={"data": ...})`; integration test asserts the block is still an error |
| `retry_safe` unchanged | same copy; risk=unknown stays `retry_safe=False`, completed error stays `True` under G3-1 `corrected` |
| Unknown execution state not reclassified | `test_unknown_outcome_is_not_offloaded_and_still_stops`: `ok=False` still yields `side_effect_unknown` |
| Completed error not turned into unknown | `test_large_error_result_keeps_semantics`: 6 KB traceback → placeholder keeps `AssertionError`, `exit code 1`, worker continues |
| M3 decision unaffected | offloading runs after the gate returns |
| M4 record unaffected | 3 live runs show `execution_outcome.result_size` of the **raw** result; ledger state and `retry_safe=False` unchanged |
| M7 audit keeps the truth | M4 `execution_outcome` = raw size; M7 `tool_observation` = model-visible size (both kept, different questions) |
| No replay | the offloader never calls a tool; transcripts show one execution per model command |

G3-1 regression: `tool_error_mode=corrected` in all 12 runs, `side_effect_unknown_workers = 0` in
both arms, and the G3-1 test suites still pass.

---

## 5. M6 resume verification

`tests/integration/test_stage_g3_2_ingress_offload.py::test_m6_resume_replays_the_reference_without_rearchiving`

1. Turn 1 runs a 20 k-char bash and the placeholder is persisted by the M6 snapshot hook.
2. A **fresh** agent is built on the same runtime; `AtlasToolAgent.run()` restores the snapshot
   instead of the prompt.
3. Assertions: the provider receives the **same placeholder** (byte-identical), the raw body is not
   restored into the request, `offloads` stays 1, exactly one archive file exists, and the archive
   still resolves through the reader.
4. The reference stays valid because the archive is on disk under the run directory and the reader
   resolves it by `ref_id`; nothing depends on process memory.

---

## 6. Tests and full regression

New/changed tests:

* `tests/unit/test_tool_result_offload.py` — 22 tests: off-mode identity, 3999/4000/4001 thresholds,
  archive + full paged recovery, head/tail exactness, digest-tampering refusal, cross-worker and
  cross-run refusal, archive-failure fallback, already-offloaded no-op, `history.read` recursion
  guard, `is_error`/`retry_safe` preservation, unknown-state preservation, deterministic error
  diagnostics with a length cap, conversation-snapshot resolution, concurrent-worker isolation,
  metric serialization, CRLF round-trip.
* `tests/integration/test_stage_g3_2_ingress_offload.py` — 9 tests: 4-turn agent loop (reference →
  `history.read` → unchanged reference), off-mode control, parallel batch (two offloads, distinct
  refs, one execution each), large error semantics, unknown outcome stop, M6 resume, duplicate-ingress
  no-op, identical reader registration in both arms.
* `tests/unit/test_history_compaction.py` — unchanged, still green (the CRLF fix is additive).

```text
.\.venv\Scripts\python.exe -m pytest   → 564 passed, 1 deselected in 199.22s
ruff check .                            → All checks passed!
ruff format --check .                   → 324 files already formatted
pyright                                 → 0 errors, 0 warnings, 0 informations
```

---

## 7. The 12 real runs

| Item | Value |
|---|---|
| Arms | O0 `tool_result_mode=off`, O1 `tool_result_mode=ingress_offload` |
| Pinned | `handoff=dual`, `budget=guarded`, `tool_error=corrected`, `compaction=off` |
| Tasks | `e1-02-report`, `e1-10-search`, `e1-12-conflict` (A1, 3 workers each) |
| Repetitions | 2 per task per arm, alternating O0/O1 and O1/O0 |
| Model / temperature | Stage E1 defaults (`deepseek-chat`, 0.0) |
| Budget / turns / tools | 120,000 tokens, 14 turns, 60 tool calls |
| Sandbox | OpenSandbox, fixture and oracle unchanged |

Raw data: `experiments/results/stage-g3-2/g32-20261111T042736Z/` (`manifest.json`, per-run
`result.json`, `provider_calls.jsonl`, `tool_conversations.json`, `history/`, `analysis/`).

| Task | Rep | Mode | Run ID | Oracle | Class | Workers ok | Turns | Prompt | Offloads | `history.read` |
|---|---|---|---|---|---|---|---|---|---|---|
| e1-02-report | 0 | off | `eval-f84545a437ba` | FAIL | BUDGET_EXCEEDED | 0/3 | 32 | 106,495 | 0 | 0 |
| e1-02-report | 0 | ingress | `eval-dfcfe8bab764` | FAIL | BUDGET_EXCEEDED | 1/3 | 29 | 107,820 | 2 | 0 |
| e1-02-report | 1 | off | `eval-24a68d281811` | FAIL | BUDGET_EXCEEDED | 0/3 | 32 | 108,927 | 0 | 0 |
| e1-02-report | 1 | ingress | `eval-4af69e2a80b5` | FAIL | BUDGET_EXCEEDED | 0/3 | 31 | 109,814 | 2 | 0 |
| e1-10-search | 0 | off | `eval-1cb3983edd23` | FAIL | BUDGET_EXCEEDED | 0/3 | 25 | 111,931 | 0 | 0 |
| e1-10-search | 0 | ingress | `eval-d5c6b50fe181` | FAIL | BUDGET_EXCEEDED | 0/3 | 30 | 111,757 | 2 | 0 |
| e1-10-search | 1 | off | `eval-4dc0532dd389` | FAIL | BUDGET_EXCEEDED | 1/3 | 25 | 111,726 | 0 | 0 |
| e1-10-search | 1 | ingress | `eval-43b92c7d5830` | FAIL | BUDGET_EXCEEDED | 0/3 | 32 | 109,600 | 1 | 0 |
| e1-12-conflict | 0 | off | `eval-1327ee5b876d` | PASS | TASK_SUCCESS | 3/3 | 11 | 23,758 | 0 | 0 |
| e1-12-conflict | 0 | ingress | `eval-b6810c361e5f` | PASS | TASK_SUCCESS | 3/3 | 12 | 25,659 | 0 | 0 |
| e1-12-conflict | 1 | off | `eval-35fe3c5f313b` | PASS | TASK_SUCCESS | 3/3 | 11 | 23,744 | 0 | 0 |
| e1-12-conflict | 1 | ingress | `eval-32fc2c1890bd` | PASS | TASK_SUCCESS | 3/3 | 16 | 39,818 | 0 | 0 |

Offload activity (O1 only): 7 archives in 4 of 6 runs — `e1-02-report` 2+2, `e1-10-search` 2+1,
`e1-12-conflict` none (its results never exceeded 4000 chars). Archived raw text 76,452 chars,
model-visible placeholders 4,405 chars, archive failures 0, duplicate archives 0, error results
offloaded 0. The two largest real offloads were the `parser` worker's `ls /opt` (5,993 chars) and a
`find /tmp`-style dump (34,794 chars) in `eval-dfcfe8bab764`.

---

## 8. Cache hit / miss analysis

| Metric | O0 (off) | O1 (ingress_offload) |
|---|---|---|
| Cache hit tokens | 437,760 | 459,264 |
| Cache miss tokens | 48,821 | 45,204 |
| Token-weighted hit ratio | 0.8997 | 0.9104 |
| Median run hit ratio | 0.9000 | 0.9069 |
| Median run ratio, first call excluded | 0.9027 | 0.9076 |

Per-pair hit-ratio deltas: −1.37 pp, −1.09 pp, +2.43 pp, +4.84 pp, +0.32 pp, −0.34 pp.

Reading: the direction is slightly positive (+1.07 pp aggregate, sign mixed across pairs) and the
mechanism is cache-friendly by construction — the placeholder is written once, at ingress, and every
later request repeats exactly the same bytes, so no prefix is invalidated retroactively. But with
n=6/arm and a 3.6 % difference in prompt volume caused by different trajectories, this is **not** a
demonstrated cache improvement. The G2b-1 finding still holds: prompt-token reduction does not
translate automatically into fewer misses or lower cost.

---

## 9. Token and cost attribution

| Metric | O0 | O1 | Δ |
|---|---|---|---|
| Prompt tokens | 486,581 | 504,468 | +17,887 (+3.7 %) |
| Completion tokens | 19,354 | 19,916 | +562 (+2.9 %) |
| Cost (USD) | 0.040498 | 0.040216 | −0.000282 (−0.7 %) |
| Model turns | 136 | 150 | +14 (+10.3 %) |
| Tool calls (transcripts) | 141 | 161 | +20 (+14.2 %) |
| Average prompt per turn | 3,577.8 | 3,363.1 | −214.7 (−6.0 %) |
| Average cost per successful run | 0.002595 | 0.003617 | +0.001022 |

Separating the factors, as required:

1. **Prompt shortening per turn** — real and measurable in isolation:
   `summary.json → ingress_savings` estimates **12,650 prompt tokens** (72 k prompt chars) avoided
   across the O1 arm by replacing archived text with placeholders on each remaining turn of the
   worker (`ESTIMATED`, tiktoken over the actual transcripts). That is ≈2.5 % of the O1 prompt.
2. **Model turns** — O1 ran **+14 turns**, which by itself *adds* prompt volume (every turn re-sends
   the conversation). The +17,887 measured delta is dominated by this, not by the offloads.
3. **Cache hit/miss** — §8; direction slightly positive, not separable.
4. **Worker re-reading history** — `history.read` was called **0 times** in all 12 runs. The model
   never tried to restore an archived result.
5. **Trajectory variance** — the largest single confounder: e.g. `e1-12-conflict` rep 1 ran 16 turns
   in O1 vs 11 in O0 with no offload at all, adding 16 k prompt tokens.
6. **Oracle** — unchanged (2/6 in both arms), so no quality gain or loss is attributable.

Conclusion: total prompt tokens did **not** fall (they rose 3.7 %); cost fell 0.7 %, i.e. within
noise; the per-turn prompt fell 6 %. A total-token decrease was not observed and is not claimed.

---

## 10. Oracle, stop reasons, budget

| Metric | O0 | O1 |
|---|---|---|
| Oracle pass | 2/6 | 2/6 |
| `TASK_SUCCESS` | 2/6 | 2/6 |
| Worker success | 7/18 | 7/18 |
| `side_effect_unknown` workers | 0 | 0 |
| `max_turns` workers | 0 | 4 |
| `BUDGET_EXCEEDED` runs | 4 | 4 |
| Budget admission rejections | 11 | 7 |
| `history.read` calls | 0 | 0 |

Both arms are budget-bound on `e1-02-report` and `e1-10-search` (8 of 12 runs hit 120 k tokens) and
clean on `e1-12-conflict`. In `e1-10-search` the O1 workers exhausted the *turn* limit while the O0
workers exhausted the *budget* (rejections 3 vs 1 per run) — the same ceiling reached through a
different door, with the same oracle outcome. No failure in either arm was caused by missing tool
output: the offloaded results in O1 were exploratory dumps, and no worker asked for them back.

---

## 11. Known limitations

1. **Small, budget-bound sample.** 12 runs, n=6/arm, one model, one temperature, 8 of 12 runs
   budget-terminated. Directional only; no statistical claim.
2. **Offloading rarely fires at 4000 chars** on these tasks (7 archives in 4 of 6 O1 runs; zero in
   `e1-12-conflict`). The threshold was not tuned in this stage.
3. **`history.read` was never used.** The recovery half of the mechanism is proven by tests, not by
   the live runs. Whether a model *should* have re-read an archived dump is not observable here.
4. **Trajectory variance dominates the aggregate.** The `max_turns` 0 → 4 shift and the +14 turns
   cannot be attributed to offloading with this sample.
5. **`metrics.tool_calls` under-counts** when a worker is killed by a budget rejection (the fallback
   `AgentRunResult` carries no `ToolCallRecord`s); the report uses transcript counts. Pre-existing
   G2a/evidence behaviour, not changed here.
6. **Audit visibility split.** `result.json → tool_calls[].output` and `tool_conversations.json`
   contain the *placeholder*; the raw text lives in `<run_dir>/history/<ref>.txt` and the raw size in
   the M4 `execution_outcome` record. Anything that needs the original text must resolve the ref.
7. **M6 HITL settlement path** writes tool-result blocks from the settlement (`resume.py`), which
   bypasses the executor boundary; a settled *large* tool result would not be offloaded. No such case
   occurs in these runs (0 suspensions) and it was not changed.
8. **Windows newline semantics** are now byte-exact for the archive; a store written by an older build
   with CRLF payloads would still fail its digest check (no historical archive needs migration
   because Stage G2b-1 arms archived nothing on Windows in the recorded rounds).

---

## 12. Final verdict

**A. Mechanism correctness — PASS.**

* Full original text is archived before any reference is returned (7/7 live archives
  digest-verified, 0 failures, 0 duplicates).
* The model received a bounded preview plus an opaque reference (4,405 placeholder chars for 76,452
  archived chars).
* The original is recoverable page by page through `history.read` (unit + integration + M6 resume).
* No already-sent history is rewritten: the only write point is block creation; no `before_turn`
  scan exists in the active configuration.
* Existing safety mechanisms do not regress: M3 decisions, M4 records, G3-1 error semantics and the
  unknown-state stop all behave as before (0 `side_effect_unknown`, 564 tests green).

**B. Performance improvement — NOT DEMONSTRATED (and one warning sign).**

* Prompt tokens: **up** 3.7 % in aggregate, though per-turn prompt is down 6.0 % and the estimated
  direct saving is 12,650 tokens.
* Cache hit ratio: +1.07 pp aggregate, sign mixed per pair — not separable from trajectory variance.
* Cost: −0.7 % (noise); average cost of successful runs up, on 2 successes per arm.
* Budget exhaustion: unchanged at 4/6 runs; `max_turns` workers went 0 → 4 in `e1-10-search`.
* The stage did not reproduce the offline G3-0 estimate (19.9 %), and that estimate was never a
  hard target.

**C. Task quality — UNCHANGED.**

* Oracle 2/6 in both arms; `TASK_SUCCESS` 2/6 in both; worker success 7/18 in both.
* No run failed because the model lacked error information: the one error-bearing candidate was
  below the threshold, and the two live offloads were exploratory listings.
* No invalid extra tool calls attributable to restoring content: `history.read` was never called.

**Recommendation:** keep `tool_result_mode=off` as the default. The mechanism is correct, safe and
cheap to keep available (`--tool-result-mode ingress_offload`), but this stage provides no evidence
that it pays for itself at `max_inline_chars=4000` on the current workload, and it introduced
4 `max_turns` workers that must be explained before it can be promoted. The next useful experiment is
a threshold sweep (e.g. 1500) on tasks whose transcripts actually carry large results, with the
`max_turns` shift as an explicit monitored metric — not a new mechanism.

---

## Changed files

Added:

* `src/atlasos/tool_result_offload.py` — ingress offloader, metrics, runtime wrapper
* `tests/unit/test_tool_result_offload.py`
* `tests/integration/test_stage_g3_2_ingress_offload.py`
* `experiments/evaluation/stage_g3_2_analysis.py`
* `experiments/results/stage-g3-2/g32-20261111T042736Z/` (raw runs + `analysis/`)
* `docs/stage-g3-2-report.md`

Modified:

* `src/atlasos/tools/dispatch.py` — optional post-execution offload at
  `AtlasDispatchExecutor.execute`
* `src/atlasos/tools/runtime.py` — `tool_result_offloader` parameter (default `None`)
* `src/atlasos/history_compaction.py` — byte-exact archive write/read (`newline=""`), required for
  CRLF payloads; no semantic change for LF
* `experiments/evaluation/runner.py` — `RunnerSettings.tool_result_mode`, ingress wiring,
  `metrics.tool_result`, `provider_calls.jsonl` per-call usage
* `experiments/evaluation/stage_e_cli.py` — `--tool-result-mode off|ingress_offload`
* `experiments/evaluation/stage_e_rounds.py` — `g3-2` suite (`o0`/`o1`)
* `.gitignore` — ignore `experiments/results/stage-g3-2/`

Read-only `git diff --stat` (working tree also carries the uncommitted G1–G3-1 work; no Git write
operation was performed in this stage):

```text
 .gitignore                                 |   5 +
 experiments/evaluation/runner.py           | 168 ++++++-
 experiments/evaluation/stage_e_cli.py      | 109 +++++
 experiments/evaluation/stage_e_rounds.py   | 530 +++++++++++++++++++++-
 src/atlasos/integrations/anycode_engine.py |  93 +++-
 src/atlasos/sandbox/tools.py               |  49 +-
 src/atlasos/tools/conversation.py          |  33 ++
 src/atlasos/tools/dispatch.py              |  44 +-
 src/atlasos/tools/runtime.py               |   8 +-
 tests/integration/test_anycode_dag.py      | 705 ++++++++++++++++++++++++++++-
 10 files changed, 1727 insertions(+), 17 deletions(-)
```

`src/atlasos/history_compaction.py` and `src/atlasos/tool_result_offload.py` are still untracked, so
they do not appear in that stat; the CRLF fix described in §3 lives in the former.
