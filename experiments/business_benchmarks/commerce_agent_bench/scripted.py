"""Scripted provider for the B0-R2 smoke test.

It never calls a model. It walks the exact tool sequence a competent worker would: list the thread,
read two messages, write both deliverables, then finish. Used to prove the runtime chain
(AtlasToolRuntime → M3 → native mock API → deliverable → native grader) without model tokens.
"""

from __future__ import annotations

import json
from typing import Any

from anycode.types import LLMResponse, StreamEvent, TextBlock, TokenUsage, ToolUseBlock

_DELIVERABLE = {
    "primary_supplier_name": "EverTherm",
    "primary_supplier_landed_cost_usd": 3.36,
    "primary_supplier_fx_rate_rmb_per_usd": None,
    "primary_supplier_fx_handling_load_pct": None,
    "primary_supplier_extra_logistics_usd_per_pc": 0.0,
    "primary_supplier_cert_authorization_letter_required": False,
    "backup_supplier_name": "AtlasWare",
    "backup_supplier_landed_cost_usd": 3.376,
    "rejected_late_arrival_supplier_name": "CupWell",
    "rejected_quality_risk_supplier_name": "MetroMug",
    "cost_adjustment_note": (
        "NorthPeak looks cheap before accessorial charges; after adjustment it is not."
    ),
    "cost_adjusted_landed_cost_usd": 3.52,
    "steelpro_latest_unit_price_usd": 3.36,
    "steelpro_lfgb_certificate_covers_target_750ml": False,
    "landed_cost_budget_usd": 3.40,
}


class ScriptedBusinessProvider:
    """Deterministic stand-in for the model, driving the real tool runtime."""

    name = "scripted-business"

    def __init__(self) -> None:
        self.calls = 0

    def _blocks(self, index: int) -> list[Any]:
        if index == 0:
            return [
                ToolUseBlock(
                    id="script-1",
                    name="slack_list_messages",
                    input={"channel": "#rfq-stainless-bottle", "max_results": 30},
                )
            ]
        if index == 1:
            return [
                ToolUseBlock(
                    id="script-2",
                    name="read_task_file",
                    input={"path": "api_reference.md"},
                )
            ]
        if index == 2:
            return [
                ToolUseBlock(
                    id="script-3",
                    name="write_deliverable",
                    input={
                        "path": "outputs/answer.json",
                        "content": json.dumps(_DELIVERABLE, indent=2),
                    },
                )
            ]
        if index == 3:
            return [
                ToolUseBlock(
                    id="script-4",
                    name="write_deliverable",
                    input={
                        "path": "outputs/quote_consolidation.md",
                        "content": "# Supplier consolidation\n\nScripted smoke deliverable.\n",
                    },
                )
            ]
        return [TextBlock(text="Scripted run complete.")]

    def _response(self, messages: list[Any], options: Any) -> LLMResponse:
        index = self.calls
        self.calls += 1
        blocks = self._blocks(index)
        return LLMResponse(
            id=f"scripted-{index}",
            content=blocks,
            model=getattr(options, "model", "scripted"),
            stop_reason="tool_use"
            if any(isinstance(b, ToolUseBlock) for b in blocks)
            else "end_turn",
            usage=TokenUsage(input_tokens=1, output_tokens=1),
        )

    async def chat(self, messages: list[Any], options: Any) -> LLMResponse:
        return self._response(messages, options)

    def stream(self, messages: list[Any], options: Any):
        async def _gen():
            response = self._response(messages, options)
            for block in response.content:
                if isinstance(block, ToolUseBlock):
                    yield StreamEvent(type="tool_use", data=block)
                elif isinstance(block, TextBlock):
                    yield StreamEvent(type="text", data=block.text)
            yield StreamEvent(type="done", data=response)

        return _gen()


__all__ = ["ScriptedBusinessProvider"]
