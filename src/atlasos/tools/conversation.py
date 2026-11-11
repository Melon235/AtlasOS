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
import uuid
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
    StopReason,
    ToolUseContext,
)

from atlasos.budget import RunBudgetHook
from atlasos.tools.state import ToolStateError
from atlasos.trajectory.codec import shape_of, size_of
from atlasos.trajectory.context import (
    CallTrajectoryContext,
    current_call_trajectory,
    reset_current_call_trajectory,
    set_current_call_trajectory,
)
from atlasos.trajectory.models import (
    TrajectoryCorrelation,
    canonical_digest,
    derive_model_response_id,
)
from atlasos.trajectory.payloads import (
    EXECUTION_INTEGRITY_RECORD,
    INPUT_RECORDED,
    INPUT_UNAVAILABLE,
)

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
        if not blocks:
            return await super()._execute_tool_blocks(
                blocks, turn_span, ctx, idempotency_scope=idempotency_scope
            )

        recorder = getattr(self._atlas_tool_runtime, "trajectory_recorder", None)
        response = LAST_ASSISTANT_RESPONSE.get()
        model_response_id = self._model_response_id(response, blocks, ctx, idempotency_scope)
        projection = self._proposal_correlation(ctx, model_response_id)
        if recorder is not None:
            for index, block in enumerate(blocks):
                logical = self._atlas_tool_runtime.logical_tool_name(block.name) or block.name
                input_digest = canonical_digest(block.input)
                input_ref, input_state = self._record_payload(
                    ctx=ctx,
                    block=block,
                    logical=logical,
                    model_response_id=model_response_id,
                    ordinal=index,
                    input_digest=input_digest,
                )
                try:  # noqa: SIM105
                    recorder.record_action_proposed(
                        model_response_id=model_response_id,
                        ordinal=index,
                        call_id=block.id,
                        tool_id=logical,
                        tool_name=block.name,
                        input_digest=input_digest,
                        input_shape=shape_of(block.input),
                        input_size=size_of(block.input),
                        input_ref=input_ref,
                        input_state=input_state,
                        correlation=projection.model_copy(
                            update={
                                "model_response_id": model_response_id,
                                "ordinal": index,
                                "call_id": block.id,
                                "tool_id": logical,
                                "task_id": _context_attribute(ctx, "task_id"),
                            }
                        ),
                    )
                except Exception:  # noqa: BLE001 - evidence is optional
                    pass

        if self._suspension_coordinator is None:
            return await self._execute_with_batch_context(
                blocks,
                turn_span,
                ctx,
                idempotency_scope=idempotency_scope,
                model_response_id=model_response_id,
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
        plan = await self._suspension_coordinator.preflight_batch(
            calls,
            ctx,
            model_response_id=model_response_id,
            execution_scope=idempotency_scope,
        )
        if not plan.requires_human:
            return await self._execute_with_batch_context(
                blocks,
                turn_span,
                ctx,
                idempotency_scope=idempotency_scope,
                model_response_id=model_response_id,
            )

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
            model_response_id=model_response_id,
        )
        raise ExecutionSuspended(suspension.suspension_id, suspension.run_id)

    async def _execute_with_batch_context(
        self,
        blocks: list[Any],
        turn_span: Any,
        ctx: ToolUseContext,
        *,
        idempotency_scope: str,
        model_response_id: str,
    ) -> list[Any]:
        token = set_current_call_trajectory(
            CallTrajectoryContext(
                model_response_id=model_response_id,
                ordinal=None,
                call_id=None,
                tool_id=None,
                execution_scope=idempotency_scope,
            )
        )
        try:
            return await super()._execute_tool_blocks(
                blocks, turn_span, ctx, idempotency_scope=idempotency_scope
            )
        finally:
            reset_current_call_trajectory(token)

    async def _run_single_tool_block(  # type: ignore[override]
        self,
        block: Any,
        turn_span: Any,
        ctx: ToolUseContext,
        *,
        idempotency_key: str,
    ) -> Any:
        batch_context = current_call_trajectory()
        logical = self._atlas_tool_runtime.logical_tool_name(block.name) or block.name
        ordinal: int | None = None
        suffix = idempotency_key.rsplit(":", 1)[-1]
        if suffix.isdigit():
            ordinal = int(suffix)
        token = set_current_call_trajectory(
            CallTrajectoryContext(
                model_response_id=(batch_context.model_response_id if batch_context else None),
                ordinal=ordinal,
                call_id=block.id,
                tool_id=logical,
                execution_scope=(batch_context.execution_scope if batch_context else None),
                occurrence_id=f"execution:{idempotency_key}:{uuid.uuid4().hex}",
            )
        )
        try:
            result = await super()._run_single_tool_block(
                block, turn_span, ctx, idempotency_key=idempotency_key
            )
        finally:
            reset_current_call_trajectory(token)

        recorder = getattr(self._atlas_tool_runtime, "trajectory_recorder", None)
        if recorder is not None:
            result_block, _record = result
            content = str(result_block.content)
            try:  # noqa: SIM105
                recorder.record_tool_observation(
                    call_id=block.id,
                    tool_id=logical,
                    is_error=bool(result_block.is_error),
                    result_digest=canonical_digest(content),
                    result_size=len(content.encode("utf-8")),
                    correlation=self._proposal_correlation(
                        ctx,
                        batch_context.model_response_id if batch_context else None,
                    ).model_copy(
                        update={
                            "ordinal": ordinal,
                            "call_id": block.id,
                            "tool_id": logical,
                        }
                    ),
                )
            except Exception:  # noqa: BLE001 - evidence is optional
                pass
        return result

    def _model_response_id(
        self,
        response: LLMResponse | None,
        blocks: list[Any],
        ctx: ToolUseContext,
        execution_scope: str,
    ) -> str:
        if response is not None and getattr(response, "id", None):
            return str(response.id)
        assistant_digest = canonical_digest(
            {
                "role": "assistant",
                "content": [block.model_dump(mode="json") for block in blocks],
            }
        )
        task_id = _context_attribute(ctx, "task_id") or "<main>"
        turn = execution_scope.rsplit(":", 1)[-1] if execution_scope else "unknown"
        return derive_model_response_id(
            run_id=self._atlas_tool_runtime.run_id,
            turn_identity=f"task:{task_id}:turn:{turn}",
            assistant_response_digest=assistant_digest,
        )

    @staticmethod
    def _proposal_correlation(
        ctx: ToolUseContext, model_response_id: str | None
    ) -> TrajectoryCorrelation:
        return TrajectoryCorrelation(
            model_response_id=model_response_id,
            worker_id=_context_attribute(ctx, "worker_id"),
            profile_id=_context_attribute(ctx, "profile_id"),
            task_id=_context_attribute(ctx, "task_id"),
        )

    def _record_payload(
        self,
        *,
        ctx: ToolUseContext,
        block: Any,
        logical: str,
        model_response_id: str,
        ordinal: int,
        input_digest: str,
    ) -> tuple[str | None, str | None]:
        """Stage R1-1: keep the exact input in the private corpus, never in the transcript."""

        if getattr(self._atlas_tool_runtime, "execution_integrity_mode", None) != (
            EXECUTION_INTEGRITY_RECORD
        ):
            return None, None
        corpus = getattr(self._atlas_tool_runtime, "tool_payload_corpus", None)
        if corpus is None:
            return None, INPUT_UNAVAILABLE
        ref = corpus.record(
            call_id=block.id,
            input_digest=input_digest,
            tool_id=logical,
            payload=block.input,
            worker_id=_context_attribute(ctx, "worker_id"),
            task_id=_context_attribute(ctx, "task_id"),
            model_response_id=model_response_id,
            ordinal=ordinal,
        )
        return (ref, INPUT_RECORDED) if ref is not None else (None, INPUT_UNAVAILABLE)


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
        budget_hook: RunBudgetHook | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._conversation_store = conversation_store
        self._atlas_tool_runtime = tool_runtime
        self._budget_hook = budget_hook

    async def _execute_run(self, messages: list[LLMMessage]) -> AgentRunResult:
        result = await super()._execute_run(messages)
        return self._apply_budget_outcome(result)

    def _apply_budget_outcome(self, result: AgentRunResult) -> AgentRunResult:
        """Make a rejected LLM request a real failure instead of an empty success.

        ``AgentRunner.stream`` turns any exception raised inside the turn loop into an ``error``
        event and returns without a ``done`` event; ``Agent.run`` then falls back to an empty
        result whose ``stop_reason`` is ``None``, which reads as ``success=True``. A budget
        rejection must never be recorded as a model success, so the reason is restored here.
        """

        hook = self._budget_hook
        if hook is None:
            return result
        reason = hook.take_rejection()
        if reason is None:
            return result
        self._record_worker_stop(code="budget_exceeded", reason=reason)
        return result.model_copy(
            update={
                "success": False,
                "output": reason,
                "stop_reason": StopReason(
                    code="budget_exceeded", message=reason, recoverable=False
                ),
            }
        )

    def _record_worker_stop(self, *, code: str, reason: str) -> None:
        """Stage R1-1: a worker that never returns a result still owes a terminal event.

        The tools this worker already ran are in the trajectory, but without this transition a
        reader cannot tell "the worker is still running" from "the worker was stopped". The event
        is additive evidence and never changes the runtime result.
        """

        runtime = self._atlas_tool_runtime
        recorder = getattr(runtime, "trajectory_recorder", None)
        if recorder is None:
            return
        if getattr(runtime, "execution_integrity_mode", None) != EXECUTION_INTEGRITY_RECORD:
            return
        execution_context = self.config.execution_context
        attributes = execution_context.attributes if execution_context is not None else {}
        worker_id = attributes.get("worker_id")
        profile_id = attributes.get("profile_id")
        if worker_id is None or profile_id is None:
            return
        try:  # noqa: SIM105 - evidence must never change runtime behavior
            recorder.record_worker_lifecycle(
                worker_id=str(worker_id),
                profile_id=str(profile_id),
                parent_agent=(
                    str(attributes["parent_agent"]) if attributes.get("parent_agent") else None
                ),
                mode="stop",
                context_hash=canonical_digest(
                    {"system_prompt": self.config.system_prompt or "", "worker": str(worker_id)}
                ),
                transition_id=f"stop:{code}:{reason}",
                stop_code=code,
                stop_reason=reason,
                correlation=TrajectoryCorrelation(
                    worker_id=str(worker_id),
                    profile_id=str(profile_id),
                    task_id=(str(attributes["task_id"]) if attributes.get("task_id") else None),
                ),
            )
        except Exception:  # noqa: BLE001 - evidence is optional
            pass

    async def _get_runner(self) -> AgentRunner:
        if (
            self._runner is not None
            or self._atlas_tool_runtime is None
            or (
                getattr(self._atlas_tool_runtime, "suspension_coordinator", None) is None
                and getattr(self._atlas_tool_runtime, "trajectory_recorder", None) is None
            )
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
