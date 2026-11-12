"""B0-R2: the business tool surface refuses everything outside the public task contract."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from experiments.business_benchmarks.commerce_agent_bench.public_inputs import (
    PublicInputManifest,
)
from experiments.business_benchmarks.commerce_agent_bench.tool_bridge import (
    MAX_DELIVERABLE_BYTES,
    BusinessToolset,
    SlackTransport,
    ToolBridgeError,
    build_slack_toolset,
)

DELIVERABLES = ("outputs/answer.json", "outputs/quote_consolidation.md")


class FakeTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.payload = {"messages": [{"message_id": "msg_1"}], "next_cursor": None}

    def post(self, path: str, body: dict) -> dict:
        self.calls.append((path, body))
        return self.payload


def _toolset(tmp_path: Path, transport) -> BusinessToolset:
    """Stage a workspace the way the runner does: freeze inputs, then let services write state."""

    source = tmp_path / "source"
    (source / "api_reference.md").parent.mkdir(parents=True, exist_ok=True)
    (source / "api_reference.md").write_text("# api\n", encoding="utf-8")
    (source / "state_manifest.md").write_text("# state\n", encoding="utf-8")
    workspace = tmp_path / "workspace"
    for item in source.rglob("*"):
        if item.is_file():
            target = workspace / item.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(item.read_bytes())
    # Runtime-generated files that must stay unreadable even though they sit in the workspace.
    (workspace / "outputs" / "mock_state").mkdir(parents=True, exist_ok=True)
    (workspace / "outputs" / "mock_audit").mkdir(parents=True, exist_ok=True)
    (workspace / "outputs" / "mock_state" / "slack_final.json").write_text(
        '{"internal": true}', encoding="utf-8"
    )
    (workspace / "outputs" / "mock_audit" / "slack_audit.json").write_text(
        '{"calls": []}', encoding="utf-8"
    )
    (workspace / "private").mkdir(exist_ok=True)
    (workspace / "private" / "expected_answer.json").write_text("{}", encoding="utf-8")
    (workspace / "tmp").mkdir(exist_ok=True)
    (workspace / "tmp" / "slack.log").write_text("service log\n", encoding="utf-8")
    manifest = PublicInputManifest.freeze(workspace, source_root=source)
    return build_slack_toolset(
        transport,
        workspace=workspace,
        allowed_deliverables=DELIVERABLES,
        manifest=manifest,
    )


def _run(toolset, name: str, **kwargs):
    """Tools are called exactly as the runtime does: (validated_input, context)."""

    tool = next(item for item in toolset.tools if item.name == name)
    return tool.execute(tool.input_model(**kwargs), None).data  # type: ignore[union-attr]


def test_public_file_is_readable(tmp_path: Path) -> None:
    toolset = _toolset(tmp_path, FakeTransport())
    assert _run(toolset, "read_task_file", path="api_reference.md") == "# api\n"


@pytest.mark.parametrize(
    "path",
    [
        "../outside.txt",
        "/etc/passwd",
        "C:/Windows/win.ini",
        "//server/share/x.txt",
        "private/expected_answer.json",
        "grader/run.sh",
        "outputs/../../escape.txt",
        "outputs/mock_state/slack_final.json",
        "outputs/mock_audit/slack_audit.json",
        "tmp/slack.log",
        "not_registered.md",
    ],
)
def test_internal_state_and_escapes_are_refused(tmp_path: Path, path: str) -> None:
    toolset = _toolset(tmp_path, FakeTransport())
    with pytest.raises(ToolBridgeError):
        _run(toolset, "read_task_file", path=path)


def test_registered_public_inputs_are_readable(tmp_path: Path) -> None:
    toolset = _toolset(tmp_path, FakeTransport())
    assert _run(toolset, "read_task_file", path="api_reference.md") == "# api\n"
    assert _run(toolset, "read_task_file", path="state_manifest.md") == "# state\n"
    assert _run(toolset, "read_task_file", path="./api_reference.md") == "# api\n"


def test_symlinked_input_is_refused(tmp_path: Path) -> None:
    toolset = _toolset(tmp_path, FakeTransport())
    workspace = tmp_path / "workspace"
    link = workspace / "link.md"
    try:
        link.symlink_to(workspace / "api_reference.md")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available on this platform")
    with pytest.raises(ToolBridgeError):
        _run(toolset, "read_task_file", path="link.md")


def test_manifest_digest_is_recorded(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "a.md").parent.mkdir(parents=True, exist_ok=True)
    (workspace / "a.md").write_text("x", encoding="utf-8")
    manifest = PublicInputManifest.freeze(workspace, source_root=workspace)
    assert manifest.digest.startswith("sha256:")
    assert manifest.as_dict()["entries"] == 1


def test_only_declared_deliverables_can_be_written(tmp_path: Path) -> None:
    toolset = _toolset(tmp_path, FakeTransport())
    with pytest.raises(ToolBridgeError):
        _run(toolset, "write_deliverable", path="outputs/mock_audit/slack_audit.json", content="x")
    with pytest.raises(ToolBridgeError):
        _run(toolset, "write_deliverable", path="api_reference.md", content="x")
    result = json.loads(
        _run(toolset, "write_deliverable", path="outputs/answer.json", content='{"a": 1}')
    )
    assert result["bytes"] == 8
    written = tmp_path / "workspace" / "outputs" / "answer.json"
    assert json.loads(written.read_text(encoding="utf-8")) == {"a": 1}


def test_oversized_deliverable_is_refused(tmp_path: Path) -> None:
    toolset = _toolset(tmp_path, FakeTransport())
    with pytest.raises(ToolBridgeError):
        _run(
            toolset,
            "write_deliverable",
            path="outputs/quote_consolidation.md",
            content="x" * (MAX_DELIVERABLE_BYTES + 1),
        )


def test_slack_tools_use_fixed_endpoints(tmp_path: Path) -> None:
    transport = FakeTransport()
    toolset = _toolset(tmp_path, transport)
    listing = json.loads(_run(toolset, "slack_list_messages", channel="#rfq-stainless-bottle"))
    assert listing["messages"][0]["message_id"] == "msg_1"
    _run(toolset, "slack_get_message", message_id="msg_9")
    assert [path for path, _ in transport.calls] == ["/slack/messages", "/slack/messages/get"]
    assert transport.calls[1][1] == {"message_id": "msg_9"}


def test_page_size_above_the_native_limit_is_rejected(tmp_path: Path) -> None:
    from pydantic import ValidationError

    toolset = _toolset(tmp_path, FakeTransport())
    with pytest.raises(ValidationError):
        _run(toolset, "slack_list_messages", channel="#c", max_results=500)


def test_toolset_has_no_shell_http_or_slack_write_tools(tmp_path: Path) -> None:
    toolset = _toolset(tmp_path, FakeTransport())
    assert set(toolset.agent_tools()) == {
        "slack_list_messages",
        "slack_get_message",
        "read_task_file",
        "write_deliverable",
    }
    for forbidden in ("bash", "git", "python", "http", "slack_send", "slack_draft", "grader"):
        assert forbidden not in toolset.agent_tools()


def test_schema_digest_is_stable(tmp_path: Path) -> None:
    first = _toolset(tmp_path, FakeTransport()).schema_digest
    second = _toolset(tmp_path, FakeTransport()).schema_digest
    assert first == second and first.startswith("sha256:")


def test_slack_transport_requires_a_bound_address() -> None:
    """The model never supplies a URL: the transport is Host-constructed."""

    transport = SlackTransport(backend=object(), container="c", host_port=None)  # type: ignore[arg-type]
    assert transport.host_port is None
    assert transport.container == "c"
