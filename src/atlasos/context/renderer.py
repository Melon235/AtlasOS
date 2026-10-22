"""Prompt renderers: HOW selected context becomes a stable, ordered prompt.

Two Stage 2 renderers share one layout contract:

- ``NaiveExecutionOrderRenderer`` preserves the incoming block order, which is projection /
  completion order. It exists to expose layout instability under parallel execution.
- ``StablePromptRenderer`` ignores arrival order and sorts by kind, stable task id, version, then
  block id, so equivalent logical context always renders identically.

Rendering is deterministic and free of timestamps, run ids, and metrics.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from anycode.types import LLMMessage, TextBlock

from atlasos.context.fingerprints import context_layout_hash
from atlasos.context.models import ContextBlock, ContextKind, ContextRef
from atlasos.metrics.fingerprint import full_prompt_hash, stable_prefix_hash
from atlasos.workflow import AtlasTaskSpec


@dataclass(frozen=True)
class RenderedPrompt:
    messages: list[dict[str, str]]
    stable_prefix_text: str
    context_layout: tuple[ContextRef, ...]
    stable_prefix_hash: str
    context_layout_hash: str
    full_prompt_hash: str
    stable_prefix_tokens: int | None = None
    context_block_tokens: int | None = None
    dynamic_tail_tokens: int | None = None


class PromptRenderer(Protocol):
    def render(
        self,
        *,
        stable_system_prefix: str,
        agent_role: str,
        task: AtlasTaskSpec,
        context_blocks: Sequence[ContextBlock],
        dynamic_tail: str | None = None,
    ) -> RenderedPrompt: ...


_KIND_PRIORITY: dict[ContextKind, int] = {
    ContextKind.SYSTEM: 0,
    ContextKind.TASK: 1,
    ContextKind.DEPENDENCY_OUTPUT: 2,
    ContextKind.MESSAGE: 3,
    ContextKind.TOOL_RESULT: 4,
    ContextKind.ARTIFACT: 5,
    ContextKind.MEMORY: 6,
    ContextKind.SUMMARY: 7,
}


def stable_block_key(block: ContextBlock) -> tuple[int, str, int, str]:
    """Total order independent of completion timing, insertion order, or random ids."""

    return (
        _KIND_PRIORITY[block.kind],
        block.task_id or "",
        block.version,
        block.block_id,
    )


class _RendererBase:
    def _order(self, blocks: Sequence[ContextBlock]) -> list[ContextBlock]:
        raise NotImplementedError

    def render(
        self,
        *,
        stable_system_prefix: str,
        agent_role: str,
        task: AtlasTaskSpec,
        context_blocks: Sequence[ContextBlock],
        dynamic_tail: str | None = None,
    ) -> RenderedPrompt:
        ordered = self._order(context_blocks)
        body = _render_body(task, ordered, dynamic_tail)

        system_text = stable_system_prefix
        if agent_role and f"Role: {agent_role}" not in system_text:
            system_text = f"{system_text}\n\nRole: {agent_role}"

        refs = tuple(
            ContextRef(block_id=block.block_id, version=block.version) for block in ordered
        )
        messages = [
            {"role": "system", "content": system_text},
            {"role": "user", "content": body},
        ]
        return RenderedPrompt(
            messages=messages,
            stable_prefix_text=system_text,
            context_layout=refs,
            stable_prefix_hash=stable_prefix_hash(system_text),
            context_layout_hash=context_layout_hash(refs),
            full_prompt_hash=full_prompt_hash(
                system_text,
                [LLMMessage(role="user", content=[TextBlock(text=body)])],
            ),
        )


class NaiveExecutionOrderRenderer(_RendererBase):
    """Preserve incoming order; deliberately unstable under parallel completion."""

    def _order(self, blocks: Sequence[ContextBlock]) -> list[ContextBlock]:
        return list(blocks)


class StablePromptRenderer(_RendererBase):
    """Deterministic order by kind, stable task id, version, and block id.

    With ``preserve_input_order=True`` the caller has already applied the stable order and an
    optimizer (ContextPilot) may have reordered blocks deliberately; the renderer then preserves
    that explicit order instead of re-sorting it back.
    """

    def __init__(self, *, preserve_input_order: bool = False) -> None:
        self._preserve_input_order = preserve_input_order

    def _order(self, blocks: Sequence[ContextBlock]) -> list[ContextBlock]:
        if self._preserve_input_order:
            return list(blocks)
        return sorted(blocks, key=stable_block_key)


def _render_body(
    task: AtlasTaskSpec,
    blocks: Sequence[ContextBlock],
    dynamic_tail: str | None,
) -> str:
    parts: list[str] = [f"Task: {task.title}"]
    for block in blocks:
        if block.kind is ContextKind.TASK:
            parts.append(block.content)
        elif block.kind is ContextKind.DEPENDENCY_OUTPUT:
            task_id = block.task_id or "unknown"
            parts.append(
                f'<dependency task_id="{task_id}" version="{block.version}">\n'
                f"{block.content}\n"
                "</dependency>"
            )
    if dynamic_tail:
        parts.append(dynamic_tail)
    return "\n\n".join(parts)
