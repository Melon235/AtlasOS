"""Minimal AnyCode extension seams for AtlasOS-owned prompt and tool surfaces.

AnyCode 0.9.0 has no public context/prompt hook: dependency outputs are injected inside the
private ``AnyCode._build_task_prompt(task, queue)`` called by ``_run_wave_task``. AtlasOS
overrides exactly that method. For V2 M1 it also overrides ``build_agent`` so every agent uses
one shared Atlas ``ToolRegistry`` and ``AtlasDispatchExecutor``; scheduling, waves, lifecycle,
checkpointing, and real tool execution remain upstream.

Stage G2a adds one optional seam: a run-scoped ``RunBudgetState`` becomes a ``TurnHook`` on every
agent, so each LLM request is admitted against the budget shared by the whole run. Without a state
the hook is absent and behaviour is unchanged.

Stage G2b-1 adds a second optional seam: a run-scoped ``HistoryCompactionRuntime`` contributes a
``ToolResultCompactionHook`` per worker. Hook order is deliberate and load-bearing (``HookRunner``
applies hooks in registration order, and ``AgentRunner`` keeps the returned list as the
conversation):

1. compaction — rewrites old tool results into references;
2. conversation snapshot (M6) — persists the conversation the model will actually see;
3. budget admission — estimates the compacted context.

``AgentConfig.context_policy`` stays ``None``, so ``AgentRunner._context_manager`` is ``None`` and
no later assembly step can change the messages between the hooks and the provider request.
"""

from __future__ import annotations

import json
from collections.abc import Callable

from anycode import AnyCode
from anycode.core.agent import Agent
from anycode.tasks.queue import TaskQueue
from anycode.types import AgentConfig, OrchestratorConfig, Task
from pydantic import BaseModel

from atlasos.budget import RunBudgetHook, RunBudgetState
from atlasos.history_compaction import HistoryCompactionRuntime, ToolResultCompactionHook
from atlasos.tools.conversation import AtlasToolAgent
from atlasos.tools.runtime import AtlasToolRuntime

TaskPromptBuilder = Callable[[Task, TaskQueue], str]


def _trusted_task_id(config: AgentConfig) -> str:
    """Read the worker task id from the trusted execution context, never from prompt text."""

    execution = config.execution_context
    attributes = getattr(execution, "attributes", None) if execution is not None else None
    value = attributes.get("task_id") if isinstance(attributes, dict) else None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            "history compaction requires a trusted task id on the agent execution context"
        )
    return value


class AtlasContextEngine(AnyCode):
    """AnyCode with replaceable task-prompt construction and an optional shared tool runtime."""

    def __init__(
        self,
        config: OrchestratorConfig,
        *,
        prompt_builder: TaskPromptBuilder | None = None,
        tool_runtime: AtlasToolRuntime | None = None,
        budget_state: RunBudgetState | None = None,
        compaction: HistoryCompactionRuntime | None = None,
    ) -> None:
        if (budget_state is not None or compaction is not None) and tool_runtime is None:
            raise ValueError(
                "budget_state/compaction require the M1 tool runtime seam: every worker must "
                "share one agent construction path so the hook is attached to all LLM requests"
            )
        super().__init__(config)
        self._atlas_prompt_builder = prompt_builder
        self._tool_runtime = tool_runtime
        self._budget_state = budget_state
        self._compaction = compaction

    def _build_task_prompt(self, task: Task, queue: TaskQueue) -> str:
        if self._atlas_prompt_builder is None:
            return super()._build_task_prompt(task, queue)
        return self._atlas_prompt_builder(task, queue)

    def build_agent(
        self,
        config: AgentConfig | dict[str, object],
        *,
        output_schema: type[BaseModel] | None = None,
    ) -> Agent:
        """Build an agent over the one shared tool registry when M1 is enabled."""

        if self._tool_runtime is None:
            return super().build_agent(config, output_schema=output_schema)

        typed = AgentConfig.model_validate(config) if isinstance(config, dict) else config
        if typed.provider_resilience is None and self._config.provider_resilience is not None:
            typed = typed.model_copy(
                update={"provider_resilience": self._config.provider_resilience}
            )
        # Native surface is fixed for the run: only catalog core tools are visible. MCP and
        # other external tools are registered in the same registry but filtered out here.
        typed = typed.model_copy(update={"tools": list(self._tool_runtime.agent_tools)})
        hooks = [*(self._hooks or [])]
        compaction_hook: ToolResultCompactionHook | None = None
        if self._compaction is not None:
            # Before the snapshot hook: M6 must persist the compacted conversation.
            compaction_hook = self._compaction.hook_for(task_id=_trusted_task_id(typed))
            hooks.append(compaction_hook)
        hooks.append(self._tool_runtime.conversation_hook())
        budget_hook: RunBudgetHook | None = None
        if self._budget_state is not None:
            # Appended last so the estimate sees the prompt the runner will actually send.
            budget_hook = RunBudgetHook(
                self._budget_state,
                worker=typed.name,
                model=typed.model,
                system_prompt=typed.system_prompt,
                tool_definitions=self._active_tool_definitions_text(typed),
                completion_reserve=typed.max_tokens,
            )
            hooks.append(budget_hook)
        return AtlasToolAgent(
            typed,
            self._tool_runtime.registry,
            self._tool_runtime.executor,  # type: ignore[arg-type]
            tracer=self._tracer,
            guardrail_config=self._guardrail_config,
            hooks=hooks,
            output_validators=self._output_validators,
            output_schema=output_schema,
            conversation_store=self._tool_runtime.conversation_store,
            tool_runtime=self._tool_runtime,
            budget_hook=budget_hook,
        )

    def _active_tool_definitions_text(self, typed: AgentConfig) -> str | None:
        """Serialize the native tool surface once, for budget estimation only.

        ``AgentRunner`` sends exactly the registry definitions allowed by ``config.tools``; the
        same filter is applied here so the prompt estimate includes the schemas the provider
        actually bills. This is estimation input only — the runner still owns the real tool list.
        """

        if self._tool_runtime is None:
            return None
        allowed = set(typed.tools or ())
        definitions = [
            definition
            for definition in self._tool_runtime.registry.to_tool_defs()
            if definition.name in allowed
        ]
        if not definitions:
            return None
        return json.dumps(
            [
                {
                    "name": definition.name,
                    "description": definition.description,
                    "input_schema": definition.input_schema,
                }
                for definition in definitions
            ],
            default=str,
            sort_keys=True,
        )
