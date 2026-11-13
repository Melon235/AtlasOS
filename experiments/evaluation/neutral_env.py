"""Neutral execution-environment capability map for the native AnyCode baseline (Final-3 R-10).

Final-2 asked for a thin adapter that runs unmodified AnyCode inside the same sandbox image as
AtlasRuntime. The *tool* half of that contract is a mapping problem, not a runtime problem: AnyCode
already owns its loop, scheduler and executor, so the only thing a neutral adapter may change is
*where* a tool runs.

This module records that mapping and can check it. It deliberately does **not** implement the
sandbox lifecycle: reusing Atlas's binding manager would place a mechanism under comparison inside
the baseline arm, and writing a second lifecycle implementation is out of this stage's scope.
Until that layer exists and is validated against a live sandbox + model, environment parity is
``BASELINE_PARITY_BLOCKED`` and no Track B cost or latency comparison is allowed.
"""

from __future__ import annotations

from dataclasses import dataclass

#: The tool names the native baseline is compiled with (``baseline.BASELINE_TOOLS``).
NATIVE_TOOLS: tuple[str, ...] = (
    "bash",
    "file_read",
    "file_write",
    "file_edit",
    "grep",
    "list_files",
)

#: Atlas's sandbox-backed implementations of the same logical operations.
ATLAS_SANDBOX_TOOLS: tuple[str, ...] = (
    "bash",
    "file_read",
    "file_write",
    "file_edit",
    "grep",
    "list_files",
)

#: Operations a software-engineering task needs, and where each side provides them.
CAPABILITY_MAP: tuple[tuple[str, str, str, str, str], ...] = (
    (
        "read a file",
        "file_read",
        "file_read",
        "equivalent",
        "same path fence inside /workspace/project",
    ),
    (
        "write a file",
        "file_write",
        "file_write",
        "equivalent",
        "Atlas counts operations; native does not",
    ),
    ("edit a file in place", "file_edit", "file_edit", "equivalent", "same replacement semantics"),
    ("list a directory", "list_files", "list_files", "equivalent", "same root restriction"),
    ("search content", "grep", "grep", "equivalent", "native regex engine, Atlas capped output"),
    (
        "run a command",
        "bash",
        "bash",
        "equivalent-with-limitation",
        "Atlas caps output and normalises exit codes",
    ),
    ("run tests", "bash", "bash", "equivalent", "task-level pytest through the same shell"),
    (
        "git operations",
        "bash",
        "bash",
        "equivalent-with-limitation",
        "no git tool on either side; bash only",
    ),
)

#: Environment facts that must match before a cost/latency claim is allowed.
ENVIRONMENT_REQUIREMENTS: tuple[str, ...] = (
    "sandbox provider and version",
    "image reference and digest",
    "network policy (none)",
    "workspace seed digest",
    "CPU / memory limits as accepted by the provider",
    "tool availability and timeout",
    "oracle mode and digest",
)


@dataclass(frozen=True)
class ParityStatus:
    status: str
    reason: str
    missing: tuple[str, ...]


def capability_report() -> ParityStatus:
    """Return the current state of environment parity, without pretending it is done."""

    native = set(NATIVE_TOOLS)
    atlas = set(ATLAS_SANDBOX_TOOLS)
    missing = tuple(sorted(native ^ atlas))
    if missing:
        return ParityStatus(
            status="CAPABILITY_MISMATCH",
            reason="the two arms expose different tool names for the same task",
            missing=missing,
        )
    return ParityStatus(
        status="BASELINE_PARITY_BLOCKED",
        reason=(
            "tool capability maps 1:1, but the native arm still runs on the host: no neutral "
            "sandbox lifecycle layer exists, so execution location, shell and setup cost differ"
        ),
        missing=(
            "neutral sandbox bind/exec/export layer",
            "live native-in-sandbox acceptance run",
        ),
    )


def uncovered_operations() -> tuple[str, ...]:
    """Operations required by SWE tasks that neither arm exposes as a dedicated tool."""

    mapped = {row[1] for row in CAPABILITY_MAP}
    return tuple(op for op in ("git operations",) if op not in mapped)
