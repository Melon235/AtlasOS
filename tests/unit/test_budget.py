"""Stage G2a: run-scoped token budget admission."""

from __future__ import annotations

import asyncio

import pytest
from anycode.types import AgentInfo, LLMMessage, LLMResponse, TextBlock, TokenUsage

from atlasos.budget import (
    REJECTION_CODE,
    RunBudgetExhausted,
    RunBudgetHook,
    RunBudgetState,
    prompt_tokens_from_usage,
)

CONTEXT = AgentInfo(name="worker", role="coding", model="fake")


def _messages(chars: int = 400) -> list[LLMMessage]:
    return [LLMMessage(role="user", content=[TextBlock(text="x" * chars)])]


def _response(*, input_tokens: int = 0, output_tokens: int = 0, cache_read: int = 0) -> LLMResponse:
    return LLMResponse(
        id="resp",
        content=[TextBlock(text="done")],
        model="fake",
        stop_reason="end_turn",
        usage=TokenUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=cache_read,
        ),
    )


def _hook(state: RunBudgetState, worker: str = "worker", reserve: int = 100) -> RunBudgetHook:
    return RunBudgetHook(
        state, worker=worker, model="fake", system_prompt="sys", completion_reserve=reserve
    )


async def test_single_worker_accumulates_settled_usage() -> None:
    state = RunBudgetState(limit_tokens=10_000)
    hook = _hook(state)
    await hook.before_turn(_messages(), CONTEXT)
    assert state.reserved_tokens > 0
    await hook.after_turn(_response(input_tokens=300, output_tokens=100), CONTEXT)
    assert state.reserved_tokens == 0
    assert state.used_prompt_tokens == 300
    assert state.used_completion_tokens == 100
    assert state.used_tokens == 400
    assert state.requests_started == 1
    assert state.requests_completed == 1
    assert state.in_flight == 0


async def test_two_workers_share_one_budget_state() -> None:
    state = RunBudgetState(limit_tokens=10_000)
    first, second = _hook(state, "a"), _hook(state, "b")
    await first.before_turn(_messages(), CONTEXT)
    await first.after_turn(_response(input_tokens=1_000, output_tokens=500), CONTEXT)
    await second.before_turn(_messages(), CONTEXT)
    await second.after_turn(_response(input_tokens=2_000, output_tokens=500), CONTEXT)
    assert state.used_tokens == 4_000
    assert state.worker("a").actual_tokens == 1_500
    assert state.worker("b").actual_tokens == 2_500
    assert state.remaining_tokens == 6_000


async def test_concurrent_requests_cannot_double_reserve_the_same_quota() -> None:
    state = RunBudgetState(limit_tokens=1_000, safety_factor=1.0)

    async def admit(worker: str) -> str:
        try:
            await state.admit(worker=worker, prompt_estimate=700, completion_reserve=0)
        except RunBudgetExhausted:
            return "rejected"
        return "admitted"

    outcomes = await asyncio.gather(admit("a"), admit("b"))
    assert sorted(outcomes) == ["admitted", "rejected"]
    assert state.requests_started == 1
    assert state.admission_rejections == 1
    assert state.reserved_tokens == 700
    assert state.remaining_tokens == 300


async def test_rejection_happens_before_the_request_is_admitted() -> None:
    state = RunBudgetState(limit_tokens=200, safety_factor=1.0)
    hook = _hook(state, reserve=500)
    with pytest.raises(RunBudgetExhausted) as error:
        await hook.before_turn(_messages(), CONTEXT)
    assert str(error.value).startswith(REJECTION_CODE)
    assert hook.outstanding is None
    assert state.requests_started == 0
    assert state.reserved_tokens == 0
    assert state.admission_rejections == 1
    assert state.last_rejection is not None


async def test_unused_reservation_is_released_at_settlement() -> None:
    state = RunBudgetState(limit_tokens=10_000, safety_factor=1.0)
    reservation = await state.admit(worker="a", prompt_estimate=800, completion_reserve=200)
    assert state.reserved_tokens == 1_000
    await state.settle(reservation, prompt_tokens=300, completion_tokens=50)
    assert state.reserved_tokens == 0
    assert state.used_tokens == 350
    assert state.budget_overrun_tokens == 0
    assert state.remaining_tokens == 9_650


async def test_actual_usage_above_the_reservation_is_recorded_as_overrun() -> None:
    state = RunBudgetState(limit_tokens=10_000, safety_factor=1.0)
    reservation = await state.admit(worker="a", prompt_estimate=100, completion_reserve=0)
    await state.settle(reservation, prompt_tokens=900, completion_tokens=200)
    assert state.budget_overrun_tokens == 1_100 - 100
    assert state.learned_ratio == 4.0  # 9x observed, capped
    assert state.used_tokens == 1_100


