# V2 M7 — Durable Agent Trajectory

Status: **PASS / FROZEN** (formal clean-tree record:
[`v2-m7-results.md`](../../experiments/results/v2-m7-results.md)).

```text
MODEL ACTION       != AUTHORIZED ACTION
AUTHORIZED ACTION  != EXECUTED ACTION
EXECUTED ACTION    != OBSERVED RESULT
TRAJECTORY EVIDENCE != RUNTIME AUTHORITY
```

M7 is the durable, auditable answer to five different questions:

- What did the model propose?
- How did Runtime judge the proposal?
- What actually reached execution?
- What was rejected, suspended, approved, resumed, replayed, or left uncertain?
- What observation returned to the model?

It records those answers without becoming a second Runtime.

## 1. Problem

M1–M6 already make the important transitions durable, but each authority exposes a different view:

- M1 knows which tools were disclosed.
- M2 knows which scoped worker existed.
- M3 knows a decision and the digests it was based on.
- M4 knows the real execution claim, replay, conflict, and uncertainty.
- M5 knows the project/sandbox binding lifecycle.
- M6 knows the suspension and approval lifecycle.

Before M7 there was no single run-scoped, durable, reconstructable evidence stream that a reviewer
could read after a crash. Process-local logs, metrics, and in-memory observations cannot answer the
five questions above after restart. Reconstructing from a second database or from the model
transcript would create a competing authority.

M7 creates one **evidence projection** over the existing AnyCode run transcript. It does not
execute, authorize, replay, resume, create claims, or transition approvals.

## 2. Invariants

```text
M7 IS EVIDENCE, NOT AUTHORITY.
MODEL ACTION != AUTHORIZED ACTION.
AUTHORIZED ACTION != EXECUTED ACTION.
EXECUTED ACTION != OBSERVED RESULT.
AUTHORITATIVE STATE PRECEDES TRAJECTORY EVIDENCE.
ACTION_PROPOSED PRECEDES AUTHORIZATION BECAUSE IT RECORDS THE FIXED MODEL OUTPUT.
M7 NEVER EXECUTES, AUTHORIZES, REPLAYS OR RESUMES ACTIONS.
M7 REUSES ANYCODE RUNSTORE.
M7 DOES NOT CREATE A SECOND EXECUTION OR RECOVERY AUTHORITY.
RECONCILIATION REPAIRS EVIDENCE ONLY.
M7 MAY REPORT EVIDENCE GAPS.
EVALUATION CONSUMES TRAJECTORY; IT NEVER CONTROLS RUNTIME.
```

The ordering exception is intentional: `ACTION_PROPOSED` is appended after the LLM response is
fixed and before M3, because the proposal is the already-fixed model output that M3 will judge.
Every other event is appended **after** the corresponding authority commit.

## 3. Architecture

```text
LLM response (fixed)
  -> ACTION_PROPOSED per ToolUseBlock (ordinal order)
  -> M1 disclosure evidence
  -> M2 worker lifecycle evidence
  -> M3 ACTION_DECISION (phase=preflight)
       -> no human: EXECUTION_OUTCOME / TOOL_OBSERVATION
       -> human:  SUSPENSION_CREATED
                    -> APPROVAL_RESOLVED
                    -> RESUME_STARTED
                    -> M3 ACTION_DECISION (phase=resume_revalidation)
                    -> EXECUTION_OUTCOME (execute/replay/uncertain)
                    -> TOOL_OBSERVATION
                    -> SUSPENSION_CLEARED
  -> RUN_TERMINAL
```

The durable substrate is the existing AnyCode `RunStore` / `TranscriptEvent`:

```text
<runs_root>/<run_id>/
  meta.json
  transcript.jsonl
  checkpoints/
```

Atlas writes `kind="lifecycle"` events whose payload contains exactly one key:

```python
{"atlas_trajectory": {...typed envelope...}}
```

No trajectory SQLite database, EventLog, checkpoint engine, recovery coordinator, approval store,
or execution authority is created. `FilesystemRunStore` is used directly. M7 disabled means a
`NoopTrajectoryRecorder`, no run-store root, and unchanged M1–M6 behavior.

## 4. Authority separation

```text
Atlas Runtime / ExecutionContext     authoritative identity and scope
M3 ActionSafetyGate                  current authorization decision
M4 ActionLedger + AnyCode idempotency authoritative execution/replay/uncertainty
M5 ProjectSandboxManager             authoritative project/sandbox continuity
M6 ExecutionSuspension + ApprovalStore authoritative pause/human decision
AnyCode RunStore                     durable evidence transport only
M7 TrajectoryReader / Reconciler     evidence projection and evidence repair only
```

