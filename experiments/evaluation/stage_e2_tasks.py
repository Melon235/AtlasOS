"""Controlled multi-stage Context Plane workloads for Stage E2 Track A.

These workflows run through the existing AtlasRuntime/AnyCode path with real DeepSeek. They do
not use the V2 sandbox or a second agent loop. Root tasks receive their document in the task
description; the merge task receives the real dependency outputs. Correctness is a machine-checked
exact token, not an LLM judge.
"""

from __future__ import annotations

from dataclasses import dataclass

from atlasos.context.requirements import ContextRequirement
from atlasos.workflow import AtlasAgentSpec, AtlasTaskSpec, AtlasWorkflow

SYSTEM = (
    "You are a precise document analyst. Use only the supplied document or dependency outputs. "
    "Return the requested marker exactly, with no explanation."
)


@dataclass(frozen=True)
class E2Definition:
    task_id: str
    profile: str
    doc_a: str
    doc_b: str
    marker_a: str
    marker_b: str
    expected: str


def _doc(name: str, marker: str, extra: str = "") -> str:
    return (
        f"DOCUMENT {name}\n"
        f"benchmark marker: {marker}\n"
        f"owner: atlas-e2-{name.lower()}\n"
        f"{extra}\n"
        "This document is part of a controlled multi-stage context evaluation. "
        "Only the benchmark marker is required for the next stage.\n"
    )


def _definitions() -> tuple[E2Definition, ...]:
    clean_a = _doc("A", "ALPHA-17", "The archive code is A-7.")
    clean_b = _doc("B", "BETA-42", "The archive code is B-9.")
    duplicate = _doc("D", "DUP-77", "Identical document supplied twice by two producers.")
    conflict_old = _doc(
        "OLD",
        "OLD-11",
        "This is version 1. It was superseded by version 2 and is not authoritative.",
    )
    conflict_new = _doc(
        "NEW",
        "NEW-22",
        "This is version 2 and is the authoritative replacement for version 1.",
    )
    long_extra = " ".join(
        f"Archive note {index:02d}: stable context blocks reduce repeated prompt work."
        for index in range(120)
    )
    long_a = _doc("LONGA", "LONG-A-5", long_extra)
    long_b = _doc("LONGB", "LONG-B-8", long_extra)
    return (
        E2Definition(
            "e2-01-clean",
            "clean",
            clean_a,
            clean_b,
            "ALPHA-17",
            "BETA-42",
            "RESULT=ALPHA-17|BETA-42",
        ),
        E2Definition(
            "e2-02-clean",
            "clean",
            _doc("C", "CLEAN-C-3"),
            _doc("D", "CLEAN-D-4"),
            "CLEAN-C-3",
            "CLEAN-D-4",
            "RESULT=CLEAN-C-3|CLEAN-D-4",
        ),
        E2Definition(
            "e2-03-duplicate",
            "duplicate_heavy",
            duplicate,
            duplicate,
            "DUP-77",
            "DUP-77",
            "RESULT=DUP-77|DUP-77",
        ),
        E2Definition(
            "e2-04-duplicate",
            "duplicate_heavy",
            _doc("D2", "DUP2-88"),
            _doc("D2", "DUP2-88"),
            "DUP2-88",
            "DUP2-88",
            "RESULT=DUP2-88|DUP2-88",
        ),
        E2Definition(
            "e2-05-conflict",
            "conflicting",
            conflict_old,
            conflict_new,
            "OLD-11",
            "NEW-22",
            "RESULT=NEW-22|NEW-22",
        ),
        E2Definition(
            "e2-06-conflict",
            "conflicting",
            _doc("V1", "V1-31", "Version 1 is deprecated."),
            _doc("V2", "V2-32", "Version 2 is authoritative."),
            "V1-31",
            "V2-32",
            "RESULT=V2-32|V2-32",
        ),
        E2Definition(
            "e2-07-long",
            "long_context",
            long_a,
            long_b,
            "LONG-A-5",
            "LONG-B-8",
            "RESULT=LONG-A-5|LONG-B-8",
        ),
        E2Definition(
            "e2-08-multihop",
            "multihop",
            _doc("MA", "MA-1", "The left key is LEFT-9."),
            _doc("MB", "MB-2", "The right key is RIGHT-6."),
            "MA-1",
            "MB-2",
            "RESULT=MA-1|MB-2",
        ),
    )


DEFINITIONS: tuple[E2Definition, ...] = _definitions()


