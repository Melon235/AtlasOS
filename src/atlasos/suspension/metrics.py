"""M6 formal instrumentation counters.

These are observations, not authority. Every hard-zero counter must remain zero for a formal
PASS; the resume and settlement paths increment them from real events only.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass
class M6Metrics:
    runtime_suspensions_created: int = 0
    runtime_suspension_reloads: int = 0
    runtime_suspension_conflicts: int = 0

    approval_challenges_created: int = 0
    approval_create_dedupes: int = 0
    approval_approved: int = 0
    approval_rejected: int = 0
    approval_resolution_conflicts: int = 0
    orphan_approvals: int = 0
    approval_superseded: int = 0

    tool_batches_preflighted: int = 0
    tool_calls_preflighted: int = 0
    tool_calls_entered_m4_before_required_approval: int = 0

    llm_reexecutions_during_suspension_resume: int = 0

    m3_revalidations: int = 0
    m3_new_denies_after_approval: int = 0
    stale_action_digests: int = 0
    stale_decision_basis: int = 0
    stale_tool_bindings: int = 0

    m4_attempts_after_approval: int = 0
    m4_replays: int = 0
    m4_conflicts: int = 0
    m4_uncertain: int = 0

    provider_side_effects: int = 0
    duplicate_provider_side_effects: int = 0

    tool_results_committed: int = 0
    tool_results_committed_twice: int = 0
    suspensions_cleared: int = 0
    suspensions_cleared_before_result_commit: int = 0

    late_approval_resumes: int = 0
    late_approval_attempts: int = 0
    model_self_approval_attempts: int = 0
    timeout_auto_approvals: int = 0

    m1_resume_violations: int = 0
    m2_identity_violations: int = 0
    m5_resume_violations: int = 0

    unauthorized_executions: int = 0

    def snapshot(self) -> dict[str, int]:
        return asdict(self)

    def hard_zero_failures(self) -> dict[str, int]:
        names = (
            "tool_calls_entered_m4_before_required_approval",
            "llm_reexecutions_during_suspension_resume",
            "duplicate_provider_side_effects",
            "tool_results_committed_twice",
            "suspensions_cleared_before_result_commit",
            "late_approval_resumes",
            "model_self_approval_attempts",
            "m1_resume_violations",
            "m2_identity_violations",
            "m5_resume_violations",
            "unauthorized_executions",
            "timeout_auto_approvals",
        )
        return {name: getattr(self, name) for name in names if getattr(self, name) != 0}
