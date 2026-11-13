"""Final-3 R-5 / R-6 / R-7: budget wiring, failure precedence and token semantics.

These helpers are the ones the coding session uses; testing them directly is what makes the
behaviour checkable without a sandbox or a model.
"""

from __future__ import annotations

from pathlib import Path

from anycode.types import TokenUsage

from atlasos.app.coding import (
    CodingRunOptions,
    _budget_evidence,
    _build_run_budget,
    _merge_failures,
    _token_evidence,
    _UATFailure,
)
from atlasos.app.service import _optional_int


def _options(**overrides: object) -> CodingRunOptions:
    base: dict[str, object] = {"task": "t", "fixture": Path("."), "runs_root": Path(".")}
    base.update(overrides)
    return CodingRunOptions(**base)  # type: ignore[arg-type]


def test_budget_is_absent_unless_requested() -> None:
    assert _build_run_budget(_options()) is None
    assert _budget_evidence(None) is None


def test_budget_is_run_scoped_and_recorded() -> None:
    state = _build_run_budget(_options(token_budget=5000))

    assert state is not None
    assert state.limit_tokens == 5000
    evidence = _budget_evidence(state)
    assert evidence is not None
    assert evidence["limit_tokens"] == 5000
    assert evidence["used_tokens"] == 0
    assert evidence["admission_rejections"] == 0
    assert evidence["stop_reason"] is None


def test_budget_rejects_more_than_the_run_limit() -> None:
    import asyncio

    import pytest

    from atlasos.budget import RunBudgetExhausted

    state = _build_run_budget(_options(token_budget=100))
    assert state is not None

    async def _try_admit() -> object:
        return await state.admit(worker="w", prompt_estimate=200, completion_reserve=0)

    with pytest.raises(RunBudgetExhausted):
        asyncio.run(_try_admit())
    assert state.admission_rejections == 1
    assert state.stop_reason is not None


def test_service_budget_switch_is_strict() -> None:
    assert _optional_int(None) is None
    assert _optional_int("") is None
    assert _optional_int("abc") is None
    assert _optional_int("0") is None
    assert _optional_int("-5") is None
    assert _optional_int("120000") == 120000


def test_agent_failure_survives_without_cleanup_failure() -> None:
    agent = _UATFailure("MODEL_UNFINISHED", "agent_loop", "no success")

    primary, secondary = _merge_failures(agent, None)

    assert primary is agent
    assert secondary is None


def test_cleanup_failure_outranks_agent_failure_and_keeps_both() -> None:
    agent = _UATFailure("MODEL_UNFINISHED", "agent_loop", "no success")
    cleanup = _UATFailure("SANDBOX_PROVIDER_FAILURE", "sandbox_cleanup", "not destroyed")

    primary, secondary = _merge_failures(agent, cleanup)

    assert primary is cleanup
    assert secondary is agent


def test_cleanup_failure_alone_is_reported() -> None:
    cleanup = _UATFailure("SANDBOX_PROVIDER_FAILURE", "sandbox_cleanup", "not destroyed")

    primary, secondary = _merge_failures(None, cleanup)

    assert primary is cleanup
    assert secondary is None


def test_no_failures_stay_absent() -> None:
    assert _merge_failures(None, None) == (None, None)


def test_token_evidence_splits_cache_and_never_collapses_it() -> None:
    usage = TokenUsage(input_tokens=30, output_tokens=7, cache_read_input_tokens=70)

    evidence = _token_evidence(usage)

    assert evidence["prompt_tokens"] == 30  # legacy meaning: cache miss only
    assert evidence["cache_miss_tokens"] == 30
    assert evidence["cache_hit_tokens"] == 70
    assert evidence["total_prompt_tokens"] == 100
    assert evidence["completion_tokens"] == 7


def test_token_evidence_without_cache_split_reports_zero_hit_only_when_reported() -> None:
    usage = TokenUsage(input_tokens=42, output_tokens=3)

    evidence = _token_evidence(usage)

    assert evidence["cache_hit_tokens"] == 0
    assert evidence["total_prompt_tokens"] == 42
