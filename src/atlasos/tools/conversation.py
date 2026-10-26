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
from anycode.types import AgentInfo, AgentRunResult, LLMMessage, LLMResponse

from atlasos.tools.state import ToolStateError

CONVERSATION_SCHEMA_VERSION = 1
CURRENT_TASK_ID: ContextVar[str | None] = contextvars.ContextVar(
    "atlas_current_task_id", default=None
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
        return response


class AtlasToolAgent(Agent):
    """Agent that resumes an in-flight task's turn-boundary conversation."""

    def __init__(
        self,
        *args: Any,
        conversation_store: ConversationStore,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._conversation_store = conversation_store

    async def run(self, prompt: str) -> AgentRunResult:
        task_id = CURRENT_TASK_ID.get()
        if task_id is not None:
            restored = self._conversation_store.load(task_id)
            if restored:
                messages = [LLMMessage.model_validate(message) for message in restored]
                return await self._execute_run(messages)
        return await super().run(prompt)
