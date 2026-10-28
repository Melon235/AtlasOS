"""Thin dispatch wrapper: Atlas routes, AnyCode executes.

The wrapper intercepts only ``dispatch_action``. Everything else is delegated unchanged to
AnyCode's ``ToolExecutor``, preserving validation, security policy, idempotency, tracing, and
provider-facing result semantics.
"""

from __future__ import annotations

import asyncio
from typing import Any

from anycode.tools.executor import ToolExecutor
from anycode.types import BatchToolCall, ToolResult, ToolUseContext
from pydantic import BaseModel, Field, ValidationError

from atlasos.tools.catalog import CORE_TOOL_DISPATCH


class DispatchActionInput(BaseModel):
    """Stable core-tool schema exposed to the model."""

    tool_id: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolSearchInput(BaseModel):
    query: str = Field(min_length=1)


class AtlasDispatchExecutor:
    """Route ``dispatch_action`` after disclosure checks; delegate real tools to AnyCode."""

    def __init__(self, inner: ToolExecutor, runtime: Any) -> None:
        self._inner = inner
        self._runtime = runtime

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
            return await self._inner.execute(
                logical_name,
                raw_input,
                context,
                idempotency_key=idempotency_key,
            )

        try:
            request = DispatchActionInput.model_validate(raw_input)
        except ValidationError as error:
            return ToolResult(
                data=f"invalid_dispatch: {error}",
                is_error=True,
            )
        return await self._runtime.dispatch(
            request.tool_id,
            request.arguments,
            context,
            idempotency_key=idempotency_key,
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
