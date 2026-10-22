"""Placement models: profiles, logical agent context state, and decision traces."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from atlasos.context.models import ContextRef
from atlasos.context.requirements import ContextRequirement


@dataclass(frozen=True)
class AgentProfile:
    agent_id: str
    capabilities: frozenset[str] = frozenset()


@dataclass(frozen=True)
class TaskRequirements:
    required_capabilities: frozenset[str] = frozenset()
    required_context: tuple[ContextRequirement, ...] = ()


@dataclass
class PlacementContextState:
    """Per-run logical working set. Never provider KV state and never durable truth."""

    agent_id: str
    available_refs: set[ContextRef] = field(default_factory=set)
    consumed_task_ids: set[str] = field(default_factory=set)
    produced_task_ids: set[str] = field(default_factory=set)

    def has_requirement(self, requirement: ContextRequirement) -> bool:
        if requirement.required_version is not None:
            return any(
                ref.block_id == requirement.logical_key
                and ref.version == requirement.required_version
                for ref in self.available_refs
            )
        if requirement.source_task_id is not None:
            if requirement.source_task_id in self.produced_task_ids:
                return True
            if requirement.source_task_id in self.consumed_task_ids:
                return True
        prefix = f"{requirement.logical_key}@"
        return any(
            ref.block_id == requirement.logical_key or ref.block_id.startswith(prefix)
            for ref in self.available_refs
        )

    def add_ref(self, ref: ContextRef) -> None:
        self.available_refs.add(ref)


@dataclass(frozen=True)
class PlacementDecision:
    task_id: str
    agent_id: str

    capability_score: float
    context_locality: float
    load_score: float
    total_score: float
    policy_name: str

    candidate_agent_ids: tuple[str, ...]
    required_context_count: int
    matched_context_count: int
    logical_handoff_refs: int
    logical_handoff_chars: int | None = None
    candidate_scores: Mapping[str, Mapping[str, float]] = field(default_factory=dict)


@dataclass(frozen=True)
class PlacementSummary:
    policy_name: str
    total_tasks: int
    dependency_agent_switches: int
    mean_assignment_locality: float
    total_logical_handoff_refs: int
    total_logical_handoff_chars: int
    assignment_distribution: Mapping[str, int]


# Historical name kept as an alias; use PlacementContextState in new code.
AgentContextState = PlacementContextState
