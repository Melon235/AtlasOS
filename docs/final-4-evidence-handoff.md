# Final-4 — Evidence Handoff for Independent Review

Status: `AWAITING_INDEPENDENT_REVIEW`. Nothing in this document is reviewer-verified. Every number is
a local report, reproducible from the run directories named here.

Baseline this document describes:

| Item | Value |
|---|---|
| Repository / branch | `Melon235/AtlasRuntime`, `chore/final-1-5-repository-baseline` |
| Final-4 closure commit | `428706908056200e06b35bddad7511c2e60e6deb` |
| Frozen runtime | `93cbd98f9407e7358e997878238520dcac15827a` (`git diff 93cbd98..HEAD -- src` empty) |
| Design contract | `37eba87df2aa188d16d24d0c31bd00412d154276` (unmodified) |

---

## 1. Handoff checklist

| Item | Value | Where |
|---|---|---|
| Real task runs | 22 distinct run ids, no duplicates | `experiments/results/final4/run_inventory.json` |
| Composition | 3 exploratory legacy + 15 corrected matrix + 4 replications | ibid., `runs[]` |
| Sandbox containers | 44 = 22 task sandboxes + 22 independent oracle sandboxes, all destroyed, 0 cleanup failures | ibid., `resources` |
| Tokens | 880 588 input (790 272 hit + 90 316 miss), 53 750 completion | ibid., `totals` |
| Cost | 0.09634 USD of the 2.00 cap, from each run's own pricing table | ibid. |
| Offline suite | 954 passed, 0 failed, 6 skipped; ruff + format + pyright clean | `pytest -q`, local |
| Live evidence retained | every legacy, corrected and replicated run, including failures and negative results | `experiments/runs/final4/**` |

Standing conclusions, unchanged by this round:

| Claim | Status |
|---|---|
| Context Plane reaches the provider request | measured (7 live runs, 1 verified at content level) |
| Router executed `DELEGATE_PARALLEL` | measured once (`eval-ffc739e09fa6`, 3 workers, oracle PASS) |
| Stable cache benefit from the Context Plane | **not established** (pair 1 said yes, pair 2 reversed it) |
| Native N0 | `BASELINE_PARITY_BLOCKED` |
| Tool disclosure ablation | `ABLATION_DEGENERATE` for these three tasks |
| Placement in the Coding path | **not wired** (`BYPASS` in every unit) |
| Final-5 | not entered |

No run was added, re-run or rewritten in this round. No model call was made.

---

## 2. A — What "14/14 PASS" actually meant (corrected)

The earlier claim was wrong on both the count and the predicate, and is retracted.

**Count.** 22 runs, not 14. `run_inventory.json` covers every `result.json` on disk at the time,
including the three legacy runs.

**Predicate.** The check is `oracle.required_artifacts_present` — one oracle sub-check — re-evaluated
by listing files in each run's exported `workspace/` directory. It is not an oracle execution.

The counter-example is inside the data:

```text
run eval-c16d3b21a134 (legacy validation-serial R1)
  patch_digest            sha256:e3b0c442…  (the empty string)
  all_workers_produced_output   false
  hidden_tests_passed           false
  required_artifacts_present    true        <- the file-presence sub-check
  oracle.passed                 false       <- unchanged, still FAIL
```

The seed files exist with `NotImplementedError` inside, so presence is trivially true. Therefore:

- the legacy FAIL is **not** rewritten, and no FAIL became a PASS;
- the field is labelled `check_kind: FILE_PRESENCE_ONLY`,
  `can_establish_full_oracle_equivalence: false` in `run_inventory.json`;
- what can be argued **without a re-run** is one-directional: a run whose full oracle already passed
  cannot be turned into a failure by an added presence condition, because the functional checks import
  and execute the deliverables, so the files had to exist. That is a logical argument, not a
  measurement, and it is the only basis on which revision-2 cells stay comparable.

`docs/final-4-evidence-closure-report.md` §B and `docs/final-4-architecture-coverage.md` §4 were
corrected to say this.

---

## 3. B — Why the native arm is blocked (source-level)

The earlier phrase "no lifecycle layer exists" is imprecise. Concretely:

| Piece | Exists? | Source |
|---|---|---|
| Provider construction | yes | `src/atlasos/sandbox/provider.py:32 build_sandbox_provider` |
| Sandbox create / exec / file io / destroy | yes | `src/atlasos/sandbox/opensandbox_provider.py:330 create`, `:393 execute`, `:477 write_file`, `:496 read_file`, `:547 destroy` |
| Workspace export for the oracle | yes | `src/atlasos/sandbox/export.py:111 export_workspace`, `:62 export_patch` |
| Git project upload | yes | `src/atlasos/sandbox/provision.py:40 GitProjectProvisioner.provision` |
| Tool backend for unmodified AnyCode | yes | `experiments/evaluation/final4/neutral_adapter.py:177 SandboxToolBackend`, `:353 NeutralEngine` |
| **The composition** create → seed → run → export → destroy, without `ProjectSandboxManager` | **no** | — |

