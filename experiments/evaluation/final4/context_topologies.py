"""Final-4: selector and renderer across five task topologies (deterministic, no model).

Reuses the production builder from ``atlasos.context.task_prompt`` — the same object the coding and
evaluation compositions pass to ``AtlasContextEngine`` — so this measures the shipped path, not a
parallel implementation.
"""

from __future__ import annotations

import statistics
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from atlasos.context import (
    InMemoryContextStore,
    NaiveExecutionOrderRenderer,
    StablePromptRenderer,
    TaskScopedSelector,
)
from atlasos.context.task_prompt import AtlasTaskPromptBuilder
from atlasos.context.transform import IdentityContextTransform
from atlasos.workflow import AtlasTaskSpec

MARKER = "NEEDLE-"


def _spec(task_id: str, depends_on: tuple[str, ...] = ()) -> AtlasTaskSpec:
    return AtlasTaskSpec(
        task_id=task_id,
        title=task_id.upper(),
        description=f"do {task_id} and include NEEDLE-{task_id}",
        assignee=f"w-{task_id}",
        depends_on=depends_on,
    )


def _done(spec: AtlasTaskSpec, minute: int) -> Any:
    return SimpleNamespace(
        title=spec.title,
        assignee=spec.assignee,
        status="completed",
        result=f"result of {spec.task_id}: NEEDLE-{spec.task_id}",
        updated_at=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=minute),
    )


def _builder(specs: list[AtlasTaskSpec], renderer: Any) -> AtlasTaskPromptBuilder:
    return AtlasTaskPromptBuilder(
        store=InMemoryContextStore(),
        selector=TaskScopedSelector(),
        renderer=renderer,
        transform=IdentityContextTransform(),
        specs_by_title={spec.title: spec for spec in specs},
        agents_by_name={spec.assignee: ("SYS", "r") for spec in specs},
    )


def _render(builder: AtlasTaskPromptBuilder, leaf: AtlasTaskSpec, completed: list[Any]) -> str:
    return builder(
        SimpleNamespace(title=leaf.title, assignee=leaf.assignee),
        SimpleNamespace(list=lambda: list(completed)),
    )


TOPOLOGIES: dict[str, dict[str, Any]] = {
    "chain4": {
        "specs": [_spec("a"), _spec("b", ("a",)), _spec("c", ("b",)), _spec("d", ("c",))],
        "leaf": "d",
        "completed": ["a", "b", "c"],
        "required": ["a", "b", "c"],
        "irrelevant": [],
    },
    "fork_join": {
        "specs": [_spec("r1"), _spec("r2"), _spec("join", ("r1", "r2"))],
        "leaf": "join",
        "completed": ["r1", "r2"],
        "required": ["r1", "r2"],
        "irrelevant": [],
    },
    "multi_dep": {
        "specs": [_spec("d1"), _spec("d2"), _spec("d3"), _spec("leaf", ("d1", "d2", "d3"))],
        "leaf": "leaf",
        "completed": ["d1", "d2", "d3"],
        "required": ["d1", "d2", "d3"],
        "irrelevant": [],
    },
    "no_dep": {
        "specs": [_spec("solo")],
        "leaf": "solo",
        "completed": [],
        "required": [],
        "irrelevant": [],
    },
    "with_irrelevant": {
        "specs": [_spec("dep"), _spec("other1"), _spec("other2"), _spec("leaf", ("dep",))],
        "leaf": "leaf",
        "completed": ["dep", "other1", "other2"],
        "required": ["dep"],
        "irrelevant": ["other1", "other2"],
    },
}


def _measure(name: str) -> dict[str, Any]:
    config = TOPOLOGIES[name]
    specs: list[AtlasTaskSpec] = config["specs"]
    by_id = {spec.task_id: spec for spec in specs}
    leaf = by_id[config["leaf"]]

    timings: list[float] = []
    builder = _builder(specs, StablePromptRenderer(preserve_input_order=True))
    completed = [
        _done(by_id[task_id], index + 1) for index, task_id in enumerate(config["completed"])
    ]
    for _ in range(5):
        started = time.perf_counter()
        prompt = _render(builder, leaf, completed)
        timings.append((time.perf_counter() - started) * 1000.0)

    required_present = sum(1 for task_id in config["required"] if f"NEEDLE-{task_id}" in prompt)
    irrelevant_injected = sum(
        1 for task_id in config["irrelevant"] if f"NEEDLE-{task_id}" in prompt
    )

    def _layout(renderer: Any, minutes: dict[str, int]) -> str:
        probe = _builder(specs, renderer)
        ordered = [_done(by_id[task_id], minute) for task_id, minute in minutes.items()]
        _render(probe, leaf, ordered)
        render = probe.last(config["leaf"])
        assert render is not None
        return render.context_layout_hash

    forward = {task_id: index + 1 for index, task_id in enumerate(config["completed"])}
    backward = {
        task_id: len(config["completed"]) - index
        for index, task_id in enumerate(config["completed"])
    }
    stable_same = _layout(StablePromptRenderer(), forward) == _layout(
        StablePromptRenderer(), backward
    )
    naive_same = _layout(NaiveExecutionOrderRenderer(), forward) == _layout(
        NaiveExecutionOrderRenderer(), backward
    )

    return {
        "topology": name,
        "leaf_prompt_chars": len(prompt),
        "required_dependencies": len(config["required"]),
        "required_present": required_present,
        "irrelevant_completed": len(config["irrelevant"]),
        "irrelevant_injected": irrelevant_injected,
        "layout_equality_stable": stable_same,
        "layout_equality_naive": naive_same,
        "selection_render_ms_median": round(statistics.median(timings), 3),
    }


def run() -> dict[str, Any]:
    cells = [_measure(name) for name in TOPOLOGIES]
    required = sum(cell["required_dependencies"] for cell in cells)
    present = sum(cell["required_present"] for cell in cells)
    return {
        "cells": cells,
        "dependency_coverage": round(present / required, 4) if required else None,
        "irrelevant_injected_total": sum(cell["irrelevant_injected"] for cell in cells),
        "stable_equality_cells": sum(1 for cell in cells if cell["layout_equality_stable"]),
        "naive_equality_cells": sum(1 for cell in cells if cell["layout_equality_naive"]),
        "cells_total": len(cells),
    }


if __name__ == "__main__":
    import json

    print(json.dumps(run(), indent=2, sort_keys=True))
