"""Atlas tool exposure policy.

One AnyCode ``ToolRegistry`` owns executable tools. ``ToolCatalog`` owns only discoverability:
which tools are always native-visible (core) and which are searchable (hidden by default).
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from anycode.tools.registry import ToolRegistry

CORE_TOOL_SEARCH = "tool.search"
CORE_TOOL_DISPATCH = "dispatch_action"

# AnyCode 0.9 built-ins plus the two Atlas control tools. The set is fixed for M1.
DEFAULT_CORE_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "bash",
        "file_read",
        "file_write",
        "file_edit",
        "grep",
        "list_files",
        CORE_TOOL_SEARCH,
        CORE_TOOL_DISPATCH,
    }
)


class ToolCatalogError(ValueError):
    """Raised when the catalog and the registry disagree."""


@dataclass(frozen=True)
class ToolCatalog:
    """Exposure-only view over the one ToolRegistry."""

    core_tool_names: frozenset[str] = DEFAULT_CORE_TOOL_NAMES
    searchable_tool_names: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        overlap = self.core_tool_names & self.searchable_tool_names
        if overlap:
            raise ToolCatalogError(f"tools cannot be both core and searchable: {sorted(overlap)}")
        if not self.core_tool_names:
            raise ToolCatalogError("at least one core tool is required")

    @property
    def native_tool_names(self) -> tuple[str, ...]:
        """Deterministic native surface for the whole run."""

        return tuple(sorted(self.core_tool_names))

    @property
    def searchable_names(self) -> tuple[str, ...]:
        return tuple(sorted(self.searchable_tool_names))

    def with_searchable(self, names: frozenset[str]) -> ToolCatalog:
        return replace(
            self,
            searchable_tool_names=self.searchable_tool_names | names,
        )

    def validate(self, registry: ToolRegistry) -> None:
        """Fail fast when the catalog names tools the registry cannot execute."""

        missing = [
            name
            for name in (*self.native_tool_names, *self.searchable_names)
            if not registry.has(name)
        ]
        if missing:
            raise ToolCatalogError(f"catalog references unregistered tools: {sorted(missing)}")