async def test_provider_error_keeps_the_reservation_until_it_is_written_off() -> None:
    state = RunBudgetState(limit_tokens=10_000, safety_factor=1.0)
    reservation = await state.admit(worker="a", prompt_estimate=1_000, completion_reserve=0)
    assert reservation.reserved_tokens == 1_000

    # The provider raised: after_turn never runs, so the usage is unknown.
    assert state.in_flight == 1
    snapshot = state.finalize()
    assert snapshot["in_flight"] == 0
    assert snapshot["requests_written_off"] == 1
    assert snapshot["written_off_tokens"] == 1_000
    assert snapshot["used_tokens"] == 1_000
    assert snapshot["last_write_off_reason"] is not None


async def test_cancelled_request_is_charged_not_released() -> None:
    state = RunBudgetState(limit_tokens=10_000, safety_factor=1.0)
    await state.admit(worker="a", prompt_estimate=2_000, completion_reserve=0)
    # asyncio cancellation between admission and settlement: no settle() happens.
    with pytest.raises(asyncio.CancelledError):

        async def _cancelled() -> None:
            raise asyncio.CancelledError

        await _cancelled()
    assert state.reserved_tokens == 2_000
    snapshot = state.finalize()
    assert snapshot["used_tokens"] == 2_000
    assert snapshot["requests_released"] == 0
    assert state.remaining_tokens == 8_000


async def test_confirmed_unsent_request_may_release_its_reservation() -> None:
    state = RunBudgetState(limit_tokens=10_000, safety_factor=1.0)
    reservation = await state.admit(worker="a", prompt_estimate=1_500, completion_reserve=0)
    await state.abandon(reservation, reason="adapter refused before dispatch")
    assert state.reserved_tokens == 0
    assert state.written_off_tokens == 0
    assert state.requests_released == 1
    assert state.last_release_reason is not None
    assert state.remaining_tokens == 10_000


async def test_separate_runs_do_not_share_budget() -> None:
    first = RunBudgetState(limit_tokens=1_000, safety_factor=1.0)
    second = RunBudgetState(limit_tokens=1_000, safety_factor=1.0)
    await first.admit(worker="a", prompt_estimate=900, completion_reserve=0)
    admitted = await second.admit(worker="a", prompt_estimate=900, completion_reserve=0)
    assert admitted.reserved_tokens > 0
    assert first.remaining_tokens < 200
    assert second.remaining_tokens >= 0
    assert second.used_tokens == 0


async def test_prompt_and_completion_are_counted_once_with_cache_included() -> None:
    usage = TokenUsage(input_tokens=120, output_tokens=80, cache_read_input_tokens=880)
    assert prompt_tokens_from_usage(usage) == 1_000

    state = RunBudgetState(limit_tokens=10_000)
    hook = _hook(state, reserve=0)
    await hook.before_turn(_messages(), CONTEXT)
    await hook.after_turn(_response(input_tokens=120, output_tokens=80, cache_read=880), CONTEXT)
    assert state.used_prompt_tokens == 1_000
    assert state.used_completion_tokens == 80
    assert state.used_tokens == 1_080


async def test_later_request_sees_the_whole_run_not_only_its_own_worker() -> None:
    state = RunBudgetState(limit_tokens=1_000, safety_factor=1.0)
    worker_a, worker_b = _hook(state, "a", reserve=0), _hook(state, "b", reserve=0)

    await worker_a.before_turn(_messages(), CONTEXT)
    await worker_a.after_turn(_response(input_tokens=400, output_tokens=0), CONTEXT)
    await worker_b.before_turn(_messages(), CONTEXT)
    await worker_b.after_turn(_response(input_tokens=400, output_tokens=0), CONTEXT)
    assert state.used_tokens == 800

    # Worker A's own spend is 400, but the run only has 200 left.
    with pytest.raises(RunBudgetExhausted):
        await state.admit(worker="a", prompt_estimate=500, completion_reserve=0)
    assert state.worker("a").rejected == 1
    assert state.remaining_tokens == 200


async def test_estimate_ratio_learns_from_settled_requests() -> None:
    state = RunBudgetState(limit_tokens=100_000)
    hook = _hook(state, reserve=0)
    await hook.before_turn(_messages(4_000), CONTEXT)
    await hook.after_turn(_response(input_tokens=4_000, output_tokens=10), CONTEXT)
    assert state.learned_ratio > 1.0
    assert state.estimate_samples == 1
    assert state.estimate_abs_error_tokens >= 0

    reservation = await state.admit(worker="worker", prompt_estimate=1_000, completion_reserve=0)
    assert reservation.estimated_prompt_tokens > 1_000


async def test_stop_reason_is_reported_after_rejection() -> None:
    state = RunBudgetState(limit_tokens=100, safety_factor=1.0)
    with pytest.raises(RunBudgetExhausted):
        await state.admit(worker="a", prompt_estimate=500, completion_reserve=0)
    snapshot = state.as_dict()
    assert snapshot["stop_reason"].startswith(REJECTION_CODE)
    assert snapshot["admission_rejections"] == 1
    assert snapshot["workers"]["a"]["rejected"] == 1
    assert snapshot["workers"]["a"]["started"] == 0


def test_state_rejects_a_non_positive_limit() -> None:
    with pytest.raises(ValueError):
        RunBudgetState(limit_tokens=0)
