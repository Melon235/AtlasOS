# ruff: noqa: E501
"""Final-4 Phase 2/3: three validation tasks, their digests, and the frozen pilot manifest.

Tasks are authored for this stage (not reused from the development benchmark, not from the holdout).
Each has: an initial workspace, a public requirement, public tests, a hidden oracle, a candidate DAG,
and computed digests. ``python -m experiments.evaluation.final4.validation_tasks`` writes
``experiments/results/final4/validation_tasks.json`` and ``pilot_manifest.json`` after checking that
every oracle fails on the seed and passes on the reference solution.
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


def _digest(files: dict[str, str]) -> str:
    payload = json.dumps(files, sort_keys=True).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


TASKS: tuple[Task, ...] = (
    Task(
        task_id="validation-serial",
        family="S",
        prompt=(
            "Two steps, strictly ordered. Step 1: implement `clean_rows` in `normalise.py` so that "
            "each row is stripped, lower-cased and empty rows are dropped. Step 2: implement "
            "`build_report` in `report.py`, which must consume `normalise.clean_rows` (do not "
            "re-implement cleaning) and return `{row: count}` sorted by row name."
        ),
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
            {"task_id": "w1", "depends_on": (), "objective": "implement clean_rows"},
            {
                "task_id": "w2",
                "depends_on": ("w1",),
                "objective": "implement build_report on top of it",
            },
        ),
        notes="serial dependency: w2 needs w1's output",
    ),
    Task(
        task_id="validation-parallel",
        family="P",
        prompt=(
            "Two independent modules and one integration step. `tokens.py` must provide "
            "`tokenise(text) -> list[str]` (split on whitespace, lower-case, drop punctuation-only "
            "tokens). `stats.py` must provide `count(tokens) -> dict[str, int]`. `app.py` must provide "
            "`summarise(text)` combining both. The two modules have no dependency on each other."
        ),
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
            {"task_id": "w1", "depends_on": (), "objective": "implement tokens.tokenise"},
            {"task_id": "w2", "depends_on": (), "objective": "implement stats.count"},
            {"task_id": "w3", "depends_on": ("w1", "w2"), "objective": "wire app.summarise"},
        ),
        notes="naturally parallel: w1 and w2 are independent, w3 integrates",
    ),
    Task(
        task_id="validation-coupled",
        family="C",
        prompt=(
            "One shared state machine in `state.py` (`STATES`, `transition(state, event)`) used by "
            "`approve.py` (`approve(state)`) and `ship.py` (`ship(state)`). Both callers must go "
            "through `transition`; adding an event in one caller without extending `STATES` must fail. "
            "Changing the machine and its two callers together is the whole task."
        ),
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
            {"task_id": "w1", "depends_on": (), "objective": "extend STATES + transition"},
            {
                "task_id": "w2",
                "depends_on": ("w1",),
                "objective": "wire approve/ship to the machine",
            },
        ),
        notes="strongly coupled: both callers share one write surface (state.py contract)",
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


def _task_record(task: Task) -> dict[str, object]:
    plan = [
        {
            "task_id": worker["task_id"],
            "depends_on": list(worker["depends_on"]),  # type: ignore[arg-type]
            "objective": worker["objective"],
        }
        for worker in task.workers
    ]
    return {
        "task_id": task.task_id,
        "family": task.family,
        "source": "authored-for-final4",
        "prior_exposure": "none (not used in development, not a holdout task)",
        "prompt": task.prompt,
        "initial_workspace_digest": _digest(task.seed),
        "oracle_digest": _digest(task.oracle_tests),
        "public_tests_digest": _digest(task.public_tests),
        "solution_digest": _digest(task.solution),
        "candidate_plan": plan,
        "candidate_plan_digest": _digest(
            {item["task_id"]: json.dumps(item, sort_keys=True) for item in plan}
        ),
        "notes": task.notes,
        "feasibility": _oracle_feasibility(task),
    }


def _manifest(records: list[dict[str, object]]) -> dict[str, object]:
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "runtime_sha": "93cbd98f9407e7358e997878238520dcac15827a",
        "model": {"provider": "deepseek", "requested": "deepseek-chat", "temperature": 0.0},
        "sandbox": {
            "provider": "opensandbox",
            "endpoint": "localhost:8080",
            "image": "atlas-uat-python:3.12",
            "network": "none",
            "cpu": "1",
            "memory": "1Gi",
        },
        "context_policy": "task_scoped + stable_deterministic + identity",
        "handoff_mode": "legacy (dual is refused with task_scoped)",
        "search_backend": "keyword",
        "budget": {
            "tokens_per_task": 120000,
            "max_tool_calls": 40,
            "turns_per_worker": 14,
            "wall_clock_seconds": 900,
            "run_level": "one shared budget per run",
        },
        "oracle": "independent, sandboxed hidden tests",
        "router": {"policy_version": "r0.1.0", "mode": "provided-proposal", "modified": False},
        "configurations": {
            "N0": "native AnyCode 0.9.0 single, neutral sandbox backend",
            "R0": "Atlas single: progressive tools, gate, ledger, binding, budget, trajectory",
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
            for config in ("N0", "R0", "R1", "R2")
        ],
        "budget_envelope": {"max_task_runs": 24, "max_usd": 2.0, "spent_usd": 0.0},
        "ablations": {
            "X0": "upstream_default context",
            "X1": "task_scoped + stable_deterministic + identity",
            "tool_disclosure": "full exposure vs progressive",
        },
    }


def main() -> int:
    records = [_task_record(task) for task in TASKS]
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "validation_tasks.json").write_text(
        json.dumps({"tasks": records}, indent=2, sort_keys=True), encoding="utf-8"
    )
    manifest = _manifest(records)
    (OUT / "pilot_manifest.json").write_text(
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
        )
    print("run_units", len(manifest["run_units"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
