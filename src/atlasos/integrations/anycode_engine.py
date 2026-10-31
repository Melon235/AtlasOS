"""Minimal AnyCode extension seams for AtlasOS-owned prompt and tool surfaces.

AnyCode 0.9.0 has no public context/prompt hook: dependency outputs are injected inside the
private ``AnyCode._build_task_prompt(task, queue)`` called by ``_run_wave_task``. AtlasOS
overrides exactly that method. For V2 M1 it also overrides ``build_agent`` so every agent uses
one shared Atlas ``ToolRegistry`` and ``AtlasDispatchExecutor``; scheduling, waves, lifecycle,
checkpointing, and real tool execution remain upstream.
"""

from __future__ import annotations

from collections.abc import Callable

from anycode import AnyCode
from anycode.core.agent import Agent
from anycode.tasks.queue import TaskQueue
from anycode.types import AgentConfig, OrchestratorConfig, Task
from pydantic import BaseModel

from atlasos.tools.conversation import AtlasToolAgent
from atlasos.tools.runtime import AtlasToolRuntime

TaskPromptBuilder = Callable[[Task, TaskQueue], str]


class AtlasContextEngine(AnyCode):
    """AnyCode with replaceable task-prompt construction and an optional shared tool runtime."""

    def __init__(
        self,
        config: OrchestratorConfig,
        *,
        prompt_builder: TaskPromptBuilder | None = None,
        tool_runtime: AtlasToolRuntime | None = None,
    ) -> None:
        super().__init__(config)
        self._atlas_prompt_builder = prompt_builder
        self._tool_runtime = tool_runtime

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
        hooks = [*(self._hooks or []), self._tool_runtime.conversation_hook()]
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
        )
