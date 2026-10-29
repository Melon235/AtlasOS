"""Same six logical workspace tools backed by one trusted sandbox binding."""

from __future__ import annotations

import asyncio
import base64
import posixpath

from anycode.constants import (
    BASH_MAX_OUTPUT_BYTES,
    BASH_TIMEOUT_LIMIT_S,
    DEFAULT_ENCODING,
)
from anycode.sandbox import SandboxCommand, SandboxProvider
from anycode.tools.built_in import BUILT_IN_TOOLS
from anycode.types import ToolDefinition, ToolResult, ToolUseContext

from atlasos.sandbox.binding import ProjectSandboxManager
from atlasos.sandbox.models import SandboxLostError

_WORKSPACE_TOOLS = frozenset({"bash", "file_read", "file_write", "file_edit", "grep", "list_files"})

_GREP_HELPER = r"""
import base64, json, os, re, sys
pattern = base64.b64decode(sys.argv[1]).decode()
root = base64.b64decode(sys.argv[2]).decode()
glob = base64.b64decode(sys.argv[3]).decode() if sys.argv[3] != "-" else ""
limit = int(sys.argv[4])
regex = re.compile(pattern)
ignore = {".git", "node_modules", "__pycache__", ".venv", "dist", "build"}
hits = []
if os.path.isfile(root):
    files = [root]
else:
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ignore]
        for name in filenames:
            if glob and not __import__("fnmatch").fnmatch(name, glob):
                continue
            files.append(os.path.join(dirpath, name))
for file in files:
    if len(hits) >= limit:
        break
    try:
        text = open(file, encoding="utf-8", errors="replace").read()
    except Exception:
        continue
    for number, line in enumerate(text.split("\n"), 1):
        if regex.search(line):
            hits.append(f"{os.path.relpath(file, root)}:{number}:{line}")
            if len(hits) >= limit:
                break
print("\n".join(hits) if hits else "No matches.")
""".strip()

_LIST_HELPER = r"""
import base64, os, sys
root = base64.b64decode(sys.argv[1]).decode()
glob = base64.b64decode(sys.argv[2]).decode() if sys.argv[2] != "-" else ""
limit = int(sys.argv[3])
ignore = {".git", "node_modules", "__pycache__", ".venv", "dist", "build"}
paths = []
for dirpath, dirnames, filenames in os.walk(root):
    dirnames[:] = [d for d in dirnames if d not in ignore]
    for name in filenames:
        if glob and not __import__("fnmatch").fnmatch(name, glob):
            continue
        paths.append(os.path.relpath(os.path.join(dirpath, name), root))
paths.sort()
print("\n".join(paths[:limit]) if paths else "(no files)")
""".strip()


