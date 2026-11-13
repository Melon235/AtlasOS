# Independent Workload Design

Design only. **No task, fixture, oracle or candidate plan is created by this document.** Everything
below is a slot with a stated source strategy; a task becomes `VERIFIED` only after it has been
imported, its oracle executed twice with identical verdicts, and its digests recorded.

Why this file exists: the historical 30-task benchmark (`atlasos-agentbench-v1`) was used to develop
the runtime, the router and most fixes. It is **development evidence**, and it can no longer be used
to demonstrate generalisation.

---

## 1. Three data classes

| Class | Contents | Permitted use |
|---|---|---|
| **Development** | the 30-task benchmark; Stage E1 families; R0/R0.5 task families; every task that was debugged against a result | fixing defects, debugging, protocol plumbing |
| **Validation** | 3 new tasks (one per structure), created for this protocol | protocol smoke, integration smoke, one bounded router-policy decision |
| **Holdout** | 6 new tasks (2 per structure) never inspected while developing | the formal measurement only |

Rules:

1. A historical task is never renamed into a holdout task.
2. A textual rewrite of a historical task is not a new problem.
3. Once a holdout task has been used to debug anything, it is re-labelled **validation** and a
   replacement holdout task is created *before* the formal run — and the replacement is disclosed.
4. No holdout task text, fixture or oracle is visible to whoever writes the candidate DAGs.

---

## 2. Task structures

| Code | Structure | Definition (structural, not a router verdict) |
|---|---|---|
| **S** | Serial dependency | the artifact of step *i* is an input to step *i+1*; at most one worker can be productive at a time |
| **P** | Naturally parallel | ≥2 independent deliverables with disjoint write surfaces, integrated by one final step |
| **C** | Strongly coupled | shared write surface or an unstable interface between two work units; a wrong split breaks the build |

`S`/`P`/`C` are structural labels for sampling coverage. They are **not** ground-truth labels for
"multi is better": a P task may still be cheaper with one worker, and the protocol must be able to
report that.

---

## 3. Source strategy

Priority order for a new task:

1. **Independently authored multi-module task** (preferred): a small repository with ≥2 modules, a
   public specification, public tests that express the contract, and a hidden oracle that only runs
   the same public contract plus extra cases. Author-controlled ⇒ no ambiguity about licence, no
   training-data contamination claim beyond the model's general knowledge, offline execution possible.
2. **Public software-engineering benchmark task** re-implemented against our own fixture material:
   only if the task's licence permits use and the original statement can be quoted. Must be recorded
   as derived.
3. **Historical open-source issue** reproduced as a fixture: acceptable, but must be recorded as
   "publicly visible, likely in training data" and never claimed to be contamination-free.

Screening checklist for a candidate (all must pass before `VERIFIED`):

- [ ] Solvable with the existing tool surface (file read/write/edit, grep, list, bash, python) — no new
      tool, no network, no credential.
- [ ] Runs fully offline; test suite completes in < 120 s on 1 CPU.
- [ ] Compatible with the sandbox contract: POSIX shell inside `/workspace/project`, `network="none"`,
      Python 3.
- [ ] Objective oracle: deterministic pytest (or equivalent) with a stable verdict.
- [ ] Licence permits local use; redistribution status recorded.
- [ ] Not used anywhere in this project's history (checked against `experiments/evaluation/tasks.py`
      and every `stage_*` task family).
- [ ] Write surface can be expressed as declared paths (required by the ownership module and by
      candidate-plan evidence).

---

## 4. Selection rules (fixed before inspection)

1. Two tasks per structure for holdout, one per structure for validation.
2. Difficulty spread: each structure gets one "small" (≤2 files touched) and one "medium" (3–6 files).
3. At least one task must be structurally *unsuitable* for delegation (this is what makes
   "avoided delegation" measurable).
4. No task is selected because the current router admits it, or rejected because the current runtime
   fails it.
5. Selection is recorded with the screening checklist output and an exclusion reason for every
   candidate that was dropped.
6. After the holdout is frozen, a task may be withdrawn only for an objective defect (fixture does not
   build, oracle non-deterministic). A withdrawal is reported with the run count it invalidates.

---

## 5. Manifest schema

Every task carries this record. Fields marked *(freeze)* are hashed into the holdout manifest digest
at freeze time.

