"""Final-3 R-4: the shared task-prompt builder renders dependency context deterministically.

These are unit-level assertions about the prompt the model receives, not about a renderer object in
isolation: the builder's return value is exactly what AnyCode sends as the task prompt.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest

from atlasos.context import (
    InMemoryContextStore,
    NaiveExecutionOrderRenderer,
    StablePromptRenderer,
    TaskScopedSelector,
)
from atlasos.context.task_prompt import AtlasTaskPromptBuilder
from atlasos.context.transform import IdentityContextTransform
from atlasos.workflow import AtlasTaskSpec


def _spec(
    task_id: str, *, depends_on: tuple[str, ...] = (), description: str = ""
) -> AtlasTaskSpec:
    return AtlasTaskSpec(
        task_id=task_id,
        title=task_id.upper(),
        description=description or f"do {task_id}",
        assignee=f"worker-{task_id}",
        depends_on=depends_on,
    )


def _done(spec: AtlasTaskSpec, result: str, *, minute: int) -> Any:
    return SimpleNamespace(
        title=spec.title,
        assignee=spec.assignee,
        status="completed",
        result=result,
        updated_at=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=minute),
    )


def _builder(
    specs: list[AtlasTaskSpec],
    *,
    renderer: Any | None = None,
    recovered: frozenset[str] = frozenset(),
    system_prompt: str = "SYSTEM-PROMPT",
) -> AtlasTaskPromptBuilder:
    return AtlasTaskPromptBuilder(
        store=InMemoryContextStore(),
        selector=TaskScopedSelector(),
        renderer=renderer or StablePromptRenderer(preserve_input_order=True),
        transform=IdentityContextTransform(),
        specs_by_title={spec.title: spec for spec in specs},
        agents_by_name={spec.assignee: (system_prompt, "role") for spec in specs},
        recovered_task_ids=recovered,
    )


def _call(builder: AtlasTaskPromptBuilder, spec: AtlasTaskSpec, completed: list[Any]) -> str:
    task = SimpleNamespace(title=spec.title, assignee=spec.assignee)
    queue = SimpleNamespace(list=lambda: list(completed))
    return builder(cast(Any, task), cast(Any, queue))


def _last(builder: AtlasTaskPromptBuilder, task_id: str) -> Any:
    render = builder.last(task_id)
    assert render is not None
    return render


def test_dependency_output_is_rendered_into_the_task_prompt() -> None:
    root, leaf = _spec("a"), _spec("b", depends_on=("a",))
    builder = _builder([root, leaf])

    prompt = _call(builder, leaf, [_done(root, "fact-from-a", minute=1)])

    assert prompt.startswith("Task: B")
    assert "do b" in prompt
    assert '<dependency task_id="a" version="1">' in prompt
    assert "fact-from-a" in prompt
    assert _last(builder, "b").rendered_dependency_order == ("a",)


def test_unrelated_completed_task_is_not_injected() -> None:
    root, unrelated, leaf = _spec("a"), _spec("c"), _spec("b", depends_on=("a",))
    builder = _builder([root, unrelated, leaf])

    prompt = _call(
        builder, leaf, [_done(root, "fact-a", minute=1), _done(unrelated, "nope", minute=2)]
    )

    assert "fact-a" in prompt
    assert "nope" not in prompt


def test_stable_layout_is_independent_of_completion_order() -> None:
    a, b = _spec("a"), _spec("b")
    leaf = _spec("c", depends_on=("a", "b"))

    first = _builder([a, b, leaf])
    second = _builder([a, b, leaf])
    prompt_first = _call(first, leaf, [_done(a, "A", minute=1), _done(b, "B", minute=2)])
    prompt_second = _call(second, leaf, [_done(b, "B", minute=1), _done(a, "A", minute=2)])

    assert prompt_first == prompt_second
    assert _last(first, "c").context_layout_hash == _last(second, "c").context_layout_hash
    assert _last(first, "c").full_prompt_hash == _last(second, "c").full_prompt_hash


def test_naive_renderer_is_order_sensitive() -> None:
    a, b = _spec("a"), _spec("b")
    leaf = _spec("c", depends_on=("a", "b"))

    first = _builder([a, b, leaf], renderer=NaiveExecutionOrderRenderer())
    second = _builder([a, b, leaf], renderer=NaiveExecutionOrderRenderer())
    _call(first, leaf, [_done(a, "A", minute=1), _done(b, "B", minute=2)])
    _call(second, leaf, [_done(b, "B", minute=1), _done(a, "A", minute=2)])

    assert _last(first, "c").context_layout_hash != _last(second, "c").context_layout_hash


def test_system_prompt_is_hashed_but_not_injected_into_the_task_body() -> None:
    root = _spec("a")
    builder = _builder([root], system_prompt="SECRET-SYSTEM-PROMPT")

    prompt = _call(builder, root, [])

    assert "SECRET-SYSTEM-PROMPT" not in prompt
    assert _last(builder, "a").stable_prefix_hash != ""


def test_resume_without_rebuilt_dependency_fails_loudly() -> None:
    root, leaf = _spec("a"), _spec("b", depends_on=("a",))
    builder = _builder([root, leaf], recovered=frozenset({"a"}))

    with pytest.raises(RuntimeError, match="resume context invariant violated"):
        _call(builder, leaf, [])


def test_x0_default_transform_is_identity() -> None:
    root = _spec("a")
    builder = _builder([root])

    _call(builder, root, [])

    assert _last(builder, "a").transform_name == "identity"
    assert _last(builder, "a").deduplicated is False
    assert set(builder.renders()) == {"a"}
