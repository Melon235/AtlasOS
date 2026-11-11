"""Stage G3-1: completed-but-failing sandbox commands are known errors, not unknown effects.

The mapping under test lives in ``SandboxWorkspaceTools.bash``: a command whose exit status was
observed (``SandboxCommandResult.ok is True``) is a *known* outcome, so ``corrected`` mode marks it
``retry_safe=True`` and the worker keeps running. Anything whose outcome was not observed
(transport failure, timeout, lost sandbox) stays ``retry_safe=False`` in both modes, and the
runner keeps its ``side_effect_unknown`` stop.

These tests never spawn a real sandbox process: ``_execute`` is scripted, so the provider call is
counted and the command runs exactly once.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest
from anycode.contracts.models import ContractError
from anycode.sandbox import SandboxCommandResult
from anycode.tools.bash import BashInput
from anycode.types import ToolResult

from atlasos.sandbox.models import SandboxLostError
from atlasos.sandbox.tools import (
    TOOL_ERROR_MODE_CORRECTED,
    TOOL_ERROR_MODE_LEGACY,
    SandboxWorkspaceTools,
)


class _Manager:
    """Only the two manager calls the bash path can make."""

    def __init__(self) -> None:
        self.lost: list[str] = []

    async def mark_lost(self, task_id: str) -> None:
        self.lost.append(task_id)


class _ScriptedTools(SandboxWorkspaceTools):
    """Real bash mapping, scripted provider results, no sandbox process."""

    def __init__(self, results: list[Any], *, mode: str) -> None:
        super().__init__(
            cast(Any, _Manager()),
            "task-g3",
            provider=cast(Any, SimpleNamespace()),
            tool_error_mode=mode,
        )
        self._scripted = list(results)
        self.executions: list[str] = []

    def _binding(self) -> Any:
        return SimpleNamespace(
            workspace_root="/workspace/project",
            sandbox_handle=SimpleNamespace(id="handle-g3"),
        )

    async def _execute(self, command: str, *, cwd: str, timeout_seconds: int = 60) -> Any:
        del cwd, timeout_seconds
        self.executions.append(command)
        item = self._scripted.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _bash(tools: SandboxWorkspaceTools, command: str) -> ToolResult:
    return asyncio.run(tools.bash(BashInput(command=command), cast(Any, SimpleNamespace())))


@pytest.mark.parametrize("mode", [TOOL_ERROR_MODE_LEGACY, TOOL_ERROR_MODE_CORRECTED])
def test_completed_success_is_retry_safe(mode: str) -> None:
    tools = _ScriptedTools([SandboxCommandResult(ok=True, exit_code=0, stdout="hello")], mode=mode)
    result = _bash(tools, "echo hello")
    assert result.is_error is False
    # legacy leaves the field unset (AnyCode infers True for a non-error); corrected states it.
    assert result.retry_safe is (True if mode == TOOL_ERROR_MODE_CORRECTED else None)
    assert _normalized(result, side_effecting=True) is True
    assert "hello" in result.data
    assert tools.executions == ["echo hello"]


def test_completed_non_zero_exit_is_a_known_error_in_corrected_mode() -> None:
    tools = _ScriptedTools(
        [
            SandboxCommandResult(
                ok=True,
                exit_code=2,
                stdout="",
                stderr="ls: cannot access '/nonexistent': No such file or directory",
            )
        ],
        mode=TOOL_ERROR_MODE_CORRECTED,
    )
    result = _bash(tools, "ls /nonexistent")
    assert result.is_error is True
    assert result.retry_safe is True
    assert "No such file or directory" in result.data
    assert "(exit code 2)" in result.data
    # The command ran exactly once: the mapping never re-executes it.
    assert tools.executions == ["ls /nonexistent"]


def test_legacy_mode_leaves_the_inference_untouched() -> None:
    tools = _ScriptedTools(
        [SandboxCommandResult(ok=True, exit_code=2, stderr="boom")], mode=TOOL_ERROR_MODE_LEGACY
    )
    result = _bash(tools, "ls /nonexistent")
    assert result.is_error is True
    assert result.retry_safe is None  # AnyCode infers False for side-effecting errors
    assert _normalized(result, side_effecting=True) is False


def test_python_traceback_keeps_the_error_and_reports_the_exit_code() -> None:
    tools = _ScriptedTools(
        [
            SandboxCommandResult(
                ok=True,
                exit_code=1,
                stderr=(
                    'Traceback (most recent call last):\n  File "script.py", line 1\nValueError: x'
                ),
            )
        ],
        mode=TOOL_ERROR_MODE_CORRECTED,
    )
    result = _bash(tools, "python script.py")
    assert result.is_error is True
    assert result.retry_safe is True
    assert "Traceback" in result.data and "ValueError: x" in result.data
    assert tools.executions == ["python script.py"]


def test_side_effect_before_failure_is_not_replayed() -> None:
    """The command completed once; the mapping is pure and never triggers a second run."""

    tools = _ScriptedTools(
        [SandboxCommandResult(ok=True, exit_code=1, stdout="appended")],
        mode=TOOL_ERROR_MODE_CORRECTED,
    )
    first = _bash(tools, "echo x >> output.txt; exit 1")
    assert first.is_error is True and first.retry_safe is True
    assert tools.executions == ["echo x >> output.txt; exit 1"]
    # No hidden second execution, and no cached replay path is consulted by the mapping.
    assert len(tools.executions) == 1


@pytest.mark.parametrize("mode", [TOOL_ERROR_MODE_LEGACY, TOOL_ERROR_MODE_CORRECTED])
def test_transport_failure_stays_unsafe_to_retry(mode: str) -> None:
    tools = _ScriptedTools(
        [
            SandboxCommandResult(
                ok=False,
                error=ContractError(code="transport_closed", message="transport closed"),
            )
        ],
        mode=mode,
    )
    result = _bash(tools, "echo hi")
    assert result.is_error is True
    assert result.retry_safe is False
    assert "sandbox execution failed" in result.data


@pytest.mark.parametrize("mode", [TOOL_ERROR_MODE_LEGACY, TOOL_ERROR_MODE_CORRECTED])
def test_timeout_stays_unsafe_to_retry(mode: str) -> None:
    tools = _ScriptedTools(
        [SandboxCommandResult(ok=False, exit_code=124, stderr="sandbox command timed out")],
        mode=mode,
    )
    result = _bash(tools, "sleep 999")
    assert result.is_error is True
    assert result.retry_safe is False


@pytest.mark.parametrize("mode", [TOOL_ERROR_MODE_LEGACY, TOOL_ERROR_MODE_CORRECTED])
def test_lost_sandbox_stays_unsafe_to_retry(mode: str) -> None:
    tools = _ScriptedTools([SandboxLostError("sandbox lost")], mode=mode)
    result = _bash(tools, "echo hi")
    assert result.is_error is True
    assert result.retry_safe is False
    assert "sandbox lost" in result.data
    # ``manager.mark_lost`` is invoked by the real ``_execute``; this stub raises directly.
    assert tools.executions == ["echo hi"]


def test_unknown_mode_is_rejected() -> None:
    with pytest.raises(ValueError):
        _ScriptedTools([], mode="optimistic")


def _normalized(result: ToolResult, *, side_effecting: bool) -> bool:
    """Reuse AnyCode's real inference so the legacy expectation is not hand-written."""

    from anycode.tools.executor import _normalize_result

    tool = cast(Any, SimpleNamespace(side_effecting=side_effecting))
    return bool(_normalize_result(tool, result).retry_safe)


def test_corrected_value_survives_anycode_normalization() -> None:
    """AnyCode keeps an explicit ``retry_safe``; the fix is not overwritten downstream."""

    tools = _ScriptedTools(
        [SandboxCommandResult(ok=True, exit_code=1, stderr="boom")], mode=TOOL_ERROR_MODE_CORRECTED
    )
    result = _bash(tools, "false")
    assert _normalized(result, side_effecting=True) is True


def test_m4_ledger_still_treats_completed_bash_error_as_uncertain() -> None:
    """M4 recovery semantics stay conservative: the runtime continues, the ledger does not."""

    from atlasos.ledger.bridge import _classify_result

    tools = _ScriptedTools(
        [SandboxCommandResult(ok=True, exit_code=1, stderr="boom")], mode=TOOL_ERROR_MODE_CORRECTED
    )
    result = _bash(tools, "false")
    state, kind, retry_safe = _classify_result(result, None, "digest")
    assert (state, kind, retry_safe) == ("UNCERTAIN", "EXECUTED_ERROR", False)
