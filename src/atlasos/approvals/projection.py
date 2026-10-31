"""Trusted, redacted review projection for M6 approval challenges.

Reviewers must see a trusted rendering built from the ActionRequest and canonical input, not
model prose. The projection deliberately carries coarse safe fields and digests; exact/sensitive
payload remains in protected Runtime suspension state.
"""

from __future__ import annotations

from typing import Any

from atlasos.actions.binding import canonical_digest
from atlasos.actions.models import ActionContext


def trusted_review_projection(
    *,
    tool_name: str,
    action: str,
    resource: str,
    canonical_arguments: dict[str, Any],
    action_context: ActionContext,
    action_digest: str,
    decision_basis_digest: str,
) -> dict[str, Any]:
    projection: dict[str, Any] = {
        "tool_name": tool_name,
        "action": action,
        "resource": resource,
        "principal": action_context.worker_profile or action_context.parent_agent,
        "classification": action_context.classification,
        "worker_id": action_context.worker_id,
        "profile_id": action_context.profile_id,
        "action_digest": action_digest,
        "decision_basis_digest": decision_basis_digest,
    }

    # Only explicit, coarse identity fields are copied. Bodies, file contents, shell strings,
    # prompts, and arbitrary objects are represented by digests instead.
    for key in ("path", "to", "repo", "issue", "issue_number", "database", "db", "table"):
        value = canonical_arguments.get(key)
        if value is not None and isinstance(value, (str, int, float, bool)):
            projection[key] = value
    for key in ("amount", "cwd", "url", "query", "service", "release"):
        value = canonical_arguments.get(key)
        if value is not None and isinstance(value, (str, int, float, bool)):
            projection[key] = value

    for sensitive in ("body", "content", "command", "subject", "text", "token", "secret"):
        if sensitive in canonical_arguments:
            projection[f"{sensitive}_digest"] = canonical_digest(canonical_arguments[sensitive])

    projection["canonical_input_digest"] = canonical_digest(canonical_arguments)
    return projection
