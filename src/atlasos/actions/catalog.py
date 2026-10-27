"""Trusted tool-name to semantic action/resource catalog.

The catalog never owns, registers, or executes tools. Unknown tools are explicitly unknown; no
name-based guessing is performed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ActionMapping:
    tool_name: str
    action: str
    resource_kind: str = "tool"
    expected_scopes: frozenset[str] = frozenset()
    hard_path: bool = False


@dataclass(frozen=True)
class ActionResolution:
    action: str
    resource: str
    expected_scopes: frozenset[str]
    hard_path: bool = False


_READ_SCOPES = frozenset({"research", "coding", "data", "diagnostics", "general"})
_CODE_SCOPES = frozenset({"coding", "diagnostics", "general"})
_DATA_SCOPES = frozenset({"data", "research"})
_COMM_SCOPES = frozenset({"communication"})

DEFAULT_MAPPINGS: tuple[ActionMapping, ...] = (
    ActionMapping("tool.search", "control.search"),
    ActionMapping("tool_search", "control.search"),
    ActionMapping("dispatch_action", "control.dispatch"),
    ActionMapping("file_read", "filesystem.read", "filesystem", _READ_SCOPES, True),
    ActionMapping("grep", "filesystem.read", "filesystem", _READ_SCOPES, True),
    ActionMapping("list_files", "filesystem.read", "filesystem", _READ_SCOPES, True),
    ActionMapping("file_write", "filesystem.write", "filesystem", _CODE_SCOPES, True),
    ActionMapping("file_edit", "filesystem.write", "filesystem", _CODE_SCOPES, True),
    ActionMapping("bash", "shell.execute", "shell", _CODE_SCOPES),
    ActionMapping("web.search", "web.search", "web", frozenset({"research", "diagnostics"})),
    ActionMapping("email.send", "communication.send", "email", _COMM_SCOPES),
    ActionMapping(
        "github.issue.get", "github.issue.read", "github", frozenset({"research", "coding"})
    ),
    ActionMapping("github.issue.create", "github.issue.create", "github", frozenset({"coding"})),
    ActionMapping("database.query", "database.read", "database", _DATA_SCOPES),
    ActionMapping("database.execute", "database.write", "database", frozenset({"data"})),
)


def _first(arguments: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = arguments.get(key)
        if value is not None and str(value).strip():
            return str(value)
    return "<unknown>"


def _resolve_resource(kind: str, tool_name: str, arguments: dict[str, Any]) -> str:
    if kind == "filesystem":
        return f"filesystem:{_first(arguments, 'path', 'cwd')}"
    if kind == "shell":
        return "shell:execute"
    if kind == "web":
        return f"web:{_first(arguments, 'query', 'url')}"
    if kind == "email":
        return f"email:{_first(arguments, 'to')}"
    if kind == "github":
        repo = _first(arguments, "repo", "repository")
        issue = _first(arguments, "issue", "issue_number")
        return f"github:{repo}:{issue}" if issue != "<unknown>" else f"github:{repo}"
    if kind == "github_issue":
        return f"github:repo:{_first(arguments, 'repo', 'repository')}"
    if kind == "logs":
        return f"logs:{_first(arguments, 'service')}:{_first(arguments, 'release')}"
    if kind == "docs":
        return f"docs:{_first(arguments, 'query')}"
    if kind == "code":
        return f"code:{_first(arguments, 'path')}"
    if kind == "tests":
        return f"tests:{_first(arguments, 'suite', 'command')}"
    if kind == "database":
        database = _first(arguments, "database", "db")
        table = _first(arguments, "table")
        return f"database:{database}/{table}" if table != "<unknown>" else f"database:{database}"
    return f"tool:{tool_name}"


class ActionCatalog:
    """Static trusted mappings; extra mappings may be supplied by the host."""

    def __init__(self, mappings: tuple[ActionMapping, ...] = DEFAULT_MAPPINGS) -> None:
        self._mappings = {mapping.tool_name: mapping for mapping in mappings}

    def mapping_for(self, tool_name: str) -> ActionMapping | None:
        return self._mappings.get(tool_name)

    def resolve(self, tool_name: str, arguments: dict[str, Any]) -> ActionResolution:
        mapping = self.mapping_for(tool_name)
        if mapping is None:
            return ActionResolution(
                action="unknown",
                resource=f"tool:{tool_name}",
                expected_scopes=frozenset(),
                hard_path=False,
            )
        return ActionResolution(
            action=mapping.action,
            resource=_resolve_resource(mapping.resource_kind, tool_name, arguments),
            expected_scopes=mapping.expected_scopes,
            hard_path=mapping.hard_path,
        )
