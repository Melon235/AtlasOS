"""Stable tool-binding identity for durable authorization (M6).

The resume path must not accept a tool merely because its name is unchanged. The binding binds
the logical tool id, its trusted source, its provider-facing name, its input schema, and the
declarative execution identity. This is a digest over *trusted registry metadata*; it never reads
model input.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from anycode.types import ToolDefinition

_ATLAS_CONTROL_TOOLS = frozenset({"tool.search", "tool_search", "dispatch_action"})
_ANYCODE_BUILTINS = frozenset(
    {"bash", "file_read", "file_write", "file_edit", "grep", "list_files"}
)


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def canonical_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def infer_tool_source(tool: ToolDefinition) -> str:
    """Best-effort trusted source identity when the host did not supply one.

    MCP tools carry their server name in the AnyCode description prefix and in the registered
    name. Core/Atlas tools are identified structurally. Unknown host tools use a stable
    ``host`` namespace; an explicit source map should be supplied when several hosts can expose
    the same logical id.
    """

    description = tool.description.strip()
    if description.startswith("[MCP:") and "]" in description:
        server = description[len("[MCP:") : description.index("]")].strip()
        return f"mcp:{server}"
    if tool.name in _ATLAS_CONTROL_TOOLS:
        return "atlas:control"
    if tool.name in _ANYCODE_BUILTINS:
        return "anycode:builtin"
    return "atlas:host"


def tool_schema(tool: ToolDefinition) -> dict[str, Any]:
    schema = tool.input_model.model_json_schema()
    schema.pop("title", None)
    for prop in schema.get("properties", {}).values():
        if isinstance(prop, dict):
            prop.pop("title", None)
    return schema


def execute_identity(tool: ToolDefinition) -> str:
    function = tool.execute
    module = getattr(function, "__module__", "unknown")
    qualname = getattr(function, "__qualname__", getattr(function, "__name__", "unknown"))
    return f"{module}.{qualname}"


def tool_binding(
    tool: ToolDefinition,
    *,
    source: str | None = None,
    provider_name: str | None = None,
) -> dict[str, Any]:
    """Return the canonical binding payload used for the digest and review projection."""

    schema = tool_schema(tool)
    return {
        "logical_tool_id": tool.name,
        "provider_tool_name": provider_name or tool.name,
        "source": source or infer_tool_source(tool),
        "description_digest": canonical_digest(tool.description),
        "input_schema": schema,
        "input_schema_digest": canonical_digest(schema),
        "side_effecting": bool(tool.side_effecting),
        "idempotency_key_field": tool.idempotency_key_field,
        "execute_identity": execute_identity(tool),
    }


def tool_binding_digest(
    tool: ToolDefinition,
    *,
    source: str | None = None,
    provider_name: str | None = None,
) -> str:
    return canonical_digest(tool_binding(tool, source=source, provider_name=provider_name))


def action_digest(
    *,
    principal: str,
    agent: str,
    action: str,
    resource: str,
    canonical_arguments: dict[str, Any],
    logical_tool_id: str,
) -> str:
    return canonical_digest(
        {
            "principal": principal,
            "agent": agent,
            "action": action,
            "resource": resource,
            "canonical_arguments": canonical_arguments,
            "logical_tool_id": logical_tool_id,
        }
    )


def decision_basis_digest(
    *,
    decision: str,
    matched_rule: str | None,
    policy_name: str | None,
    policy_digest: str,
    reason_codes: tuple[str, ...],
    scope_alignment: str,
    reviewer_verdict: str | None,
    external_policy_outcome: str | None,
    operating_scope: str | None,
    worker_profile: str | None,
    tenant: str | None,
    classification: str | None,
    tool_binding: str,
) -> str:
    return canonical_digest(
        {
            "decision": decision,
            "matched_rule": matched_rule,
            "policy_name": policy_name,
            "policy_digest": policy_digest,
            "reason_codes": list(reason_codes),
            "scope_alignment": scope_alignment,
            "reviewer_verdict": reviewer_verdict,
            "external_policy_outcome": external_policy_outcome,
            "operating_scope": operating_scope,
            "worker_profile": worker_profile,
            "tenant": tenant,
            "classification": classification,
            "tool_binding": tool_binding,
        }
    )


def input_digest(canonical_arguments: dict[str, Any]) -> str:
    return canonical_digest(canonical_arguments)
