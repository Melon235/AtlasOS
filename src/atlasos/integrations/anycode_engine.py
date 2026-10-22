"""Minimal AnyCode extension seam for AtlasOS-owned prompt construction.

AnyCode 0.9.0 has no public context/prompt hook: dependency outputs are injected inside the
private ``AnyCode._build_task_prompt(task, queue)`` called by ``_run_wave_task``. AtlasOS
overrides exactly that method and nothing else; scheduling, waves, lifecycle, and checkpointing
remain upstream. Stage 2 notes record the symbol and the upstream-version pin.
"""

from __future__ import annotations

from collections.abc import Callable

from anycode import AnyCode
from anycode.tasks.queue import TaskQueue
from anycode.types import OrchestratorConfig, Task

TaskPromptBuilder = Callable[[Task, TaskQueue], str]


class AtlasContextEngine(AnyCode):
    """AnyCode with a replaceable task-prompt builder."""

    def __init__(
        self,
        config: OrchestratorConfig,
        *,
        prompt_builder: TaskPromptBuilder,
    ) -> None:
        super().__init__(config)
        self._atlas_prompt_builder = prompt_builder

    def _build_task_prompt(self, task: Task, queue: TaskQueue) -> str:
        return self._atlas_prompt_builder(task, queue)