The runtime never reads the trajectory to make a decision. The trajectory may be stale, incomplete,
or in conflict; the authority remains correct by construction.

Examples:

- An `APPROVED` trajectory event does not execute anything; only the M6 ApprovalStore plus M3
  revalidation plus M4 can lead to execution.
- A trajectory claim of `COMPLETED` does not override an M4 ledger record that says `UNCERTAIN`.
- A trajectory claim that a sandbox was `READY` does not override an M5 binding that is `LOST`.
- A reconciliation pass cannot append an approval decision, create an M4 claim, call the provider,
  or resume a suspension.

## 5. Event taxonomy

All events use `schema_version = 1` and `protocol_version = 1`. The typed envelope contains:

```text
event_id, run_id, event_type, source, recorded_at (from TranscriptEvent.ts),
occurred_at (only from reliable authority time), correlation, data, backfilled
```

| Event | Source | Written after |
| --- | --- | --- |
| `ACTION_PROPOSED` | model | LLM response fixed, before M3 |
| `TOOL_DISCLOSED` | m1 | disclosure state persisted |
| `WORKER_LIFECYCLE` | m2 | worker record persisted / restored |
| `ACTION_DECISION` | m3 | M3 decision computed (preflight or resume revalidation) |
| `EXECUTION_OUTCOME` | m4 | M4 ledger update committed |
| `TOOL_OBSERVATION` | runtime | ToolResult is available to the model |
| `SANDBOX_LIFECYCLE` | m5 | binding state update committed |
| `SUSPENSION_CREATED` | m6 | suspension + approvals + pause state durable |
| `APPROVAL_RESOLVED` | m6 | approval CAS committed |
| `RESUME_STARTED` | runtime | suspension SETTLING transition committed |
| `SUSPENSION_CLEARED` | runtime | suspension clear transition committed |
| `RUN_TERMINAL` | runtime | manifest final status durable |

`ACTION_DECISION` is not one event with mutable state. Preflight and resume revalidation are
separate facts with `phase="preflight"` or `phase="resume_revalidation"`; a later decision never
overwrites an earlier one.

## 6. Correlation and ordering

Every event carries a typed `TrajectoryCorrelation` with only the identifiers it can truthfully
support:

```text
run_id, worker_id, profile_id, model_response_id, ordinal, call_id, tool_id,
occurrence_id, action_id, attempt_id, approval_id, suspension_id, sandbox_binding_id
```

`ACTION_PROPOSED` uses the fixed `LLMResponse.id` when available, otherwise a deterministic
digest of the run, turn identity, and assistant response. `ordinal` is the zero-based position in
the model response and is preserved through suspension, approval, resume, and observation.
`ACTION_DECISION` carries the M3 digests (`action_digest`, `decision_basis_digest`, `input_digest`,
`tool_binding_digest`) and only safe reason codes.

`ACTION_DECISION` event identity includes an `occurrence_id`, because a repeated runtime evaluation
is not duplicate evidence:

- preflight uses the fixed `model_response_id` as the batch occurrence boundary;
- resume revalidation uses the M6 settlement-attempt identity
  `settlement:<suspension_id>:<suspension.revision>`, where `revision` advances at every
  `begin_settlement` CAS and therefore changes once per real resume attempt;
- normal execution uses the runner invocation occurrence supplied by the runtime seam.

The event id includes `protocol_version`, `run_id`, `event_type`, `call_id`, `phase`,
`occurrence_id`, and `decision_basis_digest`. It never uses `recorded_at`, wall-clock retry time, or
a random retry timestamp to distinguish occurrences. Recorder retries reuse the same occurrence and
event id, so duplicate evidence writes still collapse to one logical event.

The formal evaluator reconstructs the run using only `run_id`, then verifies that all three
proposals share one `model_response_id`, that ordinals are `0,1,2`, that proposals precede the first
decision, and that the suspension/approval/action/observation identifiers link correctly.

## 7. ACTION_PROPOSED seam

`AtlasAgentRunner._execute_tool_blocks` is the single pre-execution seam. After AnyCode has fixed
the assistant response and before the M6 preflight barrier, Atlas appends one `ACTION_PROPOSED` per
ToolUseBlock in ordinal order. The event contains:

```text
model_response_id, ordinal, call_id, tool_id, tool_name, input_digest, input_shape, input_size
```

It contains no raw arguments, prompt text, or model reasoning. A fallback deterministic
`model_response_id` is derived if the provider did not supply one, so correlation never depends on
a random UUID.

