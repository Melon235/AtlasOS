"""Final-4 R-10: a neutral execution backend for the native AnyCode baseline.

Two backends implement one small async interface:

* :class:`LocalProcessBackend` — an isolated directory plus a subprocess shell (offline tests).
* :class:`SandboxToolBackend` — the **real** local OpenSandbox container, obtained from the shipped
  ``OpenSandboxProvider``. Nothing Atlas-specific is used: no gate, ledger, binding state machine,
  context plane, disclosure, router or recovery coordinator.

``NeutralEngine`` is an ``AnyCode`` subclass that swaps only the registry/executor pair; the agent
loop, scheduler, team machinery and turn hooks stay upstream (AnyCode 0.9.0).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from anycode import AnyCode
from anycode.core.agent import Agent
from anycode.sandbox import SandboxCommand
from anycode.tools.executor import ToolExecutor
from anycode.tools.registry import ToolRegistry
from anycode.types import AgentConfig, ToolDefinition, ToolResult, ToolUseContext
from pydantic import BaseModel

from atlasos.sandbox.opensandbox_provider import OpenSandboxProvider

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


class ToolBackend(Protocol):
    """The only surface the six neutral tools use."""

    operations: int
    timings_ms: list[float]

    async def aread(self, path: str) -> str: ...

    async def awrite(self, path: str, content: str) -> int: ...

    async def aedit(self, path: str, old: str, new: str) -> int: ...

    async def alist(self, path: str) -> list[str]: ...

    async def agrep(self, pattern: str, path: str) -> str: ...

    async def arun(self, command: str) -> tuple[int, str]: ...


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
        return sorted(entry.name for entry in self._resolve(path).iterdir())

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
        return process.returncode or 0, stdout.decode("utf-8", errors="replace")[:_MAX_OUTPUT]

    async def aread(self, path: str) -> str:
        return self.read(path)

    async def awrite(self, path: str, content: str) -> int:
        return self.write(path, content)

    async def aedit(self, path: str, old: str, new: str) -> int:
        return self.edit(path, old, new)

    async def alist(self, path: str) -> list[str]:
        return self.list_dir(path)

    async def arun(self, command: str) -> tuple[int, str]:
        return await self.run(command)

    async def agrep(self, pattern: str, path: str) -> str:
        import re

        base = self._resolve(path or ".")
        matcher = re.compile(pattern)
        hits: list[str] = []
        candidates = [base] if base.is_file() else sorted(base.rglob("*"))
        for candidate in candidates:
            if not candidate.is_file():
                continue
            try:
                text = candidate.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for number, line in enumerate(text.splitlines(), start=1):
                if matcher.search(line):
                    hits.append(f"{candidate}:{number}:{line}")
        return "\n".join(hits)


@dataclass
class SandboxToolBackend:
    """The same interface over a real OpenSandbox container created by the shipped provider."""

    provider: OpenSandboxProvider
    handle: Any
    workspace_root: str
    timeout_seconds: float = 120.0
    operations: int = 0
    timings_ms: list[float] = field(default_factory=list)
    _prepared: bool = False

    def _absolute(self, raw: str) -> str:
        if raw.startswith("/"):
            return raw
        return f"{self.workspace_root.rstrip('/')}/{raw.lstrip('./')}"

    def _record(self, started: float) -> None:
        self.operations += 1
        self.timings_ms.append((time.perf_counter() - started) * 1000.0)

    async def aread(self, path: str) -> str:
        await self._ensure()
        result = await self.provider.read_file(self.handle, self._absolute(path))
        if not result.ok:
            raise RuntimeError(f"read failed: {result.error or 'unknown'}")
        return bytes(result.data or b"").decode("utf-8", errors="replace")

    async def awrite(self, path: str, content: str) -> int:
        await self._ensure()
        result = await self.provider.write_file(
            self.handle, self._absolute(path), content.encode("utf-8")
        )
        if not result.ok:
            raise RuntimeError(f"write failed: {result.error or 'unknown'}")
        return len(content)

    async def aedit(self, path: str, old: str, new: str) -> int:
        text = await self.aread(path)
        if old not in text:
            raise ValueError("old text not found")
        await self.awrite(path, text.replace(old, new, 1))
        return 1

    async def alist(self, path: str) -> list[str]:
        target = self._absolute(path)
        script = "import os,sys;print(chr(10).join(sorted(os.listdir(sys.argv[1]))))"
        code, output = await self.arun(f'python3 -c "{script}" {target}')
        if code != 0:
            raise RuntimeError(f"list failed: {output}")
        return [line for line in output.splitlines() if line.strip()]

    async def agrep(self, pattern: str, path: str) -> str:
        safe_pattern = pattern.replace("'", "'\\''")
        safe_path = (path or ".").replace("'", "'\\''")
        _, output = await self.arun(f"grep -rn -- '{safe_pattern}' '{safe_path}' || true")
        return output

    async def arun(self, command: str) -> tuple[int, str]:
        await self._ensure()
        result = await self.provider.execute(
            self.handle,
            SandboxCommand(
                command=command,
                cwd=self.workspace_root,
                timeout_seconds=int(self.timeout_seconds),
            ),
        )
        output = (result.stdout or "") + (result.stderr or "")
        return int(result.exit_code or 0), output[:_MAX_OUTPUT]

    async def _ensure(self) -> None:
        if not self._prepared:
            await self._prepare_workspace()

    async def _prepare_workspace(self) -> None:
        """A fresh container has no ``/workspace/project``; commands need it to exist."""

        await self.provider.execute(
            self.handle,
            SandboxCommand(
                command=f"mkdir -p {self.workspace_root}",
                cwd="/",
                timeout_seconds=60,
            ),
        )
        self._prepared = True


def neutral_tool_definitions(backend: ToolBackend) -> tuple[ToolDefinition, ...]:
    """The six logical tools, executed by the backend instead of the host CWD."""

    async def bash(validated: _BashInput, context: ToolUseContext) -> ToolResult:
        del context
        started = time.perf_counter()
        code, output = await backend.arun(validated.command)
        backend._record(started)  # noqa: SLF001 - backend owns its own counters
        return ToolResult(data=output, is_error=code != 0)

    async def file_read(validated: _PathInput, context: ToolUseContext) -> ToolResult:
        del context
        started = time.perf_counter()
        try:
            text = await backend.aread(validated.path)
        except Exception as error:  # noqa: BLE001 - reported as a tool error
            return ToolResult(data=f"{type(error).__name__}: {error}", is_error=True)
        backend._record(started)  # noqa: SLF001
        return ToolResult(data=text[:_MAX_OUTPUT], is_error=False)

    async def file_write(validated: _WriteInput, context: ToolUseContext) -> ToolResult:
        del context
        started = time.perf_counter()
        try:
            written = await backend.awrite(validated.path, validated.content)
        except Exception as error:  # noqa: BLE001
            return ToolResult(data=f"{type(error).__name__}: {error}", is_error=True)
        backend._record(started)  # noqa: SLF001
        return ToolResult(data=f"wrote {written} chars to {validated.path}", is_error=False)

    async def file_edit(validated: _EditInput, context: ToolUseContext) -> ToolResult:
        del context
        started = time.perf_counter()
        try:
            await backend.aedit(validated.path, validated.old, validated.new)
        except Exception as error:  # noqa: BLE001
            return ToolResult(data=f"{type(error).__name__}: {error}", is_error=True)
        backend._record(started)  # noqa: SLF001
        return ToolResult(data=f"edited {validated.path}", is_error=False)

    async def grep(validated: _GrepInput, context: ToolUseContext) -> ToolResult:
        del context
        started = time.perf_counter()
        output = await backend.agrep(validated.pattern, validated.path)
        backend._record(started)  # noqa: SLF001
        return ToolResult(data=output[:8000], is_error=False)

    async def list_files(validated: _PathInput, context: ToolUseContext) -> ToolResult:
        del context
        started = time.perf_counter()
        try:
            names = await backend.alist(validated.path or ".")
        except Exception as error:  # noqa: BLE001
            return ToolResult(data=f"{type(error).__name__}: {error}", is_error=True)
        backend._record(started)  # noqa: SLF001
        return ToolResult(data="\n".join(names), is_error=False)

    return (
        ToolDefinition(
            name="bash", description="Run a shell command.", input_model=_BashInput, execute=bash
        ),
        ToolDefinition(
            name="file_read", description="Read a file.", input_model=_PathInput, execute=file_read
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
            name="grep", description="Search file contents.", input_model=_GrepInput, execute=grep
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

    def __init__(self, backend: ToolBackend, config: Any = None) -> None:
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
