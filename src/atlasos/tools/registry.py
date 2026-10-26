"""ToolRegistry with provider-safe model-facing names.

Provider function names are restricted (DeepSeek/OpenAI: ``^[A-Za-z0-9_-]+$``). Atlas logical tool
ids may contain dots (``tool.search``, ``github.issue.get``). The registry stores the logical
definition once and only sanitizes the LLM-facing name; execution always resolves back to the
logical tool id.
"""

from __future__ import annotations

import hashlib
import re

from anycode.tools.registry import ToolRegistry
from anycode.types import LLMToolDef, ToolDefinition

_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")
_MAX_LENGTH = 64


def provider_tool_name(logical_name: str) -> str:
    safe = _UNSAFE.sub("_", logical_name)
    if len(safe) > _MAX_LENGTH:
        digest = hashlib.sha256(logical_name.encode("utf-8")).hexdigest()[:8]
        safe = f"{safe[: _MAX_LENGTH - 9]}_{digest}"
    return safe


class ProviderSafeToolRegistry(ToolRegistry):
    """Logical tool ids in the registry; sanitized names at the provider boundary."""

    def __init__(self) -> None:
        super().__init__()
        self._provider_to_logical: dict[str, str] = {}
        self._logical_to_provider: dict[str, str] = {}

    def register(self, tool: ToolDefinition) -> None:
        provider_name = provider_tool_name(tool.name)
        existing = self._provider_to_logical.get(provider_name)
        if existing is not None and existing != tool.name:
            raise ValueError(
                f"provider tool-name collision: {existing!r} and {tool.name!r} "
                f"both map to {provider_name!r}"
            )
        super().register(tool)
        self._provider_to_logical[provider_name] = tool.name
        self._logical_to_provider[tool.name] = provider_name

    def deregister(self, name: str) -> None:
        super().deregister(name)
        provider_name = self._logical_to_provider.pop(name, None)
        if provider_name is not None:
            self._provider_to_logical.pop(provider_name, None)

    def logical_name(self, provider_name: str) -> str | None:
        return self._provider_to_logical.get(provider_name)

    def provider_name(self, logical_name: str) -> str | None:
        return self._logical_to_provider.get(logical_name)

    def to_tool_defs(self) -> list[LLMToolDef]:
        return [
            definition.model_copy(
                update={"name": self._logical_to_provider.get(definition.name, definition.name)}
            )
            for definition in super().to_tool_defs()
        ]