| Field | Type | Meaning |
|---|---|---|
| `task_id` *(freeze)* | str | stable id, `h-<structure><n>-<slug>` |
| `source` | enum | `authored` / `public-derived` / `issue-derived` |
| `original_task_id` | str/null | upstream id when derived |
| `source_revision` | str/null | commit/tag of the upstream material |
| `license` | str | declared licence at intake; `unknown` is not acceptable for a frozen task |
| `task_family` | enum | `S` / `P` / `C` |
| `public_prompt` *(freeze)* | text | exactly what the model sees |
| `initial_workspace_digest` *(freeze)* | sha256 | digest of the frozen starting tree |
| `environment_requirements` | list | python version, OS shell, packages |
| `required_tool_capabilities` | list | tool ids the task genuinely needs |
| `public_tests` *(freeze)* | list | files/commands visible to the model |
| `hidden_oracle_reference` | str | path to the hidden tests (never shipped to the model) |
| `oracle_digest` *(freeze)* | sha256 | digest of the hidden test bundle |
| `candidate_plan_source` | enum | `external-authored` (the only value in Track C) |
| `candidate_plan_digest` *(freeze)* | sha256 | digest of the frozen candidate DAG |
| `split` *(freeze)* | enum | `validation` / `holdout` |
| `prior_exposure` | enum | `none` / `public` / `project-history` |
| `exclusion_reason` | str/null | why a rejected candidate was dropped |
| `implementation_status` | enum | `PLANNED` / `IMPORTED` / `VERIFIED` / `WITHDRAWN` |

Digests are **`null` until the artifact exists**. This stage fabricates none.

---

## 6. Candidate slate (all PLANNED)

Structure counts: S2, P2, C2 for holdout; S1, P1, C1 for validation.

| Slot | Family | Source strategy | Shape | Status |
|---|---|---|---|---|
| `h-s1-config-migration` | S | authored: two-stage migration of a config file consumed by a second module | serial: schema → consumer update | PLANNED |
| `h-s2-layered-validation` | S | authored: validation pipeline where step 2 depends on step 1's normalised output | serial, medium (3–4 files) | PLANNED |
| `h-p1-parser-and-report` | P | authored: independent parser module + independent report module, joined by a thin CLI | parallel, disjoint writes | PLANNED |
| `h-p2-two-adapters` | P | authored: two independent adapters against one frozen protocol, plus a registry test | parallel, medium | PLANNED |
| `h-c1-shared-state-machine` | C | authored: one state machine touched by two work units (shared write surface) | coupled, high risk of split failure | PLANNED |
| `h-c2-interface-in-flight` | C | authored: an interface change that must land before its consumer | coupled by an unstable interface | PLANNED |
| `v-s1-mini-migration` | S | authored, small | validation smoke | PLANNED |
| `v-p1-two-files` | P | authored, small | validation smoke | PLANNED |
| `v-c1-coupled-edit` | C | authored, small | validation smoke | PLANNED |

Public-benchmark candidates are intentionally **not named** here: none has been downloaded, their
licences have not been checked, and naming a specific task without verifying the licence and the
fixture would create a false record. When a public task is taken, it is entered in this table with its
original id, source revision and licence, marked `IMPORTED`, and only then may it be used.

Explicit non-candidates: anything requiring network access, a credential, a new tool, a GUI, a
language runtime other than Python 3, or more than 6 files of write surface; anything already present
in `experiments/evaluation/tasks.py`.

---

## 7. Candidate-DAG authoring (Track C input)

For each holdout task, one candidate decomposition is authored **before** any run, by a person or
process that sees only: the public prompt, the initial tree, the declared write surface, and the
public tests. Not the hidden oracle, not a previous run's result.

The plan records: worker count, per-worker objectives, dependency edges, declared write paths, and the
merge/verification step. Its digest enters the manifest. The same plan is used for C1 (always multi)
and C2 (selective); C0 (always single) receives the same public information and the same total budget.

---

## 8. Freeze procedure

1. Import the task, run its oracle twice, require identical verdicts.
2. Record `initial_workspace_digest` and `oracle_digest`; mark `VERIFIED`.
3. Author and digest the candidate plan.
4. Compute the holdout manifest digest over every *(freeze)* field.
5. Write the digest, the runtime commit and the environment digest into the run protocol header.
6. Only then start the formal run. Any post-freeze change invalidates the digest and the run.

---

## 9. Contamination and honesty

- Public tasks may be in a model's training data. No claim of "no leakage" is made for
  `public-derived` or `issue-derived` tasks; the manifest records `prior_exposure`.
- Authored tasks reduce, but do not eliminate, contamination (the *pattern* may be familiar). The claim
  is "not used to develop this runtime", not "unseen by the model".
- Holdout results are reported once. Re-running a failed holdout cell after inspecting it converts the
  task into validation data and must be disclosed.
