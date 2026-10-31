"""Task conversation snapshots for in-flight tool-loop recovery.

AnyCode task checkpoints store task outcomes, not the in-flight agent conversation. This sidecar
stores the turn-boundary conversation for each task so a resumed in-flight task sees the same
tool.search result that the runtime already treats as disclosed. It is durable Atlas session
metadata, not a replacement checkpoint engine.
"""

from __future__ import annotations

import contextvars
import json
import os
from contextvars import ContextVar, Token
from pathlib import Path
from typing import Any

from anycode.core.agent import Agent
from anycode.core.runner import AgentRunner
from anycode.types import (
    AgentInfo,
    AgentRunResult,
    LLMMessage,
    LLMResponse,
    RunnerOptions,
    ToolUseContext,
)

from atlasos.tools.state import ToolStateError

CONVERSATION_SCHEMA_VERSION = 1
CURRENT_TASK_ID: ContextVar[str | None] = contextvars.ContextVar(
    "atlas_current_task_id", default=None
)
LAST_ASSISTANT_RESPONSE: ContextVar[LLMResponse | None] = contextvars.ContextVar(
    "atlas_last_assistant_response", default=None
)


def set_current_task_id(task_id: str | None) -> Token[str | None]:
    return CURRENT_TASK_ID.set(task_id)


def reset_current_task_id(token: Token[str | None]) -> None:
    CURRENT_TASK_ID.reset(token)


