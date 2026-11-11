"""Thin dispatch wrapper: Atlas routes, AnyCode executes.

The wrapper intercepts only ``dispatch_action``. Everything else is delegated unchanged to
AnyCode's ``ToolExecutor``, preserving validation, security policy, idempotency, tracing, and
provider-facing result semantics.

Stage G3-2 adds one optional post-execution step: after the inner chain (M3 gate, M4 bridge,
AnyCode executor) returned the real ``ToolResult``, an ingress offloader may replace its
``data`` with a preview plus reference. This is the outermost AtlasOS-owned boundary before
AnyCode turns the result into a ``ToolResultBlock``.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from anycode.tools.executor import ToolExecutor
from anycode.types import BatchToolCall, ToolResult, ToolUseContext
from pydantic import BaseModel, Field, ValidationError

from atlasos.tool_result_offload import task_id_of
from atlasos.tools.catalog import CORE_TOOL_DISPATCH

if TYPE_CHECKING:
    from atlasos.tool_result_offload import ToolResultIngressRuntime


class DispatchActionInput(BaseModel):
    """Stable core-tool schema exposed to the model."""

    tool_id: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolSearchInput(BaseModel):
    query: str = Field(min_length=1)


class AtlasDispatchExecutor:
    """Route ``dispatch_action`` after disclosure checks; delegate real tools to AnyCode."""

    def __init__(
        self,
        inner: ToolExecutor,
        runtime: Any,
        *,
        offloader: ToolResultIngressRuntime | None = None,
    ) -> None:
        self._inner = inner
        self._runtime = runtime
        self._offloader = offloader

    async def execute(
        self,
        tool_name: str,
        raw_input: dict[str, Any],
        context: ToolUseContext,
        *,
        idempotency_key: str | None = None,
    ) -> ToolResult:
        logical_name = self._runtime.logical_tool_name(tool_name) or tool_name
        if logical_name != CORE_TOOL_DISPATCH:
            result = await self._inner.execute(
                logical_name,
                raw_input,
                context,
                idempotency_key=idempotency_key,
            )
            return await self._offload(logical_name, result, context, idempotency_key)

        try:
            request = DispatchActionInput.model_validate(raw_input)
        except ValidationError as error:
            return ToolResult(
                data=f"invalid_dispatch: {error}",
                is_error=True,
            )
        result = await self._runtime.dispatch(
            request.tool_id,
            request.arguments,
            context,
            idempotency_key=idempotency_key,
        )
        return await self._offload(request.tool_id, result, context, idempotency_key)

    async def _offload(
        self,
        tool_id: str,
        result: ToolResult,
        context: ToolUseContext,
        idempotency_key: str | None,
    ) -> ToolResult:
        """Stage G3-2 ingress boundary: runs after M3/M4/M7, before the conversation block."""

        if self._offloader is None:
            return result
        return await self._offloader.offload_async(
            result,
            tool_name=tool_id,
            task_id=task_id_of(context),
            call_key=idempotency_key,
        )

    async def execute_batch(
        self,
        calls: list[BatchToolCall],
        context: ToolUseContext,
        *,
        idempotency_keys: dict[str, str] | None = None,
    ) -> dict[str, ToolResult]:
        async def run(call: BatchToolCall) -> tuple[str, ToolResult]:
            result = await self.execute(
                call.name,
                call.input,
                context,
                idempotency_key=(idempotency_keys or {}).get(call.id, call.id),
            )
            return call.id, result

        pairs = await asyncio.gather(*(run(call) for call in calls))
        return dict(pairs)