## 8. M1–M6 recording seams

| Stage | Seam | Evidence |
| --- | --- | --- |
| M1 | `AtlasToolRuntime.search` after disclosure persistence | `TOOL_DISCLOSED` with query digest and tool identity |
| M2 | `ScopedWorkerFactory.create` / `resume` after record persistence | `WORKER_LIFECYCLE` create/restore with context hash |
| M3 | `ActionSafetyGate.evaluate` after decision | `ACTION_DECISION` with phase and digests |
| M4 | `DurableActionBridge._finish` after ledger update | `EXECUTION_OUTCOME` with M4 state/completion vocabulary |
| M5 | `ProjectSandboxManager` after binding update | `SANDBOX_LIFECYCLE` READY/LOST/CLOSED |
| M6 | suspension/approval/resume transitions | `SUSPENSION_CREATED`, `APPROVAL_RESOLVED`, `RESUME_STARTED`, `SUSPENSION_CLEARED` |

The M6 suspension now stores the optional `model_response_id`, so approval and resume evidence can
be correlated to the exact fixed model response without asking the model to reconstruct anything.

M4 also records the M3 `call_id` in its optional ledger field. It is populated whenever a trusted
M3 call identity is present, independent of whether M7 is enabled. M4 provenance does not depend on
the trajectory feature flag. This also lets evidence-only reconciliation rebuild an
`EXECUTION_OUTCOME` with the same call correlation as the live bridge.

## 9. Execution outcome vocabulary

M7 does not invent execution states. `EXECUTION_OUTCOME` uses the real M4 vocabulary:

```text
state:           IN_PROGRESS | COMPLETED | UNCERTAIN | FAILED
completion_kind: EXECUTED_SUCCESS | REPLAYED | CONFLICT | ...
claim_outcome:   EXECUTED | REPLAYED | CONFLICT | UNCERTAIN
```

`EXECUTED_SUCCESS` means M4 actually executed the side effect. `REPLAYED` means AnyCode returned a
durable prior result without executing again. `UNCERTAIN` means the side effect may have happened
and M4 cannot prove it. A human `APPROVED` is not an execution outcome.

## 10. Observation evidence

Atlas currently does not enable AnyCode AgentRunner durability, so no native durable
`tool_call`/`tool_result` transcript exists. M7 therefore writes `TOOL_OBSERVATION` from:

- the normal `AtlasAgentRunner._run_single_tool_block` path, and
- the M6 settlement path (live or recovered), after the ToolResult batch is durable.

The event contains `call_id`, `tool_id`, `is_error`, result digest/size, and optional settlement
`outcome`; it contains no raw result content. The reader audits native transcript events too:

- native `message` events are projected defensively;
- a native `tool_result` is projected only when a preceding native `tool_call` correlates by call id;
- if a correlated native observation exists, the Atlas `TOOL_OBSERVATION` is suppressed to avoid
  duplication;
- `native_tool_observations_duplicated` is a formal zero counter.

## 11. Crash and reconciliation

The write order is authority first, evidence second. If the process dies between the two, the
authority is still correct and M7 has an evidence gap. `TrajectoryReconciler` repairs evidence only:

- M4 ledger outcomes missing from the transcript are backfilled as `backfilled=true`;
- terminal approvals missing from the transcript are backfilled;
- durable suspensions missing `SUSPENSION_CREATED`/`SUSPENSION_CLEARED` are backfilled;
- backfill reuses deterministic event ids, schema, and correlation;
- the reconciler has no execution, approval, sandbox, resume, or LLM API.

Observable or inferable missing evidence is represented as `EVIDENCE_GAP`. Some historical evidence
may be unreconstructable when the authoritative subsystem does not retain queryable historical
state. M7 does not invent missing history.

For example, a preflight decision whose M3 evaluation was never durable, or a resume-start event
whose M6 transition history is no longer queryable, is reported as a typed gap
(`missing_action_decision`, `resume_started_unreconstructable`, and similar). Reconciliation appends
only evidence it can derive from durable authority.

M7 is durable evidence, not an audit-complete transaction log.

## 12. Dedupe and conflicts

Event identity is deterministic and excludes `recorded_at`, `occurred_at`, `backfilled`, and physical
sequence. It includes the runtime `occurrence_id` for events that can really repeat. The reader
groups physical events by `event_id`, then:

1. if semantic payloads are identical, keeps one logical event and records all physical refs;
2. prefers non-backfilled evidence, then lowest sequence;
3. if semantic payloads differ, emits a typed `EVIDENCE_CONFLICT` and **does not** apply last-write-wins.

