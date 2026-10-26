"""Atlas tool runtime: one registry, exposure catalog, progressive disclosure, dispatch."""

from atlasos.tools.catalog import (
    CORE_TOOL_DISPATCH,
    CORE_TOOL_SEARCH,
    DEFAULT_CORE_TOOL_NAMES,
    ToolCatalog,
    ToolCatalogError,
)
from atlasos.tools.dispatch import AtlasDispatchExecutor, DispatchActionInput, ToolSearchInput
from atlasos.tools.registry import ProviderSafeToolRegistry, provider_tool_name
from atlasos.tools.runtime import (
    AtlasToolRuntime,
    ToolDispatchObservation,
    ToolRuntimeMetrics,
    ToolSearchObservation,
)
from atlasos.tools.search import (
    KeywordToolSearchIndex,
    MappingToolSearchIndex,
    SemanticToolSearchIndex,
    ToolSearchIndex,
    ToolSearchMatch,
    ToolSearchUnavailable,
    build_search_index,
)
from atlasos.tools.state import (
    RunToolState,
    RunToolStateStore,
    ToolStateError,
)

__all__ = [
    "CORE_TOOL_DISPATCH",
    "CORE_TOOL_SEARCH",
    "DEFAULT_CORE_TOOL_NAMES",
    "AtlasDispatchExecutor",
    "AtlasToolRuntime",
    "DispatchActionInput",
    "KeywordToolSearchIndex",
    "MappingToolSearchIndex",
    "ProviderSafeToolRegistry",
    "RunToolState",
    "RunToolStateStore",
    "SemanticToolSearchIndex",
    "ToolCatalog",
    "ToolCatalogError",
    "ToolDispatchObservation",
    "ToolRuntimeMetrics",
    "ToolSearchIndex",
    "ToolSearchInput",
    "ToolSearchMatch",
    "ToolSearchObservation",
    "ToolSearchUnavailable",
    "ToolStateError",
    "build_search_index",
    "provider_tool_name",
]
