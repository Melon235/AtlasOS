from __future__ import annotations

from atlasos.context import (
    ContextBlock,
    ContextKind,
    ContextRef,
    NaiveExecutionOrderRenderer,
    StablePromptRenderer,
)
from atlasos.workflow import AtlasTaskSpec


def _task() -> AtlasTaskSpec:
    return AtlasTaskSpec(
        task_id="review",
        title="Review",
        description="compare outputs",
        assignee="reviewer",
        depends_on=("a", "b"),
    )


def _blocks(order: tuple[str, ...], contents: dict[str, str] | None = None) -> list[ContextBlock]:
    contents = contents or {}
    task_block = ContextBlock(
        block_id="task:review",
        kind=ContextKind.TASK,
        content="compare outputs",
        task_id="review",
    )
    dependencies = [
        ContextBlock(
            block_id=f"task-output:{task_id}",
            kind=ContextKind.DEPENDENCY_OUTPUT,
            content=contents.get(task_id, f"out-{task_id}"),
            task_id=task_id,
        )
        for task_id in order
    ]
    return [task_block, *dependencies]


def _dependency_order(renderer: object, order: tuple[str, ...]) -> list[str]:
    rendered = renderer.render(  # type: ignore[attr-defined]
        stable_system_prefix="sys",
        agent_role="",
        task=_task(),
        context_blocks=_blocks(order),
    )
    return [
        ref.block_id.removeprefix("task-output:")
        for ref in rendered.context_layout
        if ref.block_id.startswith("task-output:")
    ]


def test_naive_renderer_preserves_incoming_order() -> None:
    assert _dependency_order(NaiveExecutionOrderRenderer(), ("b", "a")) == ["b", "a"]
    assert _dependency_order(NaiveExecutionOrderRenderer(), ("a", "b")) == ["a", "b"]


def test_stable_renderer_sorts_by_stable_task_id() -> None:
    assert _dependency_order(StablePromptRenderer(), ("b", "a")) == ["a", "b"]
    assert _dependency_order(StablePromptRenderer(), ("a", "b")) == ["a", "b"]


def test_naive_layout_hash_changes_when_arrival_order_changes() -> None:
    first = NaiveExecutionOrderRenderer().render(
        stable_system_prefix="sys", agent_role="", task=_task(), context_blocks=_blocks(("a", "b"))
    )
    second = NaiveExecutionOrderRenderer().render(
        stable_system_prefix="sys", agent_role="", task=_task(), context_blocks=_blocks(("b", "a"))
    )

    assert first.context_layout_hash != second.context_layout_hash
    assert first.full_prompt_hash != second.full_prompt_hash


def test_stable_layout_hash_is_identical_under_inversion() -> None:
    first = StablePromptRenderer().render(
        stable_system_prefix="sys", agent_role="", task=_task(), context_blocks=_blocks(("a", "b"))
    )
    second = StablePromptRenderer().render(
        stable_system_prefix="sys", agent_role="", task=_task(), context_blocks=_blocks(("b", "a"))
    )

    assert first.context_layout_hash == second.context_layout_hash
    assert first.full_prompt_hash == second.full_prompt_hash
    assert first.stable_prefix_hash == second.stable_prefix_hash


def test_changed_dependency_content_changes_full_prompt_not_layout() -> None:
    renderer = StablePromptRenderer()
    first = renderer.render(
        stable_system_prefix="sys", agent_role="", task=_task(), context_blocks=_blocks(("a", "b"))
    )
    second = renderer.render(
        stable_system_prefix="sys",
        agent_role="",
        task=_task(),
        context_blocks=_blocks(("a", "b"), contents={"a": "changed"}),
    )

    assert first.context_layout_hash == second.context_layout_hash
    assert first.full_prompt_hash != second.full_prompt_hash


def test_rendered_body_uses_deterministic_delimiters() -> None:
    rendered = StablePromptRenderer().render(
        stable_system_prefix="sys",
        agent_role="role",
        task=_task(),
        context_blocks=_blocks(("b", "a")),
    )
    body = rendered.messages[-1]["content"]

    assert body.startswith("Task: Review")
    assert '<dependency task_id="a" version="1">' in body
    assert '<dependency task_id="b" version="1">' in body
    assert body.index("out-a") < body.index("out-b")
    assert "timestamp" not in body.lower()
    assert "Role: role" in rendered.messages[0]["content"]


def test_layout_refs_carry_versions() -> None:
    rendered = StablePromptRenderer().render(
        stable_system_prefix="sys", agent_role="", task=_task(), context_blocks=_blocks(("a", "b"))
    )

    assert rendered.context_layout[0] == ContextRef(block_id="task:review", version=1)
    assert all(isinstance(ref, ContextRef) for ref in rendered.context_layout)