class ConversationStore:
    """Atomic per-run task conversation snapshots."""

    def __init__(self, path: str | Path | None, *, run_id: str) -> None:
        self.path = Path(path) if path is not None else None
        self.run_id = run_id

    def _load_payload(self) -> dict[str, Any]:
        if self.path is None or not self.path.exists():
            return {
                "schema_version": CONVERSATION_SCHEMA_VERSION,
                "run_id": self.run_id,
                "tasks": {},
            }
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ToolStateError(f"cannot read conversation state {self.path}: {error}") from error
        if not isinstance(payload, dict):
            raise ToolStateError(f"conversation state {self.path} must be a JSON object")
        if payload.get("schema_version") != CONVERSATION_SCHEMA_VERSION:
            raise ToolStateError(
                "unsupported conversation state schema "
                f"{payload.get('schema_version')!r}; expected {CONVERSATION_SCHEMA_VERSION}"
            )
        if payload.get("run_id") not in (None, self.run_id):
            raise ToolStateError(
                f"conversation state belongs to run {payload.get('run_id')!r}, not {self.run_id!r}"
            )
        return payload

    def load(self, task_id: str) -> list[dict[str, Any]] | None:
        tasks = self._load_payload().get("tasks")
        if not isinstance(tasks, dict):
            return None
        entry = tasks.get(task_id)
        if not isinstance(entry, dict):
            return None
        messages = entry.get("messages")
        if not isinstance(messages, list):
            return None
        return messages

    def save(self, task_id: str, messages: list[LLMMessage]) -> None:
        if self.path is None:
            return
        payload = self._load_payload()
        payload["tasks"][task_id] = {
            "messages": [message.model_dump(mode="json") for message in messages],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        os.replace(temporary, self.path)


class ConversationSnapshotHook:
    """Save the conversation before each model turn, keyed by the current Atlas task."""

    def __init__(self, store: ConversationStore) -> None:
        self._store = store

    async def before_turn(
        self,
        messages: list[LLMMessage],
        context: AgentInfo,
    ) -> list[LLMMessage]:
        del context
        task_id = CURRENT_TASK_ID.get()
        if task_id is not None:
            self._store.save(task_id, messages)
        return messages

    async def after_turn(self, response: LLMResponse, context: AgentInfo) -> LLMResponse:
        del context
        LAST_ASSISTANT_RESPONSE.set(response)
        return response


class AtlasAgentRunner(AgentRunner):
    """AgentRunner with the M6 whole-response preflight barrier.

    AnyCode executes a model response's tool blocks concurrently and has no batch hook. This
    subclass adds exactly one seam immediately before that execution: evaluate every call in the
    response, then either continue the unchanged upstream path or durably suspend the entire
    response before any call can reach M4.
    """

    def __init__(
        self,
        *args: Any,
        tool_runtime: Any,
        suspension_coordinator: Any,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._atlas_tool_runtime = tool_runtime
        self._suspension_coordinator = suspension_coordinator

    async def _execute_tool_blocks(  # type: ignore[override]
        self,
        blocks: list[Any],
        turn_span: Any,
        ctx: ToolUseContext,
        *,
        idempotency_scope: str,
    ) -> list[Any]:
        if self._suspension_coordinator is None or not blocks:
            return await super()._execute_tool_blocks(
                blocks, turn_span, ctx, idempotency_scope=idempotency_scope
            )
        from atlasos.suspension.batch import BatchCall, ExecutionSuspended

        calls = [
            BatchCall(
                ordinal=index,
                call_id=block.id,
                tool_name=block.name,
                raw_input=dict(block.input),
                execution_key=f"{idempotency_scope}:{index}",
            )
            for index, block in enumerate(blocks)
        ]
        plan = await self._suspension_coordinator.preflight_batch(calls, ctx)
        if not plan.requires_human:
            return await super()._execute_tool_blocks(
                blocks, turn_span, ctx, idempotency_scope=idempotency_scope
            )

        response = LAST_ASSISTANT_RESPONSE.get()
        if response is None:
            raise RuntimeError("M6 preflight refused: assistant response was not captured")
        task_id = _context_attribute(ctx, "task_id")
        sandbox_digest = self._atlas_tool_runtime.sandbox_binding_digest(task_id)
        sandbox_required = self._atlas_tool_runtime.sandbox_required(task_id)
        suspension = await self._suspension_coordinator.suspend(
            plan,
            ctx,
            assistant_message=LLMMessage(role="assistant", content=response.content),
            execution_scope=idempotency_scope,
            sandbox_binding_digest=sandbox_digest,
            sandbox_required=sandbox_required,
        )
        raise ExecutionSuspended(suspension.suspension_id, suspension.run_id)


def _context_attribute(context: ToolUseContext, key: str) -> str | None:
    execution_context = context.execution_context
    if execution_context is None:
        return None
    value = execution_context.attributes.get(key)
    if value is None:
        nested = execution_context.attributes.get("atlas_worker")
        if isinstance(nested, dict):
            value = nested.get(key)
    return str(value) if value is not None else None


class AtlasToolAgent(Agent):
    """Agent that resumes an in-flight task's turn-boundary conversation."""

    def __init__(
        self,
        *args: Any,
        conversation_store: ConversationStore,
        tool_runtime: Any | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._conversation_store = conversation_store
        self._atlas_tool_runtime = tool_runtime

    async def _get_runner(self) -> AgentRunner:
        if (
            self._runner is not None
            or self._atlas_tool_runtime is None
            or getattr(self._atlas_tool_runtime, "suspension_coordinator", None) is None
        ):
            return await super()._get_runner()
        from anycode.constants import AGENT_ROLE_MAX_LENGTH
        from anycode.providers.adapter import create_adapter

        provider = self.config.provider or "anthropic"
        adapter = await create_adapter(provider, resilience=self.config.provider_resilience)
        options = RunnerOptions(
            model=self.config.model,
            system_prompt=self.config.system_prompt,
            max_turns=self.config.max_turns,
            max_tokens=self.config.max_tokens,
            temperature=self.config.temperature,
            allowed_tools=self.config.tools,
            agent_name=self.name,
            agent_role=(self.config.system_prompt or "assistant")[:AGENT_ROLE_MAX_LENGTH],
            verification=self.config.verification,
            tool_security=self.config.tool_security,
            execution_context=self.config.execution_context,
        )
        self._runner = AtlasAgentRunner(
            adapter,
            self._registry,
            self._executor,
            options,
            tool_runtime=self._atlas_tool_runtime,
            suspension_coordinator=self._atlas_tool_runtime.suspension_coordinator,
            tracer=self._tracer,
            guardrail_config=self._guardrail_config,
            hooks=self._hooks,
            output_validators=self._output_validators,
            output_schema=self._output_schema,
            context_policy=self.config.context_policy,
        )
        return self._runner

    async def run(self, prompt: str) -> AgentRunResult:
        task_id = CURRENT_TASK_ID.get()
        if task_id is not None:
            restored = self._conversation_store.load(task_id)
            if restored:
                messages = [LLMMessage.model_validate(message) for message in restored]
                return await self._execute_run(messages)
        return await super().run(prompt)
