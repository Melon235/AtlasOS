"""Shared task-prompt construction: project dependency outputs, select, render.

This module is the single place where a task's model-visible prompt is assembled from the Atlas
Context Plane. It owns no scheduling, no provider call and no storage beyond the existing
``ContextStore``: it projects completed task results into ``task-output:<id>`` blocks, asks the
configured selector what the task logically needs, applies the configured transform, and renders
with the configured renderer.

It is shared by the workflow facade (``runtime.AtlasRuntime._build_atlas_prompt``) and by the coding
compositions so that both paths use one implementation instead of two.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from anycode.tasks.queue import TaskQueue

from atlasos.context.fingerprints import context_layout_hash
from atlasos.context.models import ContextBlock, ContextKind, ContextRef
from atlasos.context.renderer import PromptRenderer, StablePromptRenderer, stable_block_key
from atlasos.context.selectors import ContextSelector
from atlasos.context.store import InMemoryContextStore
from atlasos.context.transform import ContextTransform
from atlasos.workflow import AtlasTaskSpec


@dataclass(frozen=True)
class TaskPromptRender:
    """Observation of one rendered task prompt. Never an execution semantic."""

    prompt: str
    context_layout_hash: str
    stable_prefix_hash: str
    full_prompt_hash: str
    rendered_dependency_order: tuple[str, ...]
    dependency_completion_order: tuple[str, ...]
    pre_transform_layout_hash: str
    post_transform_layout_hash: str
    transform_name: str
    deduplicated: bool
    transform_latency_ms: float | None = None
    original_block_count: int | None = None
    optimized_block_count: int | None = None
    original_context_chars: int | None = None
    optimized_context_chars: int | None = None
    stable_prefix_tokens: int | None = None
    context_block_tokens: int | None = None
    dynamic_tail_tokens: int | None = None


class AtlasTaskPromptBuilder:
    """Callable ``(Task, TaskQueue) -> str`` for ``AtlasContextEngine(prompt_builder=...)``.

    The builder is stateful only in one respect: it remembers the last render per Atlas task id so
    the composition can record layout hashes and token splits as evidence.
    """

    def __init__(
        self,
        *,
        store: InMemoryContextStore,
        selector: ContextSelector,
        renderer: PromptRenderer,
        transform: ContextTransform,
        specs_by_title: Mapping[str, AtlasTaskSpec],
        agents_by_name: Mapping[str, tuple[str, str]],
        recovered_task_ids: frozenset[str] = frozenset(),
    ) -> None:
        self._store = store
        self._selector = selector
        self._renderer = renderer
        self._transform = transform
        self._specs_by_title = dict(specs_by_title)
        self._agents_by_name = dict(agents_by_name)
        self._recovered_task_ids = recovered_task_ids
        self._last: dict[str, TaskPromptRender] = {}

    # -- observation ---------------------------------------------------------------

    def last(self, atlas_task_id: str) -> TaskPromptRender | None:
        return self._last.get(atlas_task_id)

    def renders(self) -> dict[str, dict[str, Any]]:
        """Every render recorded so far, keyed by Atlas task id (evidence, not semantics)."""

        return {
            task_id: {
                "context_layout_hash": render.context_layout_hash,
                "stable_prefix_hash": render.stable_prefix_hash,
                "full_prompt_hash": render.full_prompt_hash,
                "rendered_dependency_order": list(render.rendered_dependency_order),
                "transform_name": render.transform_name,
                "deduplicated": render.deduplicated,
                "stable_prefix_tokens": render.stable_prefix_tokens,
                "context_block_tokens": render.context_block_tokens,
                "prompt_chars": len(render.prompt),
            }
            for task_id, render in self._last.items()
        }

    def forget(self) -> None:
        self._last.clear()

    # -- prompt construction -------------------------------------------------------

    def __call__(self, task: Any, queue: TaskQueue) -> str:
        spec = self._specs_by_title.get(str(task.title))
        if spec is None:
            raise RuntimeError(
                f"no Atlas task spec registered for compiled title {task.title!r}; "
                "the prompt builder must be constructed with specs_by_title"
            )
        agent_id = str(task.assignee or spec.assignee)
        self._assert_recovered_dependencies(spec)
        refs, completion_order = self._project(spec, queue)

        selected = self._selector.select(
            task=spec,
            agent_id=agent_id,
            store=self._store,
            available_refs=refs,
        )

        # Deterministic layout before the transform: the Stage 2 stable contract.
        if isinstance(self._renderer, StablePromptRenderer):
            pre_ordered = sorted(selected, key=stable_block_key)
        else:
            pre_ordered = list(selected)
        pre_refs = tuple(
            ContextRef(block_id=block.block_id, version=block.version) for block in pre_ordered
        )
        pre_hash = context_layout_hash(pre_refs)

        transformed = self._transform.transform(blocks=pre_ordered, query=spec.description)
        system_prompt, role = self._agents_by_name.get(agent_id, ("", ""))
        rendered = self._renderer.render(
            stable_system_prefix=system_prompt,
            agent_role=role,
            task=spec,
            context_blocks=transformed.blocks,
        )

        rendered_order = tuple(
            ref.block_id.removeprefix("task-output:")
            for ref in rendered.context_layout
            if ref.block_id.startswith("task-output:")
        )
        prompt = rendered.messages[-1]["content"]
        self._last[spec.task_id] = TaskPromptRender(
            prompt=prompt,
            context_layout_hash=rendered.context_layout_hash,
            stable_prefix_hash=rendered.stable_prefix_hash,
            full_prompt_hash=rendered.full_prompt_hash,
            rendered_dependency_order=rendered_order,
            dependency_completion_order=completion_order,
            pre_transform_layout_hash=pre_hash,
            post_transform_layout_hash=rendered.context_layout_hash,
            transform_name=transformed.transform_name,
            deduplicated=transformed.deduplicated,
            transform_latency_ms=transformed.transform_latency_ms,
            original_block_count=transformed.original_block_count,
            optimized_block_count=transformed.optimized_block_count,
            original_context_chars=transformed.original_context_chars,
            optimized_context_chars=transformed.optimized_context_chars,
            stable_prefix_tokens=rendered.stable_prefix_tokens,
            context_block_tokens=rendered.context_block_tokens,
            dynamic_tail_tokens=rendered.dynamic_tail_tokens,
        )
        return prompt

    # -- projection ----------------------------------------------------------------

    def _project(
        self,
        spec: AtlasTaskSpec,
        queue: TaskQueue,
    ) -> tuple[list[ContextRef], tuple[str, ...]]:
        """Write every completed task output into the store, in completion order."""

        completed = [
            candidate
            for candidate in queue.list()
            if candidate.status == "completed" and candidate.result is not None
        ]
        completed.sort(key=lambda candidate: (candidate.updated_at, candidate.title))

        refs: list[ContextRef] = []
        completion_order: list[str] = []
        dependencies = set(spec.depends_on)

        # Recovery invariant: durable dependency outputs must already be in the rebuilt store.
        # This check runs before any live projection, so a resume that begins without rebuilding
        # fails here instead of silently re-deriving recovered context from the restored queue.
        if self._recovered_task_ids:
            for task_id in sorted(self._recovered_task_ids):
                recovered_block = self._store.get(f"task-output:{task_id}")
                refs.append(
                    ContextRef(
                        block_id=recovered_block.block_id,
                        version=recovered_block.version,
                    )
                )
                if task_id in dependencies:
                    completion_order.append(task_id)

        for done in completed:
            done_spec = self._specs_by_title.get(str(done.title))
            if done_spec is None:
                continue
            if done_spec.task_id in self._recovered_task_ids:
                # Recovered context comes from the checkpoint rebuild, never from re-projecting
                # the restored queue, so resuming cannot create a duplicate logical block.
                continue
            block = ContextBlock(
                block_id=f"task-output:{done_spec.task_id}",
                kind=ContextKind.DEPENDENCY_OUTPUT,
                content=done.result or "",
                version=1,
                source=f"anycode_task:{done_spec.task_id}",
                task_id=done_spec.task_id,
                owner_agent_id=done.assignee,
            )
            self._store.put(block)
            refs.append(ContextRef(block_id=block.block_id, version=block.version))
            if done_spec.task_id in dependencies:
                completion_order.append(done_spec.task_id)

        return refs, tuple(completion_order)

    def _assert_recovered_dependencies(self, spec: AtlasTaskSpec) -> None:
        """A resumed run must have its durable dependency blocks before it renders."""

        if not self._recovered_task_ids:
            return
        missing = [
            task_id
            for task_id in sorted(set(spec.depends_on) & self._recovered_task_ids)
            if not self._store.contains(f"task-output:{task_id}")
        ]
        if missing:
            raise RuntimeError(
                "resume context invariant violated: durable dependency output for "
                f"{missing} was not rebuilt into the ContextStore before task "
                f"{spec.task_id!r} was executed"
            )
