# ruff: noqa: E501
"""Final-4 Phase 2/3: three validation tasks, their digests, and the frozen pilot manifest.

Tasks are authored for this stage (not reused from the development benchmark, not from the holdout).
Each has: an initial workspace, a public requirement, public tests, a hidden oracle, a candidate DAG,
and computed digests.

Revision 2 (2026-11-14) adds the missing half of the task definition. In revision 1 each worker step
carried one sentence ("implement clean_rows"), so a multi-worker arm knew strictly less than the
single-worker arm that received the whole requirement. Revision 2 splits every task into:

* ``shared_context`` — the public requirement, the public interface contract, the data constraints and
  the public acceptance. Every worker of the sample receives it verbatim, including the single-worker
  arm.
* ``workers[i]["assignment"]`` — that worker's own objective, owned module and step-level definition
  of done. It is the text the router reads for declared write paths, so it names only the files that
  worker owns, and the write sets stay disjoint.
* dependency outputs — never authored here. They are produced by the run and projected by whichever
  prompt builder the arm configures (``handoff`` legacy, or the Atlas Context Plane).

Revision 3 populates ``BenchmarkTask.required_artifacts`` from the public requirement text. Before that
the field was ``()``, so the router's goal-coverage evidence could never be proven and every candidate
was blocked at ``GATE_0`` with ``UNCOVERED_GOAL``. Nothing else about a task changes: prompts, shared
context, candidate plan, assignments, seed, oracle and solution all keep their revision-2 digests, and
that equality is asserted by ``tests/integration/test_final4_context_plane.py``.

Revision 1 and 2 files are left untouched. Each revision records, per task, every digest it
supersedes, so the difference is auditable instead of implied.

``python -m experiments.evaluation.final4.validation_tasks`` regenerates the revision-3 files after
checking that every oracle fails on the seed and passes on the reference solution.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

OUT = Path(__file__).resolve().parents[2] / "results" / "final4"
REVISION = 3
TASK_SET_ID = "final4-validation"
DATASET_VERSION = "final4-validation-3"

SHARED_HEADER = "[SHARED TASK CONTEXT — identical for every worker on this task]"
ASSIGNMENT_HEADER = "[YOUR ASSIGNMENT — worker {task_id}]"


@dataclass(frozen=True)
class Task:
    task_id: str
    family: str
    prompt: str
    seed: dict[str, str]
    solution: dict[str, str]
    public_tests: dict[str, str]
    oracle_tests: dict[str, str]
    workers: tuple[dict[str, object], ...] = field(default_factory=tuple)
    notes: str = ""
    interface: str = ""
    constraints: str = ""
    shared_context: str = ""
    #: Files the public requirement asks for. Fed to ``BenchmarkTask.required_artifacts``.
    deliverables: tuple[str, ...] = ()


def _digest(files: dict[str, str]) -> str:
    payload = json.dumps(files, sort_keys=True).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _text_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _shared(requirement: str, interface: str, constraints: str) -> str:
    """Render the block every worker of a task receives, in one fixed shape."""

    return "\n".join(
        [
            SHARED_HEADER,
            "",
            "PUBLIC REQUIREMENT",
            requirement,
            "",
            "PUBLIC INTERFACE CONTRACT",
            interface,
            "",
            "DATA CONSTRAINTS",
            constraints,
            "",
            "PUBLIC ACCEPTANCE",
            "- `test_public.py` is present in the project and must pass; run it with "
            "`python -m pytest -q test_public.py`.",
            "- Do not edit `test_public.py`. Hidden tests exist and are not visible; they check the "
            "same contract more strictly.",
            "",
            "WORKSPACE RULES",
            "- The project is at `/workspace/project`. Read any project file you need.",
            "- Do not search hidden tests, scoring output, evaluation artifacts, Git history or any "
            "other location for a reference implementation. They are not part of this task.",
            "- Report the files you changed, the checks you ran, and their real result.",
        ]
    )


SERIAL_REQUIREMENT = (
    "Two steps, strictly ordered. Step 1: implement `clean_rows` in `normalise.py` so that each row "
    "is stripped, lower-cased and empty rows are dropped. Step 2: implement `build_report` in "
    "`report.py`, which must consume `normalise.clean_rows` (do not re-implement cleaning) and "
    "return `{row: count}` sorted by row name. Requirement: `report.py` must import `clean_rows` at "
    "module level as `from normalise import clean_rows` and call that name, so that "
    "`report.clean_rows` can be monkeypatched; a hidden test replaces `report.clean_rows` and expects "
    "`build_report` to use the replacement."
)
SERIAL_INTERFACE = (
    "- `normalise.clean_rows(rows: list[str]) -> list[str]`\n"
    "- `report.build_report(rows: list[str]) -> dict[str, int]`\n"
    "- `report.clean_rows` is a module-level name bound by `from normalise import clean_rows`; "
    "`build_report` must call that name, not `normalise.clean_rows`."
)
SERIAL_CONSTRAINTS = (
    "- Rows are plain strings; input order matters for nothing except stability of the result.\n"
    "- `clean_rows(['  X ', 'y'])` returns `['x', 'y']`; `clean_rows(['   ', ''])` returns `[]`.\n"
    "- `build_report` returns counts sorted by row name: "
    "`list(build_report(['b', 'A', 'a']).items()) == [('a', 2), ('b', 1)]`.\n"
    "- Trimming and lower-casing are `clean_rows`' responsibility only."
)
SERIAL_SHARED = _shared(SERIAL_REQUIREMENT, SERIAL_INTERFACE, SERIAL_CONSTRAINTS)

PARALLEL_REQUIREMENT = (
    "Two independent modules and one integration step. `tokens.py` must provide "
    "`tokenise(text) -> list[str]` (split on whitespace, lower-case, drop punctuation-only "
    "tokens). `stats.py` must provide `count(tokens) -> dict[str, int]`. `app.py` must provide "
    "`summarise(text)` combining both. The two modules have no dependency on each other."
)
PARALLEL_INTERFACE = (
    "- `tokens.tokenise(text: str) -> list[str]`\n"
    "- `stats.count(tokens: list[str]) -> dict[str, int]`\n"
    "- `app.summarise(text: str) -> dict[str, int]`, equal to `count(tokenise(text))`"
)
PARALLEL_CONSTRAINTS = (
    "- `tokenise('Hi, hi!')` returns `['hi', 'hi']`; punctuation-only tokens are dropped.\n"
    "- `tokenise('... ok -- ok')` returns `['ok', 'ok']`.\n"
    "- `count(['a', 'a'])` returns `{'a': 2}`; `summarise('')` returns `{}`.\n"
    "- `tokens.py` and `stats.py` must not import each other."
)
PARALLEL_SHARED = _shared(PARALLEL_REQUIREMENT, PARALLEL_INTERFACE, PARALLEL_CONSTRAINTS)

COUPLED_REQUIREMENT = (
    "One shared state machine in `state.py` (`STATES`, `transition(state, event)`) used by "
    "`approve.py` (`approve(state)`) and `ship.py` (`ship(state)`). Both callers must go through "
    "`transition`; adding an event in one caller without extending `STATES` must fail. Changing "
    "the machine and its two callers together is the whole task."
)
COUPLED_INTERFACE = (
    "- `state.STATES: dict[str, dict[str, str]]` maps a state to its legal `{event: next_state}`\n"
    "- `state.transition(state: str, event: str) -> str`, raising `ValueError` for an unknown "
    "state or an illegal event\n"
    "- `approve.approve(state: str) -> str` calls `transition(state, 'approve')`\n"
    "- `ship.ship(state: str) -> str` calls `transition(state, 'ship')`"
)
COUPLED_CONSTRAINTS = (
    "- Legal transitions: `draft --approve--> approved`, `approved --ship--> shipped`; `shipped` is "
    "terminal.\n"
    "- `approve('draft') == 'approved'`; `ship('approved') == 'shipped'`.\n"
    "- `ship('draft')`, `approve('nowhere')` and `approve('shipped')` must all raise `ValueError`.\n"
    "- Callers must not hard-code transitions that `STATES` does not contain."
)
COUPLED_SHARED = _shared(COUPLED_REQUIREMENT, COUPLED_INTERFACE, COUPLED_CONSTRAINTS)

TASKS: tuple[Task, ...] = (
    Task(
        task_id="validation-serial",
        family="S",
        prompt=SERIAL_REQUIREMENT,
        shared_context=SERIAL_SHARED,
        # Public deliverables, read straight off the public requirement ("Create normalise.py ...",
        # "Create report.py ..."). Not derived from the solution, the oracle or any model output.
        deliverables=("normalise.py", "report.py"),
        seed={
            "normalise.py": "def clean_rows(rows):\n    raise NotImplementedError\n",
            "report.py": (
                "from normalise import clean_rows\n\n\n"
                "def build_report(rows):\n    raise NotImplementedError\n"
            ),
            "README.md": "# serial task\n\nStep 1 normalise, step 2 report.\n",
        },
        solution={
            "normalise.py": (
                "def clean_rows(rows):\n"
                "    cleaned = [row.strip().lower() for row in rows]\n"
                "    return [row for row in cleaned if row]\n"
            ),
            "report.py": (
                "from normalise import clean_rows\n\n\n"
                "def build_report(rows):\n"
                "    counts = {}\n"
                "    for row in clean_rows(rows):\n"
                "        counts[row] = counts.get(row, 0) + 1\n"
                "    return dict(sorted(counts.items()))\n"
            ),
        },
        public_tests={
            "test_public.py": (
                "from normalise import clean_rows\nfrom report import build_report\n\n\n"
                "def test_clean():\n    assert clean_rows([' A ', '']) == ['a']\n\n\n"
                "def test_report():\n    assert build_report(['b', 'a', 'a']) == {'a': 2, 'b': 1}\n"
            )
        },
        oracle_tests={
            "test_hidden.py": (
                "from normalise import clean_rows\nfrom report import build_report\n\n\n"
                "def test_trims_and_lowers():\n"
                "    assert clean_rows(['  X ', 'y']) == ['x', 'y']\n\n\n"
                "def test_drops_blank_after_strip():\n"
                "    assert clean_rows(['   ', '']) == []\n\n\n"
                "def test_report_is_sorted_and_counts():\n"
                "    assert list(build_report(['b', 'A', 'a']).items()) == [('a', 2), ('b', 1)]\n\n\n"
                "def test_report_uses_normalise(monkeypatch):\n"
                "    import report\n"
                "    monkeypatch.setattr(report, 'clean_rows', lambda rows: ['z'])\n"
                "    assert report.build_report(['ignored']) == {'z': 1}\n"
            )
        },
        workers=(
            {
                "task_id": "w1",
                "depends_on": (),
                "objective": "implement clean_rows",
                "owned_paths": ("normalise.py",),
                "assignment": (
                    "[YOUR ASSIGNMENT — worker w1]\n"
                    "You own step 1 of 2: the row cleaning function.\n\n"
                    "OBJECTIVE: Implement `clean_rows(rows)` in `normalise.py` so that each row is "
                    "stripped, lower-cased, and rows that are empty after stripping are dropped.\n\n"
                    "OWNED MODULE: `normalise.py` — you are the only worker who may create or modify "
                    "it. `report.py` belongs to the other worker: read it if you need to, but do not "
                    "edit it.\n\n"
                    "DEPENDENCIES: none. Your output is the dependency of step 2.\n\n"
                    "DEFINITION OF DONE: `normalise.clean_rows` is implemented (no "
                    "`NotImplementedError` left), `python -m pytest -q test_public.py` passes for "
                    "`test_clean`, and you report what you ran and what it printed."
                ),
            },
            {
                "task_id": "w2",
                "depends_on": ("w1",),
                "objective": "implement build_report on top of it",
                "owned_paths": ("report.py",),
                "assignment": (
                    "[YOUR ASSIGNMENT — worker w2]\n"
                    "You own step 2 of 2: the report builder that consumes step 1.\n\n"
                    "OBJECTIVE: Implement `build_report(rows)` in `report.py`. It must return "
                    "`{row: count}` sorted by row name and must call the module-level name "
                    "`clean_rows` (bound by `from normalise import clean_rows`) rather than "
                    "re-implementing cleaning.\n\n"
                    "OWNED MODULE: `report.py` — you are the only worker who may create or modify "
                    "it. `normalise.py` belongs to the other worker: read it, do not edit it.\n\n"
                    "DEPENDENCIES: step 1 (`w1`) writes `normalise.py`. Its actual result is provided "
                    "to you in this prompt when available; if it is absent or still unimplemented, "
                    "implement against the published interface and say so in your report.\n\n"
                    "DEFINITION OF DONE: `build_report` is implemented (no `NotImplementedError` "
                    "left), `python -m pytest -q test_public.py` passes for `test_report`, and the "
                    "module-level `clean_rows` name is the one your function calls."
                ),
            },
        ),
        notes="serial dependency: w2 needs w1's output",
        interface=SERIAL_INTERFACE,
        constraints=SERIAL_CONSTRAINTS,
    ),
    Task(
        task_id="validation-parallel",
        family="P",
        prompt=PARALLEL_REQUIREMENT,
        shared_context=PARALLEL_SHARED,
        deliverables=("app.py", "stats.py", "tokens.py"),
        seed={
            "tokens.py": "def tokenise(text):\n    raise NotImplementedError\n",
            "stats.py": "def count(tokens):\n    raise NotImplementedError\n",
            "app.py": (
                "from stats import count\nfrom tokens import tokenise\n\n\n"
                "def summarise(text):\n    raise NotImplementedError\n"
            ),
            "README.md": "# parallel task\n\ntokens and stats are independent; app integrates.\n",
        },
        solution={
            "tokens.py": (
                "import string\n\n\n"
                "def tokenise(text):\n"
                "    out = []\n"
                "    for raw in text.split():\n"
                "        token = raw.strip(string.punctuation).lower()\n"
                "        if token:\n"
                "            out.append(token)\n"
                "    return out\n"
            ),
            "stats.py": (
                "def count(tokens):\n"
                "    result = {}\n"
                "    for token in tokens:\n"
                "        result[token] = result.get(token, 0) + 1\n"
                "    return result\n"
            ),
            "app.py": (
                "from stats import count\nfrom tokens import tokenise\n\n\n"
                "def summarise(text):\n    return count(tokenise(text))\n"
            ),
        },
        public_tests={
            "test_public.py": (
                "from app import summarise\nfrom stats import count\nfrom tokens import tokenise\n\n\n"
                "def test_tokenise():\n    assert tokenise('Hi, hi!') == ['hi', 'hi']\n\n\n"
                "def test_count():\n    assert count(['a', 'a']) == {'a': 2}\n\n\n"
                "def test_summarise():\n    assert summarise('a b a') == {'a': 2, 'b': 1}\n"
            )
        },
        oracle_tests={
            "test_hidden.py": (
                "from app import summarise\nfrom tokens import tokenise\n\n\n"
                "def test_punctuation_only_tokens_dropped():\n"
                "    assert tokenise('... ok -- ok') == ['ok', 'ok']\n\n\n"
                "def test_case_insensitive():\n"
                "    assert summarise('Go go GO') == {'go': 3}\n\n\n"
                "def test_empty_text():\n    assert summarise('') == {}\n"
            )
        },
        workers=(
            {
                "task_id": "w1",
                "depends_on": (),
                "objective": "implement tokens.tokenise",
                "owned_paths": ("tokens.py",),
                "assignment": (
                    "[YOUR ASSIGNMENT — worker w1]\n"
                    "You own one of the two independent modules.\n\n"
                    "OBJECTIVE: Implement `tokenise(text)` in `tokens.py`: split on whitespace, "
                    "lower-case every token, and drop tokens that contain no alphanumeric character.\n\n"
                    "OWNED MODULE: `tokens.py`. `stats.py` and `app.py` belong to the other workers: "
                    "read them if you need to, do not edit them.\n\n"
                    "DEPENDENCIES: none. Nothing you write blocks the other module; `app.py` "
                    "integration depends on both.\n\n"
                    "DEFINITION OF DONE: `tokenise` is implemented (no `NotImplementedError` left), "
                    "`python -m pytest -q test_public.py` passes for `test_tokenise`, and you report "
                    "what you ran and what it printed."
                ),
            },
            {
                "task_id": "w2",
                "depends_on": (),
                "objective": "implement stats.count",
                "owned_paths": ("stats.py",),
                "assignment": (
                    "[YOUR ASSIGNMENT — worker w2]\n"
                    "You own one of the two independent modules.\n\n"
                    "OBJECTIVE: Implement `count(tokens)` in `stats.py`: return a dictionary mapping "
                    "each token to its number of occurrences.\n\n"
                    "OWNED MODULE: `stats.py`. `tokens.py` and `app.py` belong to the other workers: "
                    "read them if you need to, do not edit them.\n\n"
                    "DEPENDENCIES: none. Nothing you write blocks the other module; `app.py` "
                    "integration depends on both.\n\n"
                    "DEFINITION OF DONE: `count` is implemented (no `NotImplementedError` left), "
                    "`python -m pytest -q test_public.py` passes for `test_count`, and you report "
                    "what you ran and what it printed."
                ),
            },
            {
                "task_id": "w3",
                "depends_on": ("w1", "w2"),
                "objective": "wire app.summarise",
                "owned_paths": ("app.py",),
                "assignment": (
                    "[YOUR ASSIGNMENT — worker w3]\n"
                    "You own the integration step; the two modules are independent of each other.\n\n"
                    "OBJECTIVE: Implement `summarise(text)` in `app.py` so that it returns "
                    "`count(tokenise(text))`, using the names already imported at module level.\n\n"
                    "OWNED MODULE: `app.py`. `tokens.py` and `stats.py` belong to the other workers: "
                    "read them, do not edit them.\n\n"
                    "DEPENDENCIES: steps `w1` and `w2`. Their actual results are provided to you in "
                    "this prompt when available; if a module is still unimplemented, wire against "
                    "the published interface and say so in your report.\n\n"
                    "DEFINITION OF DONE: `summarise` is implemented (no `NotImplementedError` left), "
                    "`python -m pytest -q test_public.py` passes for `test_summarise`, and you report "
                    "what you ran and what it printed."
                ),
            },
        ),
        notes="naturally parallel: w1 and w2 are independent, w3 integrates",
        interface=PARALLEL_INTERFACE,
        constraints=PARALLEL_CONSTRAINTS,
    ),
    Task(
        task_id="validation-coupled",
        family="C",
        prompt=COUPLED_REQUIREMENT,
        shared_context=COUPLED_SHARED,
        deliverables=("approve.py", "ship.py", "state.py"),
        seed={
            "state.py": (
                "STATES = {}\n\n\ndef transition(state, event):\n    raise NotImplementedError\n"
            ),
            "approve.py": "from state import transition\n\n\ndef approve(state):\n    raise NotImplementedError\n",
            "ship.py": "from state import transition\n\n\ndef ship(state):\n    raise NotImplementedError\n",
            "README.md": "# coupled task\n\nstate machine + two callers share one contract.\n",
        },
        solution={
            "state.py": (
                "STATES = {\n"
                "    'draft': {'approve': 'approved'},\n"
                "    'approved': {'ship': 'shipped'},\n"
                "    'shipped': {},\n"
                "}\n\n\n"
                "def transition(state, event):\n"
                "    if state not in STATES:\n"
                "        raise ValueError(f'unknown state {state}')\n"
                "    if event not in STATES[state]:\n"
                "        raise ValueError(f'illegal event {event} in {state}')\n"
                "    return STATES[state][event]\n"
            ),
            "approve.py": (
                "from state import transition\n\n\ndef approve(state):\n    return transition(state, 'approve')\n"
            ),
            "ship.py": (
                "from state import transition\n\n\ndef ship(state):\n    return transition(state, 'ship')\n"
            ),
        },
        public_tests={
            "test_public.py": (
                "from approve import approve\nfrom ship import ship\n\n\n"
                "def test_approve():\n    assert approve('draft') == 'approved'\n\n\n"
                "def test_ship():\n    assert ship('approved') == 'shipped'\n"
            )
        },
        oracle_tests={
            "test_hidden.py": (
                "import pytest\n\nfrom approve import approve\nfrom ship import ship\n\n\n"
                "def test_illegal_event_rejected():\n"
                "    with pytest.raises(ValueError):\n        ship('draft')\n\n\n"
                "def test_unknown_state_rejected():\n"
                "    with pytest.raises(ValueError):\n        approve('nowhere')\n\n\n"
                "def test_terminal_state(): \n"
                "    with pytest.raises(ValueError):\n        approve('shipped')\n"
            )
        },
        workers=(
            {
                "task_id": "w1",
                "depends_on": (),
                "objective": "extend STATES + transition",
                "owned_paths": ("state.py",),
                "assignment": (
                    "[YOUR ASSIGNMENT — worker w1]\n"
                    "You own the shared contract that both callers depend on.\n\n"
                    "OBJECTIVE: Implement `STATES` in `state.py` so it encodes the three states and "
                    "the two legal transitions, and implement `transition(state, event)` to return "
                    "the next state and raise `ValueError` for an unknown state or an illegal "
                    "event.\n\n"
                    "OWNED MODULE: `state.py` — you are the only worker who may create or modify it. "
                    "`approve.py` and `ship.py` belong to the other worker: read them, do not edit "
                    "them.\n\n"
                    "DEPENDENCIES: none. The other worker's callers are written against the contract "
                    "you publish here, so keep the shapes exactly as the shared contract states.\n\n"
                    "DEFINITION OF DONE: no `NotImplementedError` is left in `state.py`, every "
                    "declared transition is reachable through `transition`, and you report what you "
                    "ran and what it printed."
                ),
            },
            {
                "task_id": "w2",
                "depends_on": ("w1",),
                "objective": "wire approve/ship to the machine",
                "owned_paths": ("approve.py", "ship.py"),
                "assignment": (
                    "[YOUR ASSIGNMENT — worker w2]\n"
                    "You own the two callers of the shared state machine.\n\n"
                    "OBJECTIVE: Implement `approve(state)` in `approve.py` and `ship(state)` in "
                    "`ship.py` so both delegate to `transition` with their own event name, and let "
                    "`ValueError` propagate unchanged.\n\n"
                    "OWNED MODULES: `approve.py` and `ship.py` — you are the only worker who may "
                    "create or modify them. `state.py` belongs to the other worker: read it, do not "
                    "edit it.\n\n"
                    "DEPENDENCIES: step `w1` writes `state.py`. Its actual result is provided to you "
                    "in this prompt when available; if `STATES` is still empty, implement against the "
                    "published contract and say so in your report.\n\n"
                    "DEFINITION OF DONE: no `NotImplementedError` is left in either caller, "
                    "`python -m pytest -q test_public.py` passes, and no caller hard-codes a "
                    "transition that `STATES` does not contain."
                ),
            },
        ),
        notes="strongly coupled: both callers share one write surface (state.py contract)",
        interface=COUPLED_INTERFACE,
        constraints=COUPLED_CONSTRAINTS,
    ),
)


def _materialise(files: dict[str, str]) -> Path:
    root = Path(tempfile.mkdtemp(prefix="final4-task-"))
    for name, content in files.items():
        (root / name).write_text(content, encoding="utf-8")
    return root


def _pytest(root: Path, tests: dict[str, str]) -> tuple[int, str]:
    for name, content in tests.items():
        (root / name).write_text(content, encoding="utf-8")
    try:
        done = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=180,
        )
        return done.returncode, (done.stdout + done.stderr)[-400:]
    finally:
        for name in tests:
            (root / name).unlink(missing_ok=True)


def _oracle_feasibility(task: Task) -> dict[str, object]:
    seed_dir = _materialise(task.seed)
    seed_code, _ = _pytest(seed_dir, task.oracle_tests)
    solved_dir = _materialise({**task.seed, **task.solution})
    solved_code, solved_tail = _pytest(solved_dir, task.oracle_tests)
    public_code, public_tail = _pytest(
        _materialise({**task.seed, **task.solution}), task.public_tests
    )
    return {
        "oracle_fails_on_seed": seed_code != 0,
        "oracle_passes_on_solution": solved_code == 0,
        "public_tests_pass_on_solution": public_code == 0,
        "oracle_tail": solved_tail.strip().splitlines()[-1:] if solved_code else [],
        "public_tail": public_tail.strip().splitlines()[-1:] if public_code else [],
    }


def _plan(task: Task) -> list[dict[str, object]]:
    return [
        {
            "task_id": worker["task_id"],
            "depends_on": list(worker["depends_on"]),  # type: ignore[arg-type]
            "objective": worker["objective"],
            "owned_paths": list(worker["owned_paths"]),  # type: ignore[arg-type]
            "assignment_digest": _text_digest(str(worker["assignment"])),
        }
        for worker in task.workers
    ]


def _proposal_view(task: Task) -> list[dict[str, object]]:
    """Exactly what the router is given, in a stable form, so its input can be digested."""

    return [
        {
            "task_id": worker["task_id"],
            "depends_on": list(worker["depends_on"]),  # type: ignore[arg-type]
            "prompt": str(worker["assignment"]),
            "declared_write_paths": list(worker["owned_paths"]),  # type: ignore[arg-type]
            "required_artifacts": list(task.deliverables),
        }
        for worker in task.workers
    ]


def _task_record(task: Task) -> dict[str, object]:
    plan = _plan(task)
    proposal = _proposal_view(task)
    return {
        "task_id": task.task_id,
        "family": task.family,
        "source": "authored-for-final4",
        "prior_exposure": "none (not used in development, not a holdout task)",
        "prompt": task.prompt,
        "shared_context": task.shared_context,
        "shared_context_digest": _text_digest(task.shared_context),
        "initial_workspace_digest": _digest(task.seed),
        "oracle_digest": _digest(task.oracle_tests),
        "public_tests_digest": _digest(task.public_tests),
        "solution_digest": _digest(task.solution),
        "deliverables": list(task.deliverables),
        "task_metadata_digest": _text_digest(
            json.dumps(
                {
                    "deliverables": list(task.deliverables),
                    "allowed_paths_source": "task record",
                    "sample_id": task.task_id,
                },
                sort_keys=True,
            )
        ),
        "router_proposal": proposal,
        "router_proposal_digest": _text_digest(json.dumps(proposal, sort_keys=True)),
        "candidate_plan": plan,
        "candidate_plan_digest": _digest(
            {item["task_id"]: json.dumps(item, sort_keys=True) for item in plan}
        ),
        "notes": task.notes,
        "feasibility": _oracle_feasibility(task),
    }


def _previous_records() -> dict[str, dict[str, object]]:
    """Earlier revisions, so every revision records exactly what it supersedes."""

    records: dict[str, dict[str, object]] = {}
    for name in ("validation_tasks.json", "validation_tasks_v2.json"):
        path = OUT / name
        if not path.is_file():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        for record in payload.get("tasks", []):
            entries = records.setdefault(str(record["task_id"]), {})
            entries[name] = record
    return records


def _supersedes(records: list[dict[str, object]]) -> dict[str, object]:
    previous = _previous_records()
    entries: dict[str, object] = {}
    for record in records:
        known = previous.get(str(record["task_id"]))
        if not known:
            continue
        entries[str(record["task_id"])] = {
            name: {
                "file": f"experiments/results/final4/{name}",
                "revision": 1 if name == "validation_tasks.json" else 2,
                "candidate_plan_digest": old.get("candidate_plan_digest"),
                "initial_workspace_digest": old.get("initial_workspace_digest"),
                "oracle_digest": old.get("oracle_digest"),
                "public_tests_digest": old.get("public_tests_digest"),
                "solution_digest": old.get("solution_digest"),
                "shared_context_digest": old.get("shared_context_digest"),
                "deliverables": list(old.get("deliverables") or []),
            }
            for name, old in known.items()
        }
    return entries


def _manifest(records: list[dict[str, object]], supersedes: dict[str, object]) -> dict[str, object]:
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "revision": REVISION,
        "dataset_version": DATASET_VERSION,
        "runtime_sha": "93cbd98f9407e7358e997878238520dcac15827a",
        "classification": "CORRECTED_EVAL_TASK_SET",
        "supersedes": supersedes,
        "model": {"provider": "deepseek", "requested": "deepseek-chat", "temperature": 0.0},
        "sandbox": {
            "provider": "opensandbox",
            "endpoint": "localhost:8080",
            "image": "atlas-uat-python:3.12",
            "network": "none",
            "cpu": "1",
            "memory": "1Gi",
        },
        "context_policy": {
            "X0": "upstream_default + legacy handoff",
            "X1": "task_scoped + stable_deterministic + identity (identical to validation-serial-R1)",
            "R0": "task_scoped + stable_deterministic + identity",
            "R1": "task_scoped + stable_deterministic + identity",
            "R2": "task_scoped + stable_deterministic + identity",
        },
        "context_config_file": "experiments/configs/final4-context.yaml",
        "handoff_mode": "legacy (dual is refused with task_scoped)",
        "search_backend": "keyword",
        "budget": {
            "tokens_per_task": 120000,
            "max_tool_calls": 40,
            "turns_per_worker": 14,
            "wall_clock_seconds": 900,
            "run_level": "one shared budget per run",
        },
        "tool_error_mode": "corrected (identical in every arm; see docs/final-4-legacy-pilot-reclassification.md §2)",
        "oracle": "independent, sandboxed hidden tests (unmodified)",
        "router": {"policy_version": "r0.1.0", "mode": "provided-proposal", "modified": False},
        "configurations": {
            "N0": "native AnyCode 0.9.0 single, neutral sandbox backend (still blocked)",
            "X0": "Atlas multi over the candidate DAG with upstream_default context + legacy handoff",
            "X1": "Atlas multi over the candidate DAG with the Atlas Context Plane (== validation-serial-R1)",
            "R0": "Atlas single, Context Plane on: progressive tools, gate, ledger, binding, budget, trajectory",
            "R1": "Atlas multi: R0 core + frozen candidate DAG",
            "R2": "Atlas router: same core, DIRECT -> R0 path, else R1 DAG",
        },
        "run_units": [
            {
                "unit_id": f"{record['task_id']}-{config}",
                "task_id": record["task_id"],
                "configuration": config,
                "candidate_plan_digest": record["candidate_plan_digest"],
                "status": "PLANNED",
            }
            for record in records
            for config in ("N0", "X0", "R0", "R1", "R2")
        ],
        "budget_envelope": {
            "max_task_runs": 24,
            "max_usd": 2.0,
            "spent_before_this_round_runs": 14,
            "spent_usd_before_this_round": 0.05996018,
        },
        "ablations": {
            "X0_vs_X1": "same tasks, same DAG, same assignment, same prompts; the only variable is which prompt builder projects the dependency output",
            "tool_disclosure": "full exposure vs progressive (Phase 4 of this round)",
            "placement": "P0 fixed assignment vs P1 context-locality assignment (not run: see docs/final-4-placement-integration-proposal.md)",
        },
    }


def main() -> int:
    records = [_task_record(task) for task in TASKS]
    OUT.mkdir(parents=True, exist_ok=True)
    supersedes = _supersedes(records)
    (OUT / "validation_tasks_v3.json").write_text(
        json.dumps(
            {
                "revision": REVISION,
                "dataset_version": DATASET_VERSION,
                "change_from_revision_2": (
                    "only BenchmarkTask.required_artifacts is populated, from the public "
                    "requirement text; prompts, shared context, candidate plan, assignments, "
                    "seed, oracle and solution are unchanged"
                ),
                "supersedes": supersedes,
                "tasks": records,
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    manifest = _manifest(records, supersedes)
    (OUT / "pilot_manifest_v3.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )
    for record in records:
        feasibility = record["feasibility"]
        print(
            record["task_id"],
            "seed_oracle_fails=",
            feasibility["oracle_fails_on_seed"],
            "solution_oracle_passes=",
            feasibility["oracle_passes_on_solution"],
            "public_passes=",
            feasibility["public_tests_pass_on_solution"],
            "plan_digest=",
            str(record["candidate_plan_digest"])[:16],
        )
    print("run_units", len(manifest["run_units"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
