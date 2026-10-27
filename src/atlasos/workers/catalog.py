"""Trusted WorkerCatalog. Profiles are immutable Python definitions."""

from __future__ import annotations

from collections.abc import Mapping

from atlasos.workers.models import WorkerConfigurationError, WorkerProfile

RESEARCH_INSTRUCTIONS = (
    "Investigate the assigned question using evidence. Prefer read-only discovery and "
    "cite concrete observations. Do not take external side effects without explicit approval."
)
CODING_INSTRUCTIONS = (
    "Implement and verify the assigned code change. Keep edits focused, preserve existing "
    "behavior outside the task, and report tests run. Do not perform external communication."
)
DATA_INSTRUCTIONS = (
    "Inspect and reason about data using read-oriented tools. Keep queries scoped to the task. "
    "Do not mutate durable data unless the task explicitly requires it."
)
GENERAL_INSTRUCTIONS = (
    "Complete the assigned task with the available basic tools. Ask for a more focused worker "
    "when the task requires a specialty; do not assume extra authority."
)

DEFAULT_PROFILES: tuple[WorkerProfile, ...] = (
    WorkerProfile(
        profile_id="research_worker",
        role="research and diagnosis worker",
        instructions=RESEARCH_INSTRUCTIONS,
        base_tools=("tool_search", "dispatch_action", "file_read", "grep", "list_files"),
        operating_scope=("research", "diagnostics"),
        max_turns=6,
        can_delegate=False,
    ),
    WorkerProfile(
        profile_id="coding_worker",
        role="coding and diagnostics worker",
        instructions=CODING_INSTRUCTIONS,
        base_tools=(
            "tool_search",
            "dispatch_action",
            "file_read",
            "file_write",
            "file_edit",
            "grep",
            "list_files",
            "bash",
        ),
        operating_scope=("coding",),
        max_turns=8,
        can_delegate=False,
    ),
    WorkerProfile(
        profile_id="data_worker",
        role="data inspection worker",
        instructions=DATA_INSTRUCTIONS,
        base_tools=("tool_search", "dispatch_action", "file_read", "grep", "list_files"),
        operating_scope=("data",),
        max_turns=6,
        can_delegate=False,
    ),
    WorkerProfile(
        profile_id="general_worker",
        role="general fallback worker",
        instructions=GENERAL_INSTRUCTIONS,
        base_tools=("tool_search", "dispatch_action", "file_read", "grep", "list_files"),
        operating_scope=("general",),
        max_turns=4,
        can_delegate=False,
    ),
)


class WorkerCatalog:
    def __init__(self, profiles: tuple[WorkerProfile, ...] = DEFAULT_PROFILES) -> None:
        self._profiles = {profile.profile_id: profile for profile in profiles}
        if len(self._profiles) != len(profiles):
            raise ValueError("duplicate worker profile_id")

    @property
    def profiles(self) -> Mapping[str, WorkerProfile]:
        return dict(self._profiles)

    def resolve(self, profile_id: str) -> WorkerProfile:
        try:
            return self._profiles[profile_id]
        except KeyError as error:
            raise WorkerConfigurationError(f"unknown worker profile {profile_id!r}") from error
