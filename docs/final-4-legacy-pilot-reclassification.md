# Final-4 — Reclassification of the Legacy Three-Run Pilot

Date: 2026-11-14. Runtime: `93cbd98f9407e7358e997878238520dcac15827a` (`src/` unchanged at HEAD).
Machine-readable form: `experiments/results/final4/legacy_pilot_classification.json`.

The three runs are **not deleted, not overwritten and not edited**. `pilot_results.json`,
`pilot_results.csv` and `router_comparison.json` are left byte-identical. This document adds the
classification they were missing.

## 1. New classification

| Unit | Run id | Previous label | New label |
|---|---|---|---|
| `validation-serial-R0` | `eval-aa9da0286142` | `EXECUTED` (pilot result) | `EXPLORATORY_LEGACY_PILOT` |
| `validation-serial-R1` | `eval-c16d3b21a134` | `EXECUTED` (pilot result) | `EXPLORATORY_LEGACY_PILOT` |
| `validation-serial-R2` | `eval-90a1e4cc127f` | `EXECUTED` (pilot result) | `EXPLORATORY_LEGACY_PILOT` |

Recorded defects, both applying to all three units:

- **`CONTEXT_CONFIGURATION_MISMATCH`** — `pilot_manifest.json` declares
  `context_policy="task_scoped + stable_deterministic + identity"`. Every unit loaded
  `experiments/configs/eval-benchmark.yaml`, which has **no `context:` section**, so `AtlasConfig`
  resolved `context.policy="upstream_default"` and
  `runner.py::_context_prompt_builder` returned `None`. Each `result.json` records that config path.
- **`UNEQUAL_PUBLIC_TASK_INFORMATION`** — `final4/validation_tasks.py` gives each worker step a
  one-sentence `objective` ("implement clean_rows", "implement build_report on top of it"), and
  `runner_entry.py::_worker_steps` uses that sentence as the worker's entire prompt. The
  single-worker arms received the full public requirement, including the monkeypatch contract.

## 2. What actually happened in R1 (proximate cause)

This audit found a third defect, and it is the one that decided the R1 oracle result:

- both workers ended with `stop_reason = side_effect_unknown`;
- the recorded tool calls show both workers exploring `.git` (`git cat-file`, `git fsck`,
  `ls .git/...`), then a command returning a non-zero exit code with
  `ls: cannot access '/tmp/project.bundle': No such file or directory` and
  `fatal: Not a valid object name …`;
- under `tool_error_mode="legacy"` (`src/atlasos/sandbox/tools.py:26-35`) a non-zero exit of a
  side-effecting tool leaves `retry_safe` unset, and the runner stops the worker;
- neither worker wrote a file, so `workspace_patch_digest` is the SHA-256 of the empty string.

So the R1 result is **not** evidence that multi-worker delegation degrades quality, and it is not
evidence about context handoff (no handoff ran). It is evidence that a worker can be killed by an
unrelated tool-error classification, and that a one-line prompt invites a worker to go looking for the
specification in Git history instead of doing the task.

`EXPLORATORY_LEGACY_PILOT` therefore has two run-level notes in addition to the two named defects:

- `WORKER_TERMINATED_BY_UNRELATED_TOOL_ERROR` (R1);
- `ROUTER_DIRECT_JUDGED_AN_UNEQUAL_PROPOSAL` (R2) — the router's DIRECT decision was correct for the
  proposal it received, but that proposal was built from the unequal worker prompts.

## 3. What these runs may still be used for

| Allowed use | Why it holds |
|---|---|
| Real end-to-end execution-chain smoke | Real DeepSeek calls, real OpenSandbox, real gate/ledger/binding, real isolated oracle, real cleanup (`sandbox_destroyed=true` in all three) |
| Evidence that the shipped router branches to DIRECT and that the collapsed plan is graded as a single worker | R2 ran with `delegation_route_mode="selective"`, `executed_worker_count=1`, worker id `…-solo` |
| A worked example of evaluation-configuration and input-construction defects | §1 and §2 above are reproducible from the archived run directories |

## 4. What these runs may no longer be used for

- A fair policy comparison against the final Atlas context-aware runtime. The treatment variable was
  absent.
- Sufficient evidence that the router avoids harmful delegation. The proposal it judged was built
  from unequal worker prompts, so "DIRECT was right" cannot be separated from "the proposal was
  malformed".
- Evidence of a Context Engineering benefit or cost. No block, selector, transform or renderer ran.
- Any inference from `27020 > 26781 > 22969` tokens. Those totals are not comparable: R1's number
  covers a run that produced no artifact, and R2's covers a different task-level prompt.

## 5. Budget accounting

The three runs and their cost stay counted against the Final-4 envelope, exactly as consumed:

| Item | Value |
|---|---|
| Task runs consumed | 3 of 24 |
| Cost consumed | 0.00838488 USD of 2.00 USD |
| Sandbox cleanup | confirmed destroyed in all three runs |
| Provider calls | 9 + 11 + 8 = 28 (from `model_turns`) |

Reclassification changes the *interpretation* of these runs, not their cost.

## 6. Provenance caveat

Both the run records and this document pin `src/` at `93cbd98`. The runs themselves record
`git_commit=4ce227b8add3fc6b548ae57e8b7c630408db2fda` with `git_dirty=true`, i.e. the working tree was
not clean when they executed. `git diff 93cbd98..HEAD -- src` is empty, so the runtime code is
pinned, but a dirty tree is a provenance caveat that this document records rather than explains.