A conflict is a reportable evidence defect, not a runtime failure and not a reason to pick the
newest claim. Native projection and Atlas observation use the same no-last-write-wins rule.

## 13. Sensitive data

M7 persists metadata only:

- digests, sizes, shape names, counts, and opaque identifiers;
- safe reason codes, not free-form policy or reviewer text;
- no raw tool arguments, tool results, prompts, assistant text, chain-of-thought, reasoning,
  secrets, or credentials.

The reader never requires raw model content. The formal evaluator scans the durable transcript to
prove a test secret is absent.

## 14. Failure injection

`TrajectoryRecorder` catches encode and append failures. A failed append is best-effort evidence
loss, never a reason to rerun the LLM, M3, a tool, an approval CAS, a resume, or a sandbox creation.
The recorder exposes `recording_failures` and `last_error` for audit.

Fault points inherited from M4/M6 still work unchanged. The main formal scenario crashes after M4
completion but before M6 settlement, then resumes in a fresh runtime: M4 must replay, not re-execute,
and the trajectory must show `EXECUTED_SUCCESS` followed by `REPLAYED`.

A second formal scenario injects an M7 append failure after M4 commits an authoritative execution
outcome, then starts a new process boundary and runs `TrajectoryReconciler`. Reconciliation must
backfill the missing `EXECUTION_OUTCOME` with `backfilled=true`, without a second provider effect.

## 15. OpenHands concepts reused

M7 borrows conceptual discipline, not a second runtime implementation:

- an append-only, typed event stream with explicit correlation;
- separation of "agent action" from "observation" and from "runtime decision";
- replay/reconstructability from durable evidence;
- explicit evidence gaps rather than silently fabricating a chain.

M7 does not reuse OpenHands' runtime, controller, state store, or event-log authority. The durable
transport is AnyCode `RunStore`; the runtime is AtlasOS/AnyCode.

## 16. AnyCode infrastructure reused

- `anycode.runstore.store.FilesystemRunStore` and `TranscriptEvent`
- `kind="lifecycle"` transcript events and the per-run `transcript.jsonl`
- existing AnyCode execution, tool registry/executor, idempotency, and checkpoint/resume boundaries
- existing Atlas M1–M6 stores and decision points; M7 adds recording, not authority

## 17. Verification

Focused coverage lives in:

- [`tests/integration/test_m7_trajectory.py`](../../tests/integration/test_m7_trajectory.py) —
  schema/freeze/roundtrip, unsupported schema, same-occurrence duplicate-write dedupe, native
  observation projection, logical dedupe, backfill preference, conflict, M4 backfill,
  unreconstructable gap, run terminal, NoOp mode.
- [`tests/integration/test_m7_runtime_trajectory.py`](../../tests/integration/test_m7_runtime_trajectory.py) —
  full M1–M6 chain, M7 without M6, recorder failure, authority-wins approval conflict, M1/M2/M5
  seams, semantic-hash invariance, M4 uncertainty versus a fake trajectory claim, real repeated
  resume occurrences, and M4 provenance independent of the M7 flag.

Quality gates: `ruff check .`, `ruff format --check .`, `pyright`, and the full `pytest` suite twice.

## 18. Known limitations

- M7 is best-effort durable evidence, not an audit-complete transaction log. A failed append may be
  observable as a gap only when authority retains enough queryable state to infer it; otherwise the
  historical evidence may be unreconstructable and is not invented.
- Atlas does not yet enable AnyCode AgentRunner durability, so native durable `tool_result` is
  audited defensively but is not the primary observation source.
- Reconciliation cannot reconstruct a decision or an observation that was never durably recorded by
  its authority; it reports a gap instead.
- Event ordering is transcript append order. Where authority records do not expose a transition
  timestamp, `occurred_at` is null rather than guessed.
- `FilesystemRunStore.append_event` is not safe for concurrent writers; the recorder serializes its
  own writes with a single `threading.Lock` and one RunStore bound to the Atlas run id. Multi-process
  concurrent trajectory writers are out of scope.
- The trajectory preserves digests and counts, not raw content. It is an audit chain, not a replay
  of prompts or tool payloads.
- M7 is disabled by default. It is evidence, not an execution semantic, and does not participate in
  `semantic_config_hash`.

## 19. Formal results

The formal evaluator is
[`examples/m7_trajectory_eval.py`](../../examples/m7_trajectory_eval.py). Results are recorded in
[`experiments/results/v2-m7-results.md`](../../experiments/results/v2-m7-results.md).