`SandboxToolBackend` needs exactly three inputs: `provider`, `handle`, `workspace_root`
(`neutral_adapter.py:180-182`). `ProjectSandboxManager.ensure_binding`
(`src/atlasos/sandbox/binding.py:303`) is what produces a handle today, and it also brings a durable
binding record, ownership and a lifecycle journal — the Atlas mechanisms that must not sit inside the
baseline arm.

So the blocker is **(a) an unwritten composition, not (b) a missing component and not (c) an interface
gap**. A ~40-line helper in the evaluation layer that calls `provider.create` → `write_file` per
fixture file → hands the handle to `SandboxToolBackend` → `export_workspace` → `destroy` would be
sufficient in principle.

Two open questions that are *not* answered by source reading, and that G-N0 would have to settle
before any live run:

1. Does the image provide `git` and a usable `git status`/`git diff` for the neutral tool list
   (`bash`, `file_read`, `file_write`, `file_edit`, `grep`, `list_files`, per
   `experiments/evaluation/baseline.py:50`)? `status/exec` parity has never been checked in-container.
2. Whether a benchmark fixture needs `git init` inside the container for the oracle's
   `forbidden_paths_unchanged` / `only_allowed_paths_changed` checks to be meaningful — today those are
   computed by the evaluation layer from the exported tree, not by git.

Neither statement is a claim that a new component is required.

---

## 4. C — Request-boundary evidence (commit `410446c`)

**Where it observes.** `ModelRecordingAdapter.stream` (`experiments/evaluation/runner.py`, the
`stream` method) calls `self._evidence.observe(messages, options)` *eagerly, before the generator body
runs*. `chat` observes too. This matters: AnyCode streams, so an implementation that only wrapped
`chat` recorded **zero** requests — that is exactly what `eval-32fbbff36188` shows (`requests
observed 0`), and the defect is now covered by
`tests/integration/test_final4_context_plane.py::test_request_evidence_covers_the_streaming_path`.

**What "first message" means.** `messages[0]` of the list handed to the adapter, rendered as
`role: text of each content block`. AnyCode passes one user message for a task (`Agent.run` builds
`[LLMMessage(user, prompt)]`), so for a task's first request it is the task turn itself.

**Where `task_prompt_sha256` comes from.** `AtlasTaskPromptBuilder` records
`sha256` of the exact string it returned to AnyCode, per task, in the store's render record. The
evaluator recomputes `sha256` of the observed `messages[0]` and compares the two strings.

**The match predicate.** `_request_linkage(...)` in `experiments/evaluation/runner.py`:

- link a request to a render when `sha256(observed first message) == task_prompt_sha256`;
- identify the worker by `sha256(options.system_prompt)` against each worker's trusted system prompt;
- count occurrences of each dependency block's `content` inside the observed request text
  (`injection_count`), and flag `> 1` as a duplicate;
- record only `sha256` digests, character counts, message counts, tool counts and booleans — no
  prompt, no transcript, no completion.

**Result for `eval-db81850a884c`.** `REQUEST_CONTENT_VERIFIED`: 12 requests observed, 12 linked, 0
unlinked. `task-output:w1` present at `call_index` 6–11, `count = 1` each, `duplicate = false`; the
producer's calls (0–5) have no dependency probe because `w1` declares none.

**What the 12/12 denominator is.** Requests, not runs: every adapter call of that single run, 6 from
`w1` and 6 from `w2`.

**What this does *not* prove.** That the provider accepted or used the body — only that the request
reached the adapter boundary with that exact first message and that the dependency content was in it,
once. It is a boundary check, not a provider-side audit. `provider_calls.jsonl` (whole-request hash, no
task identity) is unchanged and is *not* used as a substitute.

**Coverage gap, stated plainly.** Nine other corrected units ran before this wrapper existed and carry
`REQUEST_CONTENT_NOT_VERIFIED`; their per-render `context.json` is absent, and only `run.json`'s
`environment.context_resolved` is used for them. Nothing was reconstructed.

---

## 5. Still unverified

1. Whether the provider returned any cache-miss reduction attributable to the prompt layout. Five
   X0/X1 samples cannot separate it from provider cache state.
2. Whether a delegated topology is better or worse than a single worker: one cell, one repetition.
3. Whether the native arm can run in the same image at all (§3, two open questions).
4. Whether the presence sub-check is ever non-trivial for these tasks (it was true for a run with an
   empty patch).
5. Reviewer-verified status of every number above. `PASS` labels in run files are the harness's own
   oracle output, not an independent audit.
