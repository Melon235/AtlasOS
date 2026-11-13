"""Final-4 R-10: a neutral execution backend for the native AnyCode baseline.

The Final-3 blocker was that native AnyCode runs on the Windows host while Atlas runs in a sandbox.
This module closes the *wiring* half of the gap with no Atlas mechanism and no AnyCode change:

* :class:`LocalProcessBackend` executes the six tools against one isolated directory through a
  subprocess (the same shape a container backend would have).
* :func:`neutral_tool_definitions` builds the six ``ToolDefinition`` objects on top of it.
* :class:`NeutralEngine` is an ``AnyCode`` subclass that only swaps the registry/executor pair; the
  agent loop, scheduler, team machinery and turn hooks stay upstream.

Container parity remains ``ENVIRONMENT_BLOCKED``: this workspace has no sandbox credential, so
a real ``opensandbox`` instance cannot be created (``SandboxCredentialError``). Proven here:
that the native path accepts externally-executed tools, which is exactly what a container backend
would supply.
"""

from __future__ import annotations

import asyncio
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from anycode import AnyCode
from anycode.core.agent import Agent
from anycode.tools.executor import ToolExecutor
from anycode.tools.registry import ToolRegistry
from anycode.types import AgentConfig, ToolDefinition, ToolResult, ToolUseContext
from pydantic import BaseModel

NEUTRAL_TOOLS: tuple[str, ...] = (
    "bash",
    "file_read",
    "file_write",
    "file_edit",
    "grep",
    "list_files",
)

_MAX_OUTPUT = 20_000


class _BashInput(BaseModel):
    command: str = ""


class _PathInput(BaseModel):
    path: str = ""


class _WriteInput(BaseModel):
    path: str = ""
    content: str = ""


class _EditInput(BaseModel):
    path: str = ""
    old: str = ""
    new: str = ""


class _GrepInput(BaseModel):
    pattern: str = ""
    path: str = "."


@dataclass
class LocalProcessBackend:
    """One isolated directory plus a subprocess shell; no Atlas state, no binding store."""

    root: Path
    timeout_seconds: float = 60.0
    operations: int = 0
    timings_ms: list[float] = field(default_factory=list)

    def _resolve(self, raw: str) -> Path:
        candidate = (self.root / raw).resolve() if not Path(raw).is_absolute() else Path(raw)
        if self.root.resolve() not in candidate.parents and candidate != self.root.resolve():
            raise ValueError(f"path escapes the workspace: {raw}")
        return candidate

    def _record(self, started: float) -> None:
        self.operations += 1
        self.timings_ms.append((time.perf_counter() - started) * 1000.0)

    def read(self, path: str) -> str:
        return self._resolve(path).read_text(encoding="utf-8", errors="replace")

    def write(self, path: str, content: str) -> int:
        target = self._resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return len(content)

    def edit(self, path: str, old: str, new: str) -> int:
        target = self._resolve(path)
        text = target.read_text(encoding="utf-8")
        if old not in text:
            raise ValueError("old text not found")
        target.write_text(text.replace(old, new, 1), encoding="utf-8")
        return 1

    def list_dir(self, path: str = ".") -> list[str]:
        base = self._resolve(path)
        return sorted(entry.name for entry in base.iterdir())

    async def run(self, command: str) -> tuple[int, str]:
        process = await asyncio.create_subprocess_shell(
            command,
            cwd=str(self.root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=self.timeout_seconds)
        except TimeoutError:
            process.kill()
            await process.wait()
            return 124, "command timed out"
        text = stdout.decode("utf-8", errors="replace")[:_MAX_OUTPUT]
        return process.returncode or 0, text


def neutral_tool_definitions(backend: LocalProcessBackend) -> tuple[ToolDefinition, ...]:
    """The six logical tools, executed by the backend instead of the host CWD."""

    async def bash(validated: _BashInput, context: ToolUseContext) -> ToolResult:
        del context
        started = time.perf_counter()
        code, output = await backend.run(validated.command)
        backend._record(started)
        return ToolResult(data=output, is_error=code != 0)

    async def file_read(validated: _PathInput, context: ToolUseContext) -> ToolResult:
        del context
        started = time.perf_counter()
        try:
            text = backend.read(validated.path)
        except Exception as error:  # noqa: BLE001 - reported as a tool error
            return ToolResult(data=f"{type(error).__name__}: {error}", is_error=True)
        backend._record(started)
        return ToolResult(data=text[:_MAX_OUTPUT], is_error=False)

    async def file_write(validated: _WriteInput, context: ToolUseContext) -> ToolResult:
        del context
        started = time.perf_counter()
        written = backend.write(validated.path, validated.content)
        backend._record(started)
        return ToolResult(data=f"wrote {written} chars to {validated.path}", is_error=False)

    async def file_edit(validated: _EditInput, context: ToolUseContext) -> ToolResult:
        del context
        started = time.perf_counter()
        try:
            backend.edit(validated.path, validated.old, validated.new)
        except Exception as error:  # noqa: BLE001
            return ToolResult(data=f"{type(error).__name__}: {error}", is_error=True)
        backend._record(started)
        return ToolResult(data=f"edited {validated.path}", is_error=False)

    async def grep(validated: _GrepInput, context: ToolUseContext) -> ToolResult:
        del context
        started = time.perf_counter()
        code, output = await backend.run(
            f"grep -rn -- {validated.pattern!r} {validated.path!r} || true"
            if shutil.which("grep")
            else f"findstr /s /n /c:{validated.pattern} *"
        )
        backend._record(started)
        return ToolResult(data=output[:8000], is_error=False)

    async def list_files(validated: _PathInput, context: ToolUseContext) -> ToolResult:
        del context
        started = time.perf_counter()
        try:
            names = backend.list_dir(validated.path or ".")
        except Exception as error:  # noqa: BLE001
            return ToolResult(data=f"{type(error).__name__}: {error}", is_error=True)
        backend._record(started)
        return ToolResult(data="\n".join(names), is_error=False)

    return (
        ToolDefinition(
            name="bash", description="Run a shell command.", input_model=_BashInput, execute=bash
        ),
        ToolDefinition(
            name="file_read",
            description="Read a file.",
            input_model=_PathInput,
            execute=file_read,
        ),
        ToolDefinition(
            name="file_write",
            description="Write a file.",
            input_model=_WriteInput,
            execute=file_write,
        ),
        ToolDefinition(
            name="file_edit",
            description="Replace text in a file.",
            input_model=_EditInput,
            execute=file_edit,
        ),
        ToolDefinition(
            name="grep",
            description="Search file contents.",
            input_model=_GrepInput,
            execute=grep,
        ),
        ToolDefinition(
            name="list_files",
            description="List a directory.",
            input_model=_PathInput,
            execute=list_files,
        ),
    )


class NeutralEngine(AnyCode):
    """AnyCode with its own registry/executor; no Atlas gate, ledger, context or budget."""

    def __init__(self, backend: LocalProcessBackend, config: Any = None) -> None:
        super().__init__(config)
        self.backend = backend
        registry = ToolRegistry()
        for tool in neutral_tool_definitions(backend):
            registry.register(tool)
        self._neutral_registry = registry
        self._neutral_executor = ToolExecutor(registry)

    def build_agent(
        self, config: AgentConfig | dict[str, object], *, output_schema: Any = None
    ) -> Agent:
        agent_config = AgentConfig.model_validate(config) if isinstance(config, dict) else config
        return Agent(
            agent_config,
            self._neutral_registry,
            self._neutral_executor,
            output_schema=output_schema,
        )
