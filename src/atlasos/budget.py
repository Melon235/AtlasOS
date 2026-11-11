"""Run-scoped LLM token budget admission (Stage G2a).

Audit summary — before this module existed, nothing enforced a token budget at runtime:

* ``anycode.guardrails.budget.BudgetTracker`` is the only token accounting in AnyCode. It is built
  once per ``AgentRunner`` (``core/runner.py:132``) from a ``GuardrailConfig`` that AtlasOS never
  supplies, so ``is_exhausted()`` always returned ``False``. Even when configured, its counter is
  **per agent** and ``max_tokens_per_team`` is never read anywhere.
* The 120k limit lived only in ``experiments/evaluation/runner.py``: a post-hoc comparison of
  ``prompt_tokens + completion_tokens`` against ``settings.token_budget`` after the run finished.
* Consequence: concurrent workers each believed they had the full budget, and a run could spend
  373k tokens before being labelled ``BUDGET_EXCEEDED``.

This module adds the missing admission control. One ``RunBudgetState`` is created per run and
shared by every worker; ``RunBudgetHook`` is a standard AnyCode ``TurnHook`` whose ``before_turn``
runs immediately before each LLM request (``core/runner.py:322``) and whose ``after_turn`` receives
the provider response (``core/runner.py:464``). No AnyCode code is modified and no second budget
system is introduced — the upstream tracker stays untouched and unused.

Control boundary (documented, not overstated): prompt tokens are estimated before the request with
the repository's existing tokenizer selection (``tiktoken`` when installed, heuristic otherwise)
and the estimate is corrected by the observed estimate/actual ratio of earlier requests in the same
run. Completion is reserved at the model's ``max_tokens`` cap. Retried provider attempts inside
``ResilientAdapter`` are invisible until the final response, so this is a conservative admission
control, **not** a mathematical guarantee of a 0-token overrun.
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass, field
from typing import Any

from anycode.context.tokenizer import select_tokenizer
from anycode.types import AgentInfo, LLMMessage, LLMResponse, TokenUsage

#: Stable prefix used to classify a provider-visible failure as a budget rejection.
REJECTION_CODE = "budget_exhausted"
#: ``legacy`` keeps the historical (post-hoc only) behaviour; ``guarded`` enables admission.
BUDGET_MODE_LEGACY = "legacy"
BUDGET_MODE_GUARDED = "guarded"
BUDGET_MODES = (BUDGET_MODE_LEGACY, BUDGET_MODE_GUARDED)
#: Upper bound for the learned estimate correction ratio.
MAX_LEARNED_RATIO = 4.0
#: Completion reservation when the agent config does not pin ``max_tokens``.
DEFAULT_COMPLETION_RESERVE = 4096
#: Bounded per-request sample list kept for the estimate-vs-actual report.
MAX_SAMPLES = 64


class RunBudgetExhausted(RuntimeError):
    """Raised before the provider call when the run budget cannot admit the request."""

    def __init__(self, message: str, *, requested: int, remaining: int) -> None:
        super().__init__(f"{REJECTION_CODE}: {message}")
        self.requested_tokens = requested
        self.remaining_tokens = remaining


@dataclass(frozen=True)
class BudgetReservation:
    """One admitted-but-unsettled LLM request."""

    request_id: int
    worker: str
    raw_prompt_estimate: int
    estimated_prompt_tokens: int
    reserved_completion_tokens: int
    reserved_tokens: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "worker": self.worker,
            "raw_prompt_estimate": self.raw_prompt_estimate,
            "estimated_prompt_tokens": self.estimated_prompt_tokens,
            "reserved_completion_tokens": self.reserved_completion_tokens,
            "reserved_tokens": self.reserved_tokens,
        }


@dataclass
class _WorkerCounters:
    started: int = 0
    completed: int = 0
    rejected: int = 0
    written_off: int = 0
    actual_tokens: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "started": self.started,
            "completed": self.completed,
            "rejected": self.rejected,
            "written_off": self.written_off,
            "actual_tokens": self.actual_tokens,
        }


@dataclass
class RunBudgetState:
    """Token budget shared by every worker of one run.

    ``admit`` and ``settle`` are atomic with respect to each other through ``asyncio.Lock``; the
    lock never spans a provider call. Separate runs construct separate states and cannot share
    quota.
    """

    limit_tokens: int
    safety_factor: float = 1.15
    max_samples: int = MAX_SAMPLES
    used_prompt_tokens: int = 0
    used_completion_tokens: int = 0
    reserved_tokens: int = 0
    written_off_tokens: int = 0
    peak_reserved_tokens: int = 0
    requests_started: int = 0
    requests_completed: int = 0
    requests_written_off: int = 0
    requests_released: int = 0
    admission_rejections: int = 0
    budget_overrun_tokens: int = 0
    estimate_abs_error_tokens: int = 0
    estimate_samples: int = 0
    learned_ratio: float = 1.0
    last_rejection: str | None = None
    last_write_off_reason: str | None = None
    last_release_reason: str | None = None
    _request_seq: int = 0
    _in_flight: dict[int, BudgetReservation] = field(default_factory=dict)
    _workers: dict[str, _WorkerCounters] = field(default_factory=dict)
    _samples: list[dict[str, Any]] = field(default_factory=list)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    def __post_init__(self) -> None:
        if self.limit_tokens <= 0:
            raise ValueError("limit_tokens must be positive")
        self.safety_factor = max(1.0, float(self.safety_factor))

    # -- read-only views --

    @property
    def used_tokens(self) -> int:
        return self.used_prompt_tokens + self.used_completion_tokens + self.written_off_tokens

    @property
    def remaining_tokens(self) -> int:
        return max(0, self.limit_tokens - self.used_tokens - self.reserved_tokens)

    @property
    def in_flight(self) -> int:
        return len(self._in_flight)

    def worker(self, name: str) -> _WorkerCounters:
        return self._workers.setdefault(name, _WorkerCounters())

    @property
    def stop_reason(self) -> str | None:
        if self.admission_rejections:
            return f"{REJECTION_CODE}: {self.last_rejection}"
        if self.used_tokens >= self.limit_tokens:
            return (
                f"{REJECTION_CODE}: run budget fully consumed "
                f"({self.used_tokens}/{self.limit_tokens})"
            )
        return None

    # -- admission / settlement --

    def _scaled_prompt_estimate(self, raw: int) -> int:
        ratio = max(1.0, self.learned_ratio)
        return max(1, math.ceil(raw * self.safety_factor * ratio))

    async def admit(
        self,
        *,
        worker: str,
        prompt_estimate: int,
        completion_reserve: int = 0,
    ) -> BudgetReservation:
        """Reserve budget for one request or raise :class:`RunBudgetExhausted`."""

        raw = max(1, int(prompt_estimate))
        completion = max(0, int(completion_reserve))
        async with self._lock:
            requested = self._scaled_prompt_estimate(raw) + completion
            remaining = self.remaining_tokens
            counters = self.worker(worker)
            if requested > remaining:
                self.admission_rejections += 1
                counters.rejected += 1
                self.last_rejection = (
                    f"worker {worker} needs ~{requested} tokens "
                    f"(used {self.used_tokens}, reserved {self.reserved_tokens}), "
                    f"{remaining} of {self.limit_tokens} left"
                )
                if self.requests_started == 0 and not self.reserved_tokens:
                    self.last_rejection += "; no request has been admitted in this run"
                raise RunBudgetExhausted(
                    self.last_rejection, requested=requested, remaining=remaining
                )
            self._request_seq += 1
            reservation = BudgetReservation(
                request_id=self._request_seq,
                worker=worker,
                raw_prompt_estimate=raw,
                estimated_prompt_tokens=requested - completion,
                reserved_completion_tokens=completion,
                reserved_tokens=requested,
            )
            self._in_flight[reservation.request_id] = reservation
            self.reserved_tokens += requested
            self.peak_reserved_tokens = max(self.peak_reserved_tokens, self.reserved_tokens)
            self.requests_started += 1
            counters.started += 1
            return reservation

    async def settle(
        self,
        reservation: BudgetReservation,
        *,
        prompt_tokens: int,
        completion_tokens: int,
    ) -> None:
        """Replace a reservation with the provider-reported usage."""

        prompt = max(0, int(prompt_tokens))
        completion = max(0, int(completion_tokens))
        async with self._lock:
            if self._in_flight.pop(reservation.request_id, None) is None:
                return
            self.reserved_tokens = max(0, self.reserved_tokens - reservation.reserved_tokens)
            self.used_prompt_tokens += prompt
            self.used_completion_tokens += completion
            self.requests_completed += 1
            counters = self.worker(reservation.worker)
            counters.completed += 1
            counters.actual_tokens += prompt + completion
            actual = prompt + completion
            if actual > reservation.reserved_tokens:
                self.budget_overrun_tokens += actual - reservation.reserved_tokens
            self._learn(reservation, prompt_tokens=prompt)
            if len(self._samples) < self.max_samples:
                self._samples.append(
                    {
                        "request_id": reservation.request_id,
                        "worker": reservation.worker,
                        "estimated_tokens": reservation.reserved_tokens,
                        "actual_tokens": actual,
                        "actual_prompt_tokens": prompt,
                        "actual_completion_tokens": completion,
                    }
                )

    async def abandon(self, reservation: BudgetReservation, *, reason: str) -> None:
        """Release a reservation only when the request provably never reached the provider."""

        async with self._lock:
            if self._in_flight.pop(reservation.request_id, None) is None:
                return
            self.reserved_tokens = max(0, self.reserved_tokens - reservation.reserved_tokens)
            self.requests_released += 1
            self.last_release_reason = f"{reason} (request {reservation.request_id})"

    async def write_off(self, reservation: BudgetReservation, *, reason: str) -> None:
        """Charge an unknown-outcome request at its reserved amount instead of releasing it."""

        async with self._lock:
            if self._in_flight.pop(reservation.request_id, None) is None:
                return
            self.reserved_tokens = max(0, self.reserved_tokens - reservation.reserved_tokens)
            self.written_off_tokens += reservation.reserved_tokens
            self.requests_written_off += 1
            self.worker(reservation.worker).written_off += 1
            self.last_write_off_reason = f"{reason} (request {reservation.request_id})"

    def _learn(self, reservation: BudgetReservation, *, prompt_tokens: int) -> None:
        """Track the raw estimator error and converge the correction ratio.

        The correction is an EMA rather than a running maximum: a single early outlier (for
        example a first prompt dominated by tool schemas) must not inflate every later
        reservation, which would stop runs that are in fact within budget.
        """

        raw = max(1, reservation.raw_prompt_estimate)
        observed = prompt_tokens / raw
        blended = 0.5 * self.learned_ratio + 0.5 * observed
        self.learned_ratio = min(MAX_LEARNED_RATIO, max(1.0, blended))
        self.estimate_abs_error_tokens += abs(raw - prompt_tokens)
        self.estimate_samples += 1

    def finalize(self) -> dict[str, Any]:
        """Write off still-unsettled reservations and freeze the counters.

        Must only be called once the run has quiesced. A reservation whose request never settled
        (provider error, timeout, cancellation) keeps its budget instead of being released: the
        usage is unknown, so assuming zero would be unsafe.
        """

        outstanding = list(self._in_flight.values())
        if outstanding:
            self._in_flight.clear()
            for reservation in outstanding:
                self.reserved_tokens = max(0, self.reserved_tokens - reservation.reserved_tokens)
                self.written_off_tokens += reservation.reserved_tokens
                self.requests_written_off += 1
                self.worker(reservation.worker).written_off += 1
            self.last_write_off_reason = (
                f"{len(outstanding)} request(s) never settled; charged at the reserved amount"
            )
        return self.as_dict()

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": BUDGET_MODE_GUARDED,
            "limit_tokens": self.limit_tokens,
            "used_prompt_tokens": self.used_prompt_tokens,
            "used_completion_tokens": self.used_completion_tokens,
            "used_tokens": self.used_tokens,
            "written_off_tokens": self.written_off_tokens,
            "reserved_tokens": self.reserved_tokens,
            "remaining_tokens": self.remaining_tokens,
            "peak_reserved_tokens": self.peak_reserved_tokens,
            "requests_started": self.requests_started,
            "requests_completed": self.requests_completed,
            "requests_written_off": self.requests_written_off,
            "requests_released": self.requests_released,
            "in_flight": self.in_flight,
            "admission_rejections": self.admission_rejections,
            "budget_overrun_tokens": self.budget_overrun_tokens,
            "estimate_abs_error_tokens": self.estimate_abs_error_tokens,
            "estimate_samples": self.estimate_samples,
            "learned_ratio": round(self.learned_ratio, 4),
            "stop_reason": self.stop_reason,
            "last_rejection": self.last_rejection,
            "last_write_off_reason": self.last_write_off_reason,
            "last_release_reason": self.last_release_reason,
            "workers": {name: item.as_dict() for name, item in sorted(self._workers.items())},
            "samples": list(self._samples),
        }


def prompt_tokens_from_usage(usage: TokenUsage) -> int:
    """Full prompt size for one provider call.

    ``TokenUsage.input_tokens`` excludes cached input for providers that report the split, so the
    cache counters must be added back. Cache hits are prompt tokens: they count once, never twice.
    """

    return (
        max(0, int(usage.input_tokens))
        + max(0, int(usage.cache_read_input_tokens))
        + max(0, int(usage.cache_creation_input_tokens))
    )


class RunBudgetHook:
    """Per-agent ``TurnHook`` that admits every LLM request against the shared run budget."""

    def __init__(
        self,
        state: RunBudgetState,
        *,
        worker: str,
        model: str | None = None,
        system_prompt: str | None = None,
        tool_definitions: str | None = None,
        completion_reserve: int | None = None,
    ) -> None:
        self._state = state
        self._worker = worker
        self._completion_reserve = (
            DEFAULT_COMPLETION_RESERVE if completion_reserve is None else max(0, completion_reserve)
        )
        self._tokenizer = select_tokenizer("tiktoken", model=model)
        # ``before_turn`` only sees the conversation; the system prompt and the tool schemas are
        # sent alongside it, so they are counted once here as a fixed prefix.
        self._static_prefix_tokens = sum(
            self._tokenizer.count_text(text) for text in (system_prompt, tool_definitions) if text
        )
        self._outstanding: BudgetReservation | None = None
        self._rejection: str | None = None

    @property
    def state(self) -> RunBudgetState:
        return self._state

    @property
    def worker(self) -> str:
        return self._worker

    @property
    def estimate_confidence(self) -> str:
        return self._tokenizer.confidence

    @property
    def outstanding(self) -> BudgetReservation | None:
        return self._outstanding

    def estimate_prompt_tokens(self, messages: list[LLMMessage]) -> int:
        return self._tokenizer.count_messages(messages) + self._static_prefix_tokens

    def take_rejection(self) -> str | None:
        """Return and clear the pending admission rejection for this worker, if any."""

        reason, self._rejection = self._rejection, None
        return reason

    async def before_turn(self, messages: list[LLMMessage], context: AgentInfo) -> list[LLMMessage]:
        del context
        if self._outstanding is not None:
            # Defensive: a previous turn of this agent never settled. Charge it before admitting.
            await self._state.write_off(
                self._outstanding, reason="previous turn of the same worker did not settle"
            )
            self._outstanding = None
        try:
            self._outstanding = await self._state.admit(
                worker=self._worker,
                prompt_estimate=self.estimate_prompt_tokens(messages),
                completion_reserve=self._completion_reserve,
            )
        except RunBudgetExhausted as error:
            # Remember the reason: AnyCode ends the turn loop on any exception and would otherwise
            # report an empty, "successful" run. The agent surfaces this through the run result.
            self._rejection = str(error)
            raise
        return messages

    async def after_turn(self, response: LLMResponse, context: AgentInfo) -> LLMResponse:
        del context
        reservation, self._outstanding = self._outstanding, None
        if reservation is None:
            return response
        await self._state.settle(
            reservation,
            prompt_tokens=prompt_tokens_from_usage(response.usage),
            completion_tokens=response.usage.output_tokens,
        )
        return response
