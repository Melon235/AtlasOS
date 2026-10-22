"""Deterministic task-placement policies (pre-placement planner)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from atlasos.context.requirements import ContextRequirement
from atlasos.placement.models import AgentContextState, AgentProfile, PlacementDecision
from atlasos.workflow import AtlasTaskSpec


@dataclass(frozen=True)
class PlacementWeights:
    locality: float = 1.0
    load: float = 0.2


class StaticPlacementPolicy:
    """Baseline that keeps the workflow's pre-assigned agents (not upstream_default)."""

    name = "static_baseline"

    def choose(
        self,
        *,
        task: AtlasTaskSpec,
        requirements,
        candidates: Sequence[AgentProfile],
        agent_context: Mapping[str, AgentContextState],
        load: Mapping[str, int],
        weights: PlacementWeights,
    ) -> PlacementDecision:
        del candidates, load, weights
        required = tuple(requirements.required_context)
        total = len(required)
        state = agent_context[task.assignee]
        matched = sum(1 for requirement in required if state.has_requirement(requirement))
        locality = 1.0 if total == 0 else matched / total
        return PlacementDecision(
            task_id=task.task_id,
            agent_id=task.assignee,
            capability_score=1.0,
            context_locality=locality,
            load_score=0.0,
            total_score=0.0,
            policy_name=self.name,
            candidate_agent_ids=(task.assignee,),
            required_context_count=total,
            matched_context_count=matched,
            logical_handoff_refs=total - matched,
            candidate_scores={},
        )


class CapabilityOnlyPolicy:
    """Eligibility by capability; deterministic tie-break by load then agent id."""

    name = "capability_only"

    def choose(
        self,
        *,
        task: AtlasTaskSpec,
        requirements,
        candidates: Sequence[AgentProfile],
        agent_context: Mapping[str, AgentContextState],
        load: Mapping[str, int],
        weights: PlacementWeights,
    ) -> PlacementDecision:
        del agent_context
        eligible = [
            candidate for candidate in candidates if _has_capabilities(candidate, requirements)
        ]
        if not eligible:
            raise ValueError(f"no eligible agent for task {task.task_id!r}")
        scores = {
            candidate.agent_id: 1.0 - weights.load * load.get(candidate.agent_id, 0)
            for candidate in eligible
        }
        chosen = max(
            sorted(eligible, key=lambda candidate: candidate.agent_id),
            key=lambda candidate: (scores[candidate.agent_id], -load.get(candidate.agent_id, 0)),
        )
        return PlacementDecision(
            task_id=task.task_id,
            agent_id=chosen.agent_id,
            capability_score=1.0,
            context_locality=0.0,
            load_score=float(load.get(chosen.agent_id, 0)),
            total_score=scores[chosen.agent_id],
            policy_name=self.name,
            candidate_agent_ids=tuple(candidate.agent_id for candidate in eligible),
            required_context_count=len(requirements.required_context),
            matched_context_count=0,
            logical_handoff_refs=len(requirements.required_context),
            candidate_scores={
                agent_id: {
                    "capability": 1.0,
                    "locality": 0.0,
                    "load": float(load.get(agent_id, 0)),
                    "score": score,
                }
                for agent_id, score in scores.items()
            },
        )


class ContextLocalityPolicy:
    """Capability filter plus version-aware logical context locality."""

    name = "context_locality"

    def choose(
        self,
        *,
        task: AtlasTaskSpec,
        requirements,
        candidates: Sequence[AgentProfile],
        agent_context: Mapping[str, AgentContextState],
        load: Mapping[str, int],
        weights: PlacementWeights,
    ) -> PlacementDecision:
        eligible = [
            candidate for candidate in candidates if _has_capabilities(candidate, requirements)
        ]
        if not eligible:
            raise ValueError(f"no eligible agent for task {task.task_id!r}")

        required: tuple[ContextRequirement, ...] = tuple(requirements.required_context)
        total = len(required)
        rows: dict[str, dict[str, float]] = {}
        for candidate in eligible:
            state = agent_context[candidate.agent_id]
            matched = sum(1 for requirement in required if state.has_requirement(requirement))
            locality = 1.0 if total == 0 else matched / total
            load_value = float(load.get(candidate.agent_id, 0))
            score = weights.locality * locality - weights.load * load_value
            rows[candidate.agent_id] = {
                "capability": 1.0,
                "locality": locality,
                "load": load_value,
                "score": score,
                "matched": float(matched),
            }

        chosen_id = max(
            sorted(rows),
            key=lambda agent_id: (
                rows[agent_id]["score"],
                -rows[agent_id]["load"],
                _reverse_lexicographic(agent_id),
            ),
        )
        chosen = rows[chosen_id]
        matched_count = int(chosen["matched"])
        return PlacementDecision(
            task_id=task.task_id,
            agent_id=chosen_id,
            capability_score=1.0,
            context_locality=chosen["locality"],
            load_score=chosen["load"],
            total_score=chosen["score"],
            policy_name=self.name,
            candidate_agent_ids=tuple(candidate.agent_id for candidate in eligible),
            required_context_count=total,
            matched_context_count=matched_count,
            logical_handoff_refs=total - matched_count,
            candidate_scores={
                agent_id: {key: value for key, value in row.items() if key != "matched"}
                for agent_id, row in rows.items()
            },
        )


def _has_capabilities(profile: AgentProfile, requirements) -> bool:
    return requirements.required_capabilities <= profile.capabilities


def _reverse_lexicographic(agent_id: str) -> str:
    """Make the deterministic tie-break prefer the lexicographically smallest agent id."""

    return "".join(chr(0x10FFFF - ord(char)) for char in agent_id)
