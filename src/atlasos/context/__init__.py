"""Stage 2 context runtime: explicit blocks, selection, and deterministic rendering."""

from atlasos.context.fingerprints import context_layout_hash
from atlasos.context.models import ContextBlock, ContextKind, ContextRef
from atlasos.context.renderer import (
    NaiveExecutionOrderRenderer,
    PromptRenderer,
    RenderedPrompt,
    StablePromptRenderer,
    stable_block_key,
)
from atlasos.context.selectors import ContextSelector, TaskScopedSelector
from atlasos.context.store import ContextStore, InMemoryContextStore

__all__ = [
    "ContextBlock",
    "ContextKind",
    "ContextRef",
    "ContextSelector",
    "ContextStore",
    "InMemoryContextStore",
    "NaiveExecutionOrderRenderer",
    "PromptRenderer",
    "RenderedPrompt",
    "StablePromptRenderer",
    "TaskScopedSelector",
    "context_layout_hash",
    "stable_block_key",
]