class SandboxWorkspaceTools:
    """Copy the six logical tool definitions; replace only their physical backend."""

    def __init__(
        self,
        manager: ProjectSandboxManager,
        task_id: str,
        provider: SandboxProvider,
    ) -> None:
        self.manager = manager
        self.task_id = task_id
        self.provider = provider
        self.lock = asyncio.Lock()

    def _binding(self):
        try:
            return self.manager.require_ready(self.task_id)
        except SandboxLostError:
            raise
        except Exception as error:
            raise SandboxLostError(str(error)) from error

    def _logical(self, raw: str | None, *, default_workspace: bool = False) -> str:
        workspace = self._binding().workspace_root
        if raw is None:
            if not default_workspace:
                raise ValueError("a workspace path is required")
            raw = workspace
        if not raw.startswith("/"):
            raw = posixpath.join(workspace, raw)
        normalized = posixpath.normpath(raw)
        if normalized != workspace and not normalized.startswith(workspace + "/"):
            raise ValueError(f'path "{raw}" escapes sandbox workspace {workspace}')
        return normalized

    def _relative(self, logical: str) -> str:
        workspace = self._binding().workspace_root
        return posixpath.relpath(logical, workspace)

    async def _execute(self, command: str, *, cwd: str, timeout_seconds: int = 60):
        binding = self._binding()
        async with self.lock:
            try:
                result = await self.provider.execute(
                    binding.sandbox_handle,
                    SandboxCommand(command=command, cwd=cwd, timeout_seconds=timeout_seconds),
                )
            except SandboxLostError:
                await self.manager.mark_lost(self.task_id)
                raise
        return result

    async def file_read(self, validated, context: ToolUseContext) -> ToolResult:
        del context
        try:
            logical = self._logical(validated.path)
            binding = self._binding()
            async with self.lock:
                result = await self.provider.read_file(binding.sandbox_handle, logical)
            if not result.ok or result.data is None:
                return ToolResult(
                    data=f'Unable to read "{validated.path}": {result.error}', is_error=True
                )
            raw = result.data.decode(DEFAULT_ENCODING, errors="replace")
        except ValueError as error:
            return ToolResult(data=str(error), is_error=True)
        except SandboxLostError as error:
            await self.manager.mark_lost(self.task_id)
            return ToolResult(data=str(error), is_error=True)

        lines = raw.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        total = len(lines)
        start = max(0, (validated.offset or 1) - 1)
        if start >= total > 0:
            return ToolResult(
                data=f'File "{validated.path}" contains {total} line{"s" if total != 1 else ""} '
                f"but offset {validated.offset} exceeds the range.",
                is_error=True,
            )
        end = min(start + validated.limit, total) if validated.limit else total
        numbered = "\n".join(f"{start + i + 1}\t{line}" for i, line in enumerate(lines[start:end]))
        meta = f"\n\n(showing lines {start + 1}\u2013{end} of {total})" if end < total else ""
        return ToolResult(data=numbered + meta, is_error=False)

    async def file_write(self, validated, context: ToolUseContext) -> ToolResult:
        del context
        try:
            logical = self._logical(validated.path)
            binding = self._binding()
            async with self.lock:
                existed_result = await self.provider.read_file(binding.sandbox_handle, logical)
                existed = existed_result.ok
                logical_parent = posixpath.dirname(logical)
                if logical_parent != binding.workspace_root:
                    await self.provider.execute(
                        binding.sandbox_handle,
                        SandboxCommand(
                            command=f'mkdir -p "{logical_parent}"',
                            cwd=binding.workspace_root,
                            timeout_seconds=30,
                        ),
                    )
                result = await self.provider.write_file(
                    binding.sandbox_handle, logical, validated.content.encode(DEFAULT_ENCODING)
                )
            if not result.ok:
                return ToolResult(
                    data=f'Could not write file "{validated.path}": {result.error}', is_error=True
                )
        except ValueError as error:
            return ToolResult(data=str(error), is_error=True)
        except SandboxLostError as error:
            await self.manager.mark_lost(self.task_id)
            return ToolResult(data=str(error), is_error=True)
        line_count = validated.content.count("\n") + (
            1 if validated.content and not validated.content.endswith("\n") else 0
        )
        byte_count = len(validated.content.encode(DEFAULT_ENCODING))
        action = "Overwrote" if existed else "Created"
        return ToolResult(
            data=f'{action} "{validated.path}" ({line_count} line{"s" if line_count != 1 else ""}, '
            f"{byte_count} bytes).",
            is_error=False,
        )

    async def file_edit(self, validated, context: ToolUseContext) -> ToolResult:
        del context
        try:
            logical = self._logical(validated.path)
            binding = self._binding()
            async with self.lock:
                read = await self.provider.read_file(binding.sandbox_handle, logical)
                if not read.ok or read.data is None:
                    return ToolResult(
                        data=f'Unable to read "{validated.path}": {read.error}', is_error=True
                    )
                original = read.data.decode(DEFAULT_ENCODING, errors="replace")
                hits = _tally(original, validated.old_string)
                if hits == 0:
                    return ToolResult(
                        data=f'The target string was not found in "{validated.path}".\n'
                        "Verify that old_string matches the file content exactly.",
                        is_error=True,
                    )
                if hits > 1 and not validated.replace_all:
                    return ToolResult(
                        data=f'old_string appears {hits} times in "{validated.path}". '
                        "Use a more specific string or set replace_all=True.",
                        is_error=True,
                    )
                updated = (
                    original.replace(validated.old_string, validated.new_string)
                    if validated.replace_all
                    else original.replace(validated.old_string, validated.new_string, 1)
                )
                write = await self.provider.write_file(
                    binding.sandbox_handle, logical, updated.encode(DEFAULT_ENCODING)
                )
                if not write.ok:
                    return ToolResult(
                        data=f'Unable to write "{validated.path}": {write.error}',
                        is_error=True,
                    )
        except ValueError as error:
            return ToolResult(data=str(error), is_error=True)
        except SandboxLostError as error:
            await self.manager.mark_lost(self.task_id)
            return ToolResult(data=str(error), is_error=True)
        replaced = hits if validated.replace_all else 1
        return ToolResult(
            data=(
                f"Replaced {replaced} occurrence{'s' if replaced != 1 else ''} "
                f'in "{validated.path}".'
            ),
            is_error=False,
        )

    async def grep(self, validated, context: ToolUseContext) -> ToolResult:
        del context
        try:
            logical = self._logical(validated.path, default_workspace=True)
            relative = self._relative(logical) or "."
            pattern = base64.b64encode(validated.pattern.encode()).decode()
            root = base64.b64encode(relative.encode()).decode()
            glob = base64.b64encode(validated.glob.encode()).decode() if validated.glob else "-"
            encoded = base64.b64encode(_GREP_HELPER.encode()).decode()
            command = (
                "python3 -c \"import base64;exec(base64.b64decode('"
                + encoded
                + "'))\" "
                + f"{pattern} {root} {glob} {validated.max_results}"
            )
            result = await self._execute(command, cwd=self._binding().workspace_root)
            if not result.ok or result.exit_code != 0:
                return ToolResult(
                    data=f"grep helper failed: {result.stderr or result.error}", is_error=True
                )
            output = result.stdout.strip()
            if output == "No matches.":
                return ToolResult(data="No matches.", is_error=False)
            return ToolResult(data=output, is_error=False)
        except (ValueError, SandboxLostError) as error:
            return ToolResult(data=str(error), is_error=True)

    async def list_files(self, validated, context: ToolUseContext) -> ToolResult:
        del context
        try:
            logical = self._logical(validated.path, default_workspace=True)
            relative = self._relative(logical) or "."
            root = base64.b64encode(relative.encode()).decode()
            glob = base64.b64encode(validated.glob.encode()).decode() if validated.glob else "-"
            encoded = base64.b64encode(_LIST_HELPER.encode()).decode()
            command = (
                "python3 -c \"import base64;exec(base64.b64decode('"
                + encoded
                + "'))\" "
                + f"{root} {glob} {validated.max_results}"
            )
            result = await self._execute(command, cwd=self._binding().workspace_root)
            if not result.ok or result.exit_code != 0:
                return ToolResult(
                    data=f"list_files helper failed: {result.stderr or result.error}",
                    is_error=True,
                )
            output = result.stdout.strip()
            if output == "(no files)":
                return ToolResult(data="No files found (backend: sandbox).", is_error=False)
            count = len(output.splitlines())
            note = f"\n\n({count} file{'s' if count != 1 else ''}, backend: sandbox)"
            return ToolResult(data=output + note, is_error=False)
        except (ValueError, SandboxLostError) as error:
            return ToolResult(data=str(error), is_error=True)

    async def bash(self, validated, context: ToolUseContext) -> ToolResult:
        del context
        try:
            cwd = self._logical(validated.cwd, default_workspace=True)
            timeout = int(validated.timeout or BASH_TIMEOUT_LIMIT_S)
            cap = (
                validated.max_output_bytes
                if validated.max_output_bytes is not None
                else BASH_MAX_OUTPUT_BYTES
            )
            result = await self._execute(validated.command, cwd=cwd, timeout_seconds=timeout)
            if not result.ok:
                return ToolResult(data=f"sandbox execution failed: {result.error}", is_error=True)
            stdout = _decode_capped(result.stdout.encode(), result.stdout, cap)
            stderr = _decode_capped(result.stderr.encode(), result.stderr, cap)
            parts: list[str] = []
            if stdout:
                parts.append(stdout)
            if stderr:
                parts.append(f"-- stderr --\n{stderr}" if stdout else stderr)
            code = result.exit_code or 0
            if not parts:
                return ToolResult(
                    data="(completed silently \u2014 no output)"
                    if code == 0
                    else f"(exited with code {code} \u2014 no output produced)",
                    is_error=code != 0,
                )
            if code != 0:
                parts.append(f"\n(exit code {code})")
            return ToolResult(data="\n".join(parts), is_error=code != 0)
        except (ValueError, SandboxLostError) as error:
            return ToolResult(data=str(error), is_error=True)

    def tool_definitions(self) -> tuple[ToolDefinition, ...]:
        builders = {
            "bash": self.bash,
            "file_read": self.file_read,
            "file_write": self.file_write,
            "file_edit": self.file_edit,
            "grep": self.grep,
            "list_files": self.list_files,
        }
        return tuple(
            definition.model_copy(update={"execute": builders[definition.name]})
            for definition in BUILT_IN_TOOLS
            if definition.name in _WORKSPACE_TOOLS
        )


def _tally(source: str, search: str) -> int:
    if not search:
        return 0
    count = 0
    position = 0
    while True:
        position = source.find(search, position)
        if position == -1:
            return count
        count += 1
        position += len(search)


def _decode_capped(stored: bytes, text: str, cap: int) -> str:
    del stored
    encoded = text.encode(DEFAULT_ENCODING, errors="replace")
    if len(encoded) <= cap:
        return text
    return (
        encoded[:cap].decode(DEFAULT_ENCODING, errors="replace")
        + f"\n[output truncated: showing first {cap} of {len(encoded)} bytes]"
    )


def sandbox_tool_names() -> frozenset[str]:
    return _WORKSPACE_TOOLS