def _root(task_id: str, title: str, document: str, marker_key: str, assignee: str) -> AtlasTaskSpec:
    return AtlasTaskSpec(
        task_id=task_id,
        title=title,
        description=(
            f"{document}\n\nReturn exactly `{marker_key}=<benchmark marker value>` and nothing "
            "else."
        ),
        assignee=assignee,
        required_capabilities=frozenset({"write"}),
    )


def build_workflow(definition: E2Definition) -> AtlasWorkflow:
    agents = (
        AtlasAgentSpec(
            name="writer-a",
            role="document analyst A",
            system_prompt=SYSTEM,
            capabilities=frozenset({"write"}),
        ),
        AtlasAgentSpec(
            name="writer-b",
            role="document analyst B",
            system_prompt=SYSTEM,
            capabilities=frozenset({"write"}),
        ),
        AtlasAgentSpec(
            name="merger",
            role="result integrator",
            system_prompt=SYSTEM,
            capabilities=frozenset({"merge"}),
        ),
    )
    merge_rule = (
        "The OLD document is deprecated. Use the NEW authoritative marker for both slots."
        if definition.profile == "conflicting"
        else "Use each dependency's marker in its own slot."
    )
    marker_key = "MARKER" if definition.profile == "duplicate_heavy" else "A_MARKER"
    marker_key_b = "MARKER" if definition.profile == "duplicate_heavy" else "B_MARKER"
    return AtlasWorkflow(
        name=f"e2-{definition.task_id}",
        agents=agents,
        tasks=(
            _root("a", f"{definition.task_id}-a", definition.doc_a, marker_key, "writer-a"),
            _root("b", f"{definition.task_id}-b", definition.doc_b, marker_key_b, "writer-b"),
            AtlasTaskSpec(
                task_id="merge",
                title=f"{definition.task_id}-merge",
                description=(
                    "Read the dependency outputs. " + merge_rule + " Return exactly "
                    "`RESULT=<marker_a>|<marker_b>` and nothing else."
                ),
                assignee="merger",
                depends_on=("a", "b"),
                required_capabilities=frozenset({"merge"}),
                required_context=(
                    ContextRequirement("dep:a", source_task_id="a"),
                    ContextRequirement("dep:b", source_task_id="b"),
                ),
            ),
        ),
    )


def build_placement_workflow(definition: E2Definition) -> AtlasWorkflow:
    """Five-node DAG used only for the P0/P1 placement factor."""

    agents = tuple(
        AtlasAgentSpec(
            name=f"writer-{index}",
            role=f"writer {index}",
            system_prompt=SYSTEM,
            capabilities=frozenset({"write"}),
        )
        for index in range(1, 4)
    ) + (
        AtlasAgentSpec(
            name="merger",
            role="result integrator",
            system_prompt=SYSTEM,
            capabilities=frozenset({"merge"}),
        ),
    )
    base = definition
    return AtlasWorkflow(
        name=f"e2p-{definition.task_id}",
        agents=agents,
        tasks=(
            _root("a1", f"{definition.task_id}-a1", base.doc_a, "A1", "writer-1"),
            _root("b1", f"{definition.task_id}-b1", base.doc_b, "B1", "writer-2"),
            AtlasTaskSpec(
                "a2",
                f"{definition.task_id}-a2",
                "Read the a1 dependency output and return exactly `A2_MARKER=<marker>`.",
                "writer-1",
                depends_on=("a1",),
                required_capabilities=frozenset({"write"}),
                required_context=(ContextRequirement("dep:a1", source_task_id="a1"),),
            ),
            AtlasTaskSpec(
                "b2",
                f"{definition.task_id}-b2",
                "Read the b1 dependency output and return exactly `B2_MARKER=<marker>`.",
                "writer-2",
                depends_on=("b1",),
                required_capabilities=frozenset({"write"}),
                required_context=(ContextRequirement("dep:b1", source_task_id="b1"),),
            ),
            AtlasTaskSpec(
                "merge",
                f"{definition.task_id}-merge",
                "Read the a2 and b2 outputs. Return exactly `RESULT=<a2_marker>|<b2_marker>`.",
                "merger",
                depends_on=("a2", "b2"),
                required_capabilities=frozenset({"merge"}),
                required_context=(
                    ContextRequirement("dep:a2", source_task_id="a2"),
                    ContextRequirement("dep:b2", source_task_id="b2"),
                ),
            ),
        ),
    )


def check_output(definition: E2Definition, output: str) -> bool:
    return definition.expected in output
