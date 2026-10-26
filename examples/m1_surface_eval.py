"""M1 native tool-surface footprint and provider-cache microbenchmark.

Baseline A exposes every external tool in native tools[]. Atlas B exposes only core tools and
uses tool.search/dispatch_action. Offline records are deterministic schema measurements. Live
records are optional (``ATLASOS_RUN_LIVE_TESTS=1``) and use the existing DeepSeek adapter; the
provider cache is warm-prone and uncontrolled.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from anycode.types import LLMChatOptions, LLMMessage, TextBlock, ToolDefinition, ToolResult
from pydantic import BaseModel, ConfigDict

from atlasos.experimental.tool_search_eval import TOOL_CATALOG
from atlasos.experiments import ExperimentStore, git_commit, git_dirty
from atlasos.integrations.deepseek import DeepSeekAdapter
from atlasos.metrics import InferenceObservation
from atlasos.tools import AtlasToolRuntime

REPO_ROOT = Path(__file__).resolve().parents[1]
STORE = REPO_ROOT / "experiments" / "runs" / "v2-m1-surface.jsonl"
MODEL = "deepseek-chat"
REPETITIONS = 3


class QueryInput(BaseModel):
    query: str = ""


def _external_tools() -> tuple[ToolDefinition, ...]:
    tools: list[ToolDefinition] = []
    for tool_id, description in TOOL_CATALOG:

        async def execute(
            validated: QueryInput,
            context: Any,
            _tool_id: str = tool_id,
        ) -> ToolResult:
            del context
            return ToolResult(data=f"{_tool_id}:{validated.query}", is_error=False)

        tools.append(
            ToolDefinition(
                name=tool_id,
                description=description,
                input_model=QueryInput,
                execute=execute,
                side_effecting=False,
            )
        )
    return tuple(tools)


class SurfaceRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    experiment: str
    variant: str
    mode: str
    native_tool_count: int
    native_schema_chars: int
    native_surface_hash: str
    stable_after_search: bool | None = None
    full_tool_count: int | None = None
    full_schema_chars: int | None = None
    schema_reduction: float | None = None

    repetition: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cache_hit_tokens: int | None = None
    cache_miss_tokens: int | None = None
    provider_latency_ms: float | None = None

    model: str
    provider: str
    git_commit: str | None
    git_dirty: bool
    timestamp: datetime
    success: bool


def _offline_records(runtime: AtlasToolRuntime, *, stable: bool) -> list[SurfaceRecord]:
    native_defs = runtime.native_tool_defs()
    full_defs = runtime.registry.to_tool_defs()
    before_hash = runtime.native_surface_hash()
    native_chars = runtime.native_schema_chars()
    full_chars = len(
        json.dumps(
            [
                {
                    "name": definition.name,
                    "description": definition.description,
                    "input_schema": definition.input_schema,
                }
                for definition in full_defs
            ],
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    records = [
        SurfaceRecord(
            experiment="M1-surface",
            variant="atlas_core",
            mode="offline",
            native_tool_count=len(native_defs),
            native_schema_chars=native_chars,
            native_surface_hash=before_hash,
            stable_after_search=stable,
            full_tool_count=len(full_defs),
            full_schema_chars=full_chars,
            schema_reduction=(full_chars - native_chars) / full_chars if full_chars else None,
            model="deterministic",
            provider="none",
            git_commit=git_commit(REPO_ROOT),
            git_dirty=git_dirty(REPO_ROOT),
            timestamp=datetime.now(UTC),
            success=True,
        )
    ]
    return records


async def _live_records(runtime: AtlasToolRuntime) -> list[SurfaceRecord]:
    observations: list[InferenceObservation] = []
    adapter = DeepSeekAdapter.from_env(on_call=observations.append)
    variants = [
        ("full_exposure", runtime.registry.to_tool_defs()),
        ("atlas_core", runtime.native_tool_defs()),
    ]
    records: list[SurfaceRecord] = []
    for variant, tools in variants:
        for repetition in range(1, REPETITIONS + 1):
            before = len(observations)
            response = await adapter.chat(
                [LLMMessage(role="user", content=[TextBlock(text="Reply with OK.")])],
                LLMChatOptions(
                    model=MODEL,
                    system_prompt=(
                        "You are a tool-using agent. Reply with OK only. Do not call tools."
                    ),
                    tools=list(tools),
                    max_tokens=5,
                    temperature=0,
                ),
            )
            if len(observations) == before:
                raise RuntimeError("provider call produced no usage observation")
            usage = observations[-1].usage
            records.append(
                SurfaceRecord(
                    experiment="M1-surface",
                    variant=variant,
                    mode="live",
                    native_tool_count=len(tools),
                    native_schema_chars=runtime.native_schema_chars()
                    if variant == "atlas_core"
                    else 0,
                    native_surface_hash=runtime.native_surface_hash()
                    if variant == "atlas_core"
                    else "",
                    repetition=repetition,
                    prompt_tokens=usage.prompt_tokens,
                    completion_tokens=usage.completion_tokens,
                    cache_hit_tokens=usage.cache_hit_tokens,
                    cache_miss_tokens=usage.cache_miss_tokens,
                    provider_latency_ms=usage.latency_ms,
                    model=MODEL,
                    provider="deepseek",
                    git_commit=git_commit(REPO_ROOT),
                    git_dirty=git_dirty(REPO_ROOT),
                    timestamp=datetime.now(UTC),
                    success=bool(response.content),
                )
            )
            print(
                f"surface {variant} rep={repetition} tools={len(tools)} "
                f"prompt={usage.prompt_tokens} hit={usage.cache_hit_tokens} "
                f"miss={usage.cache_miss_tokens} latency={usage.latency_ms:.0f}"
            )
    return records


async def main() -> None:
    dry_run = os.environ.get("ATLASOS_M1_DRY_RUN") == "1"
    if git_dirty(REPO_ROOT) and not dry_run:
        raise SystemExit("M1 formal records require a clean worktree; commit first")

    runtime = AtlasToolRuntime("surface-eval", external_tools=_external_tools())
    before_hash = runtime.native_surface_hash()
    search_result = await runtime.search("internal policy")
    stable = before_hash == runtime.native_surface_hash() and search_result.is_error is False
    records = _offline_records(runtime, stable=stable)

    if os.environ.get("ATLASOS_RUN_LIVE_TESTS") == "1":
        records.extend(await _live_records(runtime))

    for record in records:
        if record.mode == "offline":
            print(
                f"offline: native={record.native_tool_count} tools/{record.native_schema_chars} "
                f"chars full={record.full_tool_count} tools/{record.full_schema_chars} chars "
                f"reduction={record.schema_reduction:.1%} stable={record.stable_after_search}"
            )

    if not dry_run:
        store = ExperimentStore(STORE)
        for record in records:
            store.append(record)


if __name__ == "__main__":
    asyncio.run(main())
