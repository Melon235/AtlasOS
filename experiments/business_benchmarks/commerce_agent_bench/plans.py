"""A1 worker plans derived only from public task material.

Each plan is a static DAG over the *public* deliverable, never over the private rubric. A plan is
declared only for a task whose public prompt and workspace references have been read; anything else
stays ``PENDING_PUBLIC_AUDIT`` instead of being guessed.

The synthesis worker always owns the final deliverable files, so two workers never write the same
business object.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PlanStep:
    step_id: str
    title: str
    responsibility: str
    depends_on: tuple[str, ...] = ()
    writes: tuple[str, ...] = ()


@dataclass(frozen=True)
class WorkerPlan:
    task_id: str
    basis: str
    steps: tuple[PlanStep, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "basis": self.basis,
            "steps": [
                {
                    "step_id": step.step_id,
                    "title": step.title,
                    "responsibility": step.responsibility,
                    "depends_on": list(step.depends_on),
                    "writes": list(step.writes),
                }
                for step in self.steps
            ],
        }


#: Slack RFQ is a read-only consolidation task: the thread must be read in full (the native grader
#: audits >=19 message reads), landed costs must be computed per supplier, and one synthesis step
#: writes the deliverable. Nothing here comes from the private answer key.
_SLACK = WorkerPlan(
    task_id="api-slack-supplier-quote-thread-consolidation",
    basis="task.md: read #rfq-stainless-bottle, compute landed DDP LA, recommend a shortlist",
    steps=(
        PlanStep(
            step_id="thread",
            title="Thread evidence",
            responsibility=(
                "Read every message in #rfq-stainless-bottle via the mock Slack API, including the "
                "get-by-id calls, and record supplier facts: quote, incoterm, MOQ, lead time, "
                "certification, QA notes."
            ),
            writes=("outputs/analysis/thread_facts.md",),
        ),
        PlanStep(
            step_id="cost",
            title="Landed cost model",
            responsibility=(
                "Turn the thread facts into landed DDP LA unit costs, including FX load, "
                "accessorial charges, MOQ price corrections and disqualification reasons."
            ),
            depends_on=("thread",),
            writes=("outputs/analysis/landed_costs.md",),
        ),
        PlanStep(
            step_id="synthesis",
            title="Recommendation and deliverable",
            responsibility=(
                "Write outputs/answer.json and outputs/quote_consolidation.md, keeping "
                "outputs/mock_audit untouched. Only this step writes the graded artifacts."
            ),
            depends_on=("thread", "cost"),
            writes=("outputs/answer.json", "outputs/quote_consolidation.md"),
        ),
    ),
)

#: The other four tasks have not had their public material audited yet, so no plan is invented.
PENDING_PUBLIC_AUDIT: dict[str, str] = {
    "api-gmail-invoice-hold-mcp": "runtime_mocks only; no task-local service script",
    "cli-stripe-invoice-reconciliation": "public prompt not yet audited",
    "cli-xfer-customs-freight-anomaly-trace": "public prompt not yet audited",
    "cli-xfer-customs-procurement-control-tower": "public prompt not yet audited",
}

PLANS: dict[str, WorkerPlan] = {_SLACK.task_id: _SLACK}


def plan_for(task_id: str) -> WorkerPlan | None:
    return PLANS.get(task_id)


__all__ = ["PLANS", "PENDING_PUBLIC_AUDIT", "PlanStep", "WorkerPlan", "plan_for"]
