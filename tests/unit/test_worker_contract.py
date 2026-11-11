"""Stage G3-3: the worker contract is derived from public task text only."""

from __future__ import annotations

import pytest
from experiments.evaluation.models import WorkerStep
from experiments.evaluation.stage_e_tasks import DEFINITIONS, definition_by_id
from experiments.evaluation.stage_g3_worker_contracts import (
    ENV_TAIL,
    UNSPECIFIED,
    WORKER_CONTRACT_LEGACY,
    WORKER_CONTRACT_STRUCTURED,
    build_contract,
    classify_paths,
    contract_overhead,
    worker_task_text,
)

SECTIONS = (
    "[WORKER TASK CONTRACT]",
    "TASK GOAL",
    "WORKSPACE SCOPE",
    "FILE OWNERSHIP",
    "DEPENDENCIES",
    "DELIVERABLES",
    "VERIFICATION",
    "COMPLETION REPORT",
)


def _text(definition_id: str, step_id: str, mode: str) -> str:
    definition = definition_by_id(definition_id)
    step = next(item for item in definition.workers if item.task_id == step_id)
    return worker_task_text(
        step, steps=definition.workers, sample_id=definition.sample_id, mode=mode
    )


def test_legacy_text_is_object_identical() -> None:
    for definition in DEFINITIONS:
        for step in definition.workers:
            assert (
                worker_task_text(
                    step,
                    steps=definition.workers,
                    sample_id=definition.sample_id,
                    mode=WORKER_CONTRACT_LEGACY,
                )
                is step.prompt
            )


def test_structured_contract_contains_every_section() -> None:
    text = _text("e1-02-report", "render", WORKER_CONTRACT_STRUCTURED)
    for section in SECTIONS:
        assert section in text


def test_original_goal_is_preserved() -> None:
    definition = definition_by_id("e1-02-report")
    step = next(item for item in definition.workers if item.task_id == "render")
    text = worker_task_text(
        step,
        steps=definition.workers,
        sample_id=definition.sample_id,
        mode=WORKER_CONTRACT_STRUCTURED,
    )

    # The public task text survives verbatim; the contract does not repeat its environment
    # sentence (SCOPE re-expresses it once).
    assert step.prompt in text
    assert text.count(ENV_TAIL) == 1
    assert "Do not modify tests/ or files outside your owned paths." in text


def test_declared_dependencies_are_kept() -> None:
    exporter = _text("e1-07-export", "exporter", WORKER_CONTRACT_STRUCTURED)
    assert "Declared upstream steps: formatter." in exporter

    render = _text("e1-02-report", "render", WORKER_CONTRACT_STRUCTURED)
    assert "no upstream dependency" in render
    assert "references sibling work: parser, stats" in render


def test_ownership_is_never_invented() -> None:
    step = WorkerStep("solo", "generic-solo", "Do something useful in the project.")
    contract = build_contract(step, steps=(step,), sample_id="generic")
    assert contract.owned_files == ()
    assert contract.excluded_files == ()
    assert UNSPECIFIED in contract.render()


def test_deliverables_are_not_fabricated() -> None:
    exporter = build_contract(
        next(
            item for item in definition_by_id("e1-07-export").workers if item.task_id == "exporter"
        ),
        steps=definition_by_id("e1-07-export").workers,
        sample_id="e1-07-export",
    )
    # formatter.py is read, not owned: it must not appear as a deliverable.
    assert exporter.owned_files == ("exporter.py",)
    assert "formatter.py" not in exporter._deliverable_line()

    unspecified = build_contract(
        WorkerStep("solo", "generic-solo", "Do something useful."),
        steps=(WorkerStep("solo", "generic-solo", "Do something useful."),),
        sample_id="generic",
    )
    assert UNSPECIFIED in unspecified._deliverable_line()


@pytest.mark.parametrize(
    "forbidden",
    [
        "test_hidden_",
        "assert ",
        "mean=4.0;max=6;min=2",
        "format_row(['a', 'bb']",
        "pytest.raises",
        "hidden_files",
        "oracle",
    ],
)
def test_contract_contains_no_hidden_oracle_content(forbidden: str) -> None:
    for definition in DEFINITIONS:
        for step in definition.workers:
            text = worker_task_text(
                step,
                steps=definition.workers,
                sample_id=definition.sample_id,
                mode=WORKER_CONTRACT_STRUCTURED,
            )
            assert forbidden not in text


def test_building_does_not_mutate_the_original_steps() -> None:
    definition = definition_by_id("e1-02-report")
    before = tuple(
        (step.task_id, step.prompt, step.depends_on, id(step)) for step in definition.workers
    )
    for step in definition.workers:
        worker_task_text(
            step,
            steps=definition.workers,
            sample_id=definition.sample_id,
            mode=WORKER_CONTRACT_STRUCTURED,
        )
    after = tuple(
        (step.task_id, step.prompt, step.depends_on, id(step)) for step in definition.workers
    )
    assert before == after


def test_contracts_do_not_cross_workers() -> None:
    parser = build_contract(
        next(item for item in definition_by_id("e1-02-report").workers if item.task_id == "parser"),
        steps=definition_by_id("e1-02-report").workers,
        sample_id="e1-02-report",
    )
    stats = build_contract(
        next(item for item in definition_by_id("e1-02-report").workers if item.task_id == "stats"),
        steps=definition_by_id("e1-02-report").workers,
        sample_id="e1-02-report",
    )
    assert parser.owned_files == ("report/parser.py",)
    assert stats.owned_files == ("report/stats.py",)
    assert "report/stats.py" not in parser.render()
    assert "report/parser.py" not in stats.render()


def test_mode_switch_only_appends() -> None:
    for definition in DEFINITIONS:
        for step in definition.workers:
            legacy = worker_task_text(
                step,
                steps=definition.workers,
                sample_id=definition.sample_id,
                mode=WORKER_CONTRACT_LEGACY,
            )
            structured = worker_task_text(
                step,
                steps=definition.workers,
                sample_id=definition.sample_id,
                mode=WORKER_CONTRACT_STRUCTURED,
            )
            assert structured.startswith(legacy)
            assert contract_overhead(step.prompt, structured) == len(structured) - len(legacy)


def test_contract_is_deterministic() -> None:
    first = _text("e1-12-conflict", "merge", WORKER_CONTRACT_STRUCTURED)
    second = _text("e1-12-conflict", "merge", WORKER_CONTRACT_STRUCTURED)
    assert first == second


def test_unknown_mode_is_rejected() -> None:
    step = WorkerStep("solo", "generic-solo", "Do something useful.")
    with pytest.raises(ValueError):
        worker_task_text(step, steps=(step,), sample_id="generic", mode="verbose")


def test_read_then_write_owns_only_the_write_target() -> None:
    owned, excluded = classify_paths(
        "Read formatter.py then implement exporter.py using format_row."
    )
    assert owned == ("exporter.py",)
    assert excluded == ()


def test_negated_sentence_marks_an_exclusion() -> None:
    owned, excluded = classify_paths(
        'Write /workspace/project/state.json with exactly {"a": 1}. Do not create final.json.'
    )
    assert owned == ("state.json",)
    assert excluded == ("final.json",)


def test_single_letter_siblings_are_not_inferred() -> None:
    definition = definition_by_id("e1-12-conflict")
    worker_b = next(item for item in definition.workers if item.task_id == "b")
    contract = build_contract(worker_b, steps=definition.workers, sample_id=definition.sample_id)
    assert contract.referenced_siblings == ()
