"""Controlled business tool surface for one Commerce Agent Bench task.

Four tools, all registered as Atlas ``external_tools`` so every call passes the M3
``ActionSafetyGate`` and lands in the M4 ledger / M7 trajectory like any other tool:

* ``slack_list_messages`` — the native ``POST /slack/messages`` endpoint.
* ``slack_get_message``   — the native ``POST /slack/messages/get`` endpoint.
* ``read_task_file``      — read a file from the task's public workspace copy.
* ``write_deliverable``   — write one of the task's declared deliverables.

No shell, no arbitrary HTTP, no Docker, no Slack send/draft, no grader access. The Host binds the
mock-service address; the model never supplies a URL.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from anycode.tools.built_in import ToolDefinition
from anycode.types import ToolResult
from experiments.business_benchmarks.commerce_agent_bench.container import ContainerBackend
from experiments.business_benchmarks.commerce_agent_bench.public_inputs import (
    PublicInputError,
    PublicInputManifest,
)
from pydantic import BaseModel, Field

SLACK_PORT = 9110
AUTH_TOKEN = "local-mock-token"
MAX_MESSAGE_CHARS = 20_000
MAX_DELIVERABLE_BYTES = 400_000


class ToolBridgeError(RuntimeError):
    """A business tool refused the call."""


class SlackListInput(BaseModel):
    channel: str = Field(default="#rfq-stainless-bottle", description="Channel name.")
    max_results: int = Field(default=30, ge=1, le=50, description="Page size (1-50).")


class SlackGetInput(BaseModel):
    message_id: str = Field(description="Opaque message id returned by slack_list_messages.")


class ReadFileInput(BaseModel):
    path: str = Field(description="Workspace-relative path, e.g. 'api_reference.md'.")


class WriteDeliverableInput(BaseModel):
    path: str = Field(description="Declared deliverable path, e.g. 'outputs/answer.json'.")
    content: str = Field(description="Full UTF-8 content to write.")


#: Names that never exist in the agent-visible workspace; refused defensively even if a future
#: deployment accidentally stages them.
_BLOCKED_PREFIXES = frozenset({"private", "grader", "tests"})


def _safe_workspace_path(root: Path, raw: str) -> Path:
    import os

    candidate = PurePosixPath(str(raw).replace("\\", "/"))
    if candidate.is_absolute() or not candidate.parts or ".." in candidate.parts:
        raise ToolBridgeError(f"path must be workspace-relative without '..': {raw!r}")
    if len(str(raw)) >= 2 and str(raw)[1] == ":":
        raise ToolBridgeError(f"drive-letter paths are not allowed: {raw!r}")
    if candidate.parts[0] in _BLOCKED_PREFIXES:
        raise ToolBridgeError(f"path {raw!r} is not part of the public workspace")
    if candidate.parts[:2] == ("outputs", "mock_audit"):
        raise ToolBridgeError("the mock audit directory is not writable")
    target = root.joinpath(*candidate.parts)
    resolved = target.resolve()
    # Ownership by canonical containment, not by string prefix.
    root_resolved = root.resolve()
    if os.path.commonpath([str(root_resolved), str(resolved)]) != str(root_resolved):
        raise ToolBridgeError(f"path escapes the workspace: {raw!r}")
    if target.is_symlink():
        raise ToolBridgeError(f"symlinks are not readable: {raw!r}")
    return target


@dataclass(frozen=True)
class SlackTransport:
    """Where a Slack call is executed: published port from Windows, or inside the container."""

    backend: ContainerBackend
    container: str
    host_port: int | None = None

    def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        if self.host_port is not None:
            return self._post_from_host(path, body)
        return self._post_in_container(path, body)

    def _post_from_host(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.host_port}{path}",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {AUTH_TOKEN}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")[:400]
            raise ToolBridgeError(f"native mock API returned {error.code}: {detail}") from error
        except (urllib.error.URLError, TimeoutError) as error:
            raise ToolBridgeError(f"native mock API unreachable: {error}") from error

    def _post_in_container(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        script = (
            "import json,sys,urllib.request\n"
            "path, raw = sys.argv[1], sys.argv[2]\n"
            "req = urllib.request.Request('http://127.0.0.1:%d' % int(sys.argv[3]) + path,\n"
            "    data=raw.encode(), method='POST',\n"
            "    headers={'Authorization': 'Bearer ' + sys.argv[4],\n"
            "             'Content-Type': 'application/json'})\n"
            "print(urllib.request.urlopen(req, timeout=20).read().decode())\n"
        )
        payload = json.dumps(body)
        command = (
            "python3 -c "
            + _shell_quote(script)
            + " "
            + " ".join(_shell_quote(item) for item in (path, payload, str(SLACK_PORT), AUTH_TOKEN))
        )
        result = self.backend.exec_in(self.container, command, timeout_seconds=40)
        if not result.ok:
            raise ToolBridgeError(
                f"native mock API call failed (exit {result.exit_code}): {result.stderr[:400]}"
            )
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise ToolBridgeError("native mock API returned non-JSON") from error


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


@dataclass(frozen=True)
class BusinessToolset:
    """The tool definitions plus the policy metadata the gate needs."""

    tools: tuple[ToolDefinition, ...]
    actions: dict[str, str]
    schema_digest: str

    def agent_tools(self) -> tuple[str, ...]:
        return tuple(tool.name for tool in self.tools)


def build_slack_toolset(
    transport: SlackTransport,
    *,
    workspace: Path,
    allowed_deliverables: tuple[str, ...],
    manifest: PublicInputManifest | None = None,
) -> BusinessToolset:
    import hashlib

    def list_messages(validated: SlackListInput, context: Any) -> ToolResult:
        del context
        body = {
            "channel": validated.channel or "#rfq-stainless-bottle",
            "max_results": max(1, min(50, int(validated.max_results))),
        }
        return ToolResult(
            data=json.dumps(transport.post("/slack/messages", body), ensure_ascii=False)
        )

    def get_message(validated: SlackGetInput, context: Any) -> ToolResult:
        del context
        message_id = str(validated.message_id or "").strip()
        if not message_id:
            raise ToolBridgeError("message_id is required")
        payload = transport.post("/slack/messages/get", {"message_id": message_id})
        text = json.dumps(payload, ensure_ascii=False)
        if len(text) > MAX_MESSAGE_CHARS:
            raise ToolBridgeError("message payload exceeds the tool response limit")
        return ToolResult(data=text)

    def read_task_file(validated: ReadFileInput, context: Any) -> ToolResult:
        del context
        if manifest is None:
            raise ToolBridgeError("no public input manifest was frozen for this run")
        try:
            target = manifest.resolve(str(validated.path or ""))
        except PublicInputError as error:
            raise ToolBridgeError(str(error)) from error
        return ToolResult(data=target.read_text(encoding="utf-8", errors="replace"))

    def write_deliverable(validated: WriteDeliverableInput, context: Any) -> ToolResult:
        del context
        raw = str(validated.path or "").replace("\\", "/")
        if raw not in allowed_deliverables:
            raise ToolBridgeError(
                f"path {raw!r} is not an allowed deliverable; allowed: {list(allowed_deliverables)}"
            )
        content = str(validated.content or "")
        encoded = content.encode("utf-8")
        if len(encoded) > MAX_DELIVERABLE_BYTES:
            raise ToolBridgeError("deliverable exceeds the size limit")
        target = _safe_workspace_path(workspace, raw)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")
        return ToolResult(
            data=json.dumps(
                {"path": raw, "bytes": len(encoded), "sha256": hashlib.sha256(encoded).hexdigest()}
            )
        )

    tools = (
        ToolDefinition(
            name="slack_list_messages",
            description=(
                "List message previews in a Slack channel of the local mock workspace. "
                "Returns message ids that must be expanded with slack_get_message."
            ),
            input_model=SlackListInput,
            execute=list_messages,  # type: ignore[arg-type]
        ),
        ToolDefinition(
            name="slack_get_message",
            description="Fetch one full Slack message by its message id.",
            input_model=SlackGetInput,
            execute=get_message,  # type: ignore[arg-type]
        ),
        ToolDefinition(
            name="read_task_file",
            description="Read one public file of the current task workspace (UTF-8 text).",
            input_model=ReadFileInput,
            execute=read_task_file,  # type: ignore[arg-type]
        ),
        ToolDefinition(
            name="write_deliverable",
            description=(
                "Write one declared deliverable file. Only the task's declared output paths are "
                "accepted; the mock audit directory is never writable."
            ),
            input_model=WriteDeliverableInput,
            execute=write_deliverable,  # type: ignore[arg-type]
        ),
    )
    actions = {
        "slack_list_messages": "external.read",
        "slack_get_message": "external.read",
        "read_task_file": "filesystem.read",
        "write_deliverable": "filesystem.write",
    }
    schema = json.dumps(
        [
            {
                "name": tool.name,
                "description": tool.description,
                "input": tool.input_model.model_json_schema(),
            }
            for tool in tools
        ],
        sort_keys=True,
    )
    digest = "sha256:" + hashlib.sha256(schema.encode("utf-8")).hexdigest()
    return BusinessToolset(tools=tools, actions=actions, schema_digest=digest)


__all__ = [
    "AUTH_TOKEN",
    "BusinessToolset",
    "MAX_DELIVERABLE_BYTES",
    "SLACK_PORT",
    "SlackListInput",
    "SlackTransport",
    "ToolBridgeError",
    "build_slack_toolset",
]
