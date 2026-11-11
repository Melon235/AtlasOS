"""Stage R1-1: ownership enforcement through the real M3 gate (T1-T3, T9, T14-T16).

Every test drives ``ActionSafetyGate`` the way the runtime does, so the assertions cover the real
seam (external policy -> DENY -> audit -> no tool body) instead of a private helper.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from anycode.identity.context import ExecutionContext
from anycode.tools.executor import ToolExecutor
from anycode.tools.idempotency import InMemoryToolIdempotencyStore
from anycode.tools.registry import ToolRegistry
from anycode.types import AgentInfo, ToolDefinition, ToolResult, ToolUseContext
from pydantic import BaseModel

from atlasos.actions import ActionCatalog, ActionPolicy, ActionRule, ActionSafetyGate
from atlasos.ownership import (
    OWNERSHIP_DENIED_FOREIGN_PATH,
    OWNERSHIP_DENIED_UNDECLARED_PATH,
    OWNERSHIP_DENIED_WORKSPACE_WIDE,
    OWNERSHIP_MODE_AUDIT,
    OWNERSHIP_MODE_ENFORCE,
    OWNERSHIP_MODE_OFF,
    OWNERSHIP_UNCLASSIFIABLE_SHELL,
    OWNERSHIP_UNKNOWN_WORKER,
    OwnedPathDeclaration,
    WorkspaceOwnershipPolicy,
    build_manifest,
    build_ownership_enforcer,
)

ROOT = "/workspace/project"
RUN_ID = "run-r11"


class WriteInput(BaseModel):
    path: str
    content: str = "x"


class EditInput(BaseModel):
    path: str
    old_string: str = "a"
    new_string: str = "b"
    replace_all: bool = False


class BashInput(BaseModel):
    command: str
    cwd: str | None = None


def _manifest():
    return build_manifest(
        run_id=RUN_ID,
        workspace_root=ROOT,
        declarations=(
            OwnedPathDeclaration(
                worker_id=f"{RUN_ID}-a",
                task_id=f"{RUN_ID}-a",
                paths=("ledger/entries.py",),
                provenance="verified_task_metadata",
            ),
            OwnedPathDeclaration(
                worker_id=f"{RUN_ID}-b",
                task_id=f"{RUN_ID}-b",
                paths=("ledger/money.py",),
                provenance="verified_task_metadata",
            ),
        ),
    )


def _context(worker: str = "a", *, run_id: str = RUN_ID) -> ToolUseContext:
    task_id = f"{run_id}-{worker}"
    attributes: dict[str, Any] = {
        "run_id": run_id,
        "task_id": task_id,
        "worker_id": task_id,
        "profile_id": "coding_worker",
        "operating_scope": "coding",
        "atlas_worker": {
            "worker_id": task_id,
            "profile_id": "coding_worker",
            "task_id": task_id,
            "parent_agent": "atlas-evaluation",
        },
    }
    return ToolUseContext(
        agent=AgentInfo(name=f"coding_worker:{task_id}", role="worker", model="fake"),
        execution_context=ExecutionContext(
            principal="user:evaluation",
            tenant_scope="atlas-evaluation",
            attributes=attributes,
        ),
    )


async def _placeholder(validated, context: ToolUseContext) -> ToolResult:
    del validated, context
    return ToolResult(data="placeholder")


TOOLS = (
    ToolDefinition(
        name="file_write", description="write", input_model=WriteInput, execute=_placeholder
    ),
    ToolDefinition(
        name="file_edit", description="edit", input_model=EditInput, execute=_placeholder
    ),
    ToolDefinition(name="bash", description="shell", input_model=BashInput, execute=_placeholder),
)


def _allow_policy() -> ActionPolicy:
    return ActionPolicy(
        name="test",
        default="DENY",
        rules=(
            ActionRule(id="allow-write", decision="ALLOW", action="filesystem.write"),
            ActionRule(id="allow-shell", decision="ALLOW", action="shell.execute"),
        ),
    )


def _gate(
    mode: str, *, retry_safe: bool = True
) -> tuple[ActionSafetyGate, list[dict[str, Any]], WorkspaceOwnershipPolicy]:
    registry = ToolRegistry()
    executed: list[dict[str, Any]] = []

    for definition in TOOLS:

        async def execute(validated, context: ToolUseContext, _name=definition.name) -> ToolResult:
            del context
            executed.append({"tool": _name, **validated.model_dump(mode="json")})
            return ToolResult(data=f"{_name}:ok")

        registry.register(definition.model_copy(update={"execute": execute}))

    enforcer, policy = build_ownership_enforcer(_manifest(), mode=mode)
    assert policy is not None
    inner = ToolExecutor(registry, idempotency_store=InMemoryToolIdempotencyStore())
    gate = ActionSafetyGate(
        inner,
        registry,
        policy=_allow_policy(),
        catalog=ActionCatalog(),
        external_policy=enforcer,
        external_denial_retry_safe=retry_safe,
    )
    return gate, executed, policy


def test_t1_worker_writes_its_own_file() -> None:
    gate, executed, policy = _gate(OWNERSHIP_MODE_ENFORCE)
    result = asyncio.run(
        gate.execute("file_write", {"path": "ledger/entries.py", "content": "ok"}, _context("a"))
    )
    assert not result.is_error
    assert executed == [{"tool": "file_write", "path": "ledger/entries.py", "content": "ok"}]
    assert policy.evidence.denied_calls == 0


def test_t2_foreign_file_write_is_denied_without_executing() -> None:
    gate, executed, policy = _gate(OWNERSHIP_MODE_ENFORCE)
    result = asyncio.run(
        gate.execute("file_write", {"path": "ledger/money.py", "content": "bad"}, _context("a"))
    )
    assert result.is_error is True
    assert OWNERSHIP_DENIED_FOREIGN_PATH in result.data
    assert executed == []
    assert policy.evidence.denied_calls == 1
    assert policy.evidence.denials[0]["reason_code"] == OWNERSHIP_DENIED_FOREIGN_PATH
    assert policy.evidence.denials[0]["worker_id"] == f"{RUN_ID}-a"


def test_t3_foreign_file_edit_is_denied() -> None:
    gate, executed, _ = _gate(OWNERSHIP_MODE_ENFORCE)
    result = asyncio.run(
        gate.execute("file_edit", {"path": "ledger/money.py", "old_string": "a"}, _context("a"))
    )
    assert result.is_error is True
    assert OWNERSHIP_DENIED_FOREIGN_PATH in result.data
    assert executed == []


def test_denied_write_is_marked_retry_safe_only_when_configured() -> None:
    gate, executed, _ = _gate(OWNERSHIP_MODE_ENFORCE, retry_safe=True)
    denied = asyncio.run(
        gate.execute("file_write", {"path": "ledger/money.py", "content": "x"}, _context("a"))
    )
    assert denied.retry_safe is True
    assert executed == []

    legacy_gate, _, _ = _gate(OWNERSHIP_MODE_ENFORCE, retry_safe=False)
    legacy = asyncio.run(
        legacy_gate.execute(
            "file_write", {"path": "ledger/money.py", "content": "x"}, _context("a")
        )
    )
    assert legacy.retry_safe is False


def test_undeclared_path_is_denied() -> None:
    gate, executed, _ = _gate(OWNERSHIP_MODE_ENFORCE)
    result = asyncio.run(
        gate.execute("file_write", {"path": "unowned/new.py", "content": "x"}, _context("a"))
    )
    assert OWNERSHIP_DENIED_UNDECLARED_PATH in result.data
    assert executed == []


@pytest.mark.parametrize(
    "raw",
    ["../ledger/money.py", "/etc/passwd", "/workspace/project/ledger/money.py", "ledger\\money.py"],
)
def test_path_escape_is_denied_for_every_spelling(raw: str) -> None:
    gate, executed, _ = _gate(OWNERSHIP_MODE_ENFORCE)
    result = asyncio.run(gate.execute("file_write", {"path": raw, "content": "x"}, _context("a")))
    assert result.is_error is True, raw
    assert executed == []


def test_directory_creation_for_an_owned_file_is_allowed() -> None:
    manifest = build_manifest(
        run_id=RUN_ID,
        workspace_root=ROOT,
        declarations=(
            OwnedPathDeclaration(
                worker_id=f"{RUN_ID}-a",
                task_id=f"{RUN_ID}-a",
                paths=("ledger/nested/deep.py",),
                provenance="verified_task_metadata",
            ),
        ),
    )
    enforcer, _ = build_ownership_enforcer(manifest, mode=OWNERSHIP_MODE_ENFORCE)
    registry = ToolRegistry()
    executed: list[str] = []

    async def execute(validated, context: ToolUseContext) -> ToolResult:
        del validated, context
        executed.append("bash")
        return ToolResult(data="ok")

    registry.register(TOOLS[2].model_copy(update={"execute": execute}))
    gate = ActionSafetyGate(
        ToolExecutor(registry, idempotency_store=InMemoryToolIdempotencyStore()),
        registry,
        policy=_allow_policy(),
        catalog=ActionCatalog(),
        external_policy=enforcer,
        external_denial_retry_safe=True,
    )
    allowed = asyncio.run(
        gate.execute("bash", {"command": "mkdir -p ledger/nested"}, _context("a"))
    )
    assert not allowed.is_error, allowed.data
    assert executed == ["bash"]


def test_t4_historical_rollback_is_blocked_in_enforce() -> None:
    gate, executed, policy = _gate(OWNERSHIP_MODE_ENFORCE)
    result = asyncio.run(
        gate.execute("bash", {"command": "git checkout -- ledger/money.py"}, _context("a"))
    )
    assert result.is_error is True
    assert OWNERSHIP_DENIED_FOREIGN_PATH in result.data
    assert executed == []
    assert policy.evidence.denied_calls == 1


def test_t4_historical_rollback_is_only_recorded_in_audit() -> None:
    gate, executed, policy = _gate(OWNERSHIP_MODE_AUDIT)
    result = asyncio.run(
        gate.execute("bash", {"command": "git checkout -- ledger/money.py"}, _context("a"))
    )
    assert not result.is_error
    assert [item["tool"] for item in executed] == ["bash"]
    assert policy.evidence.denials == []
    assert len(policy.evidence.audit_findings) == 1
    assert policy.evidence.audit_findings[0]["decision"] == "AUDIT"


def test_dev_null_redirect_is_not_a_workspace_escape() -> None:
    gate, executed, _ = _gate(OWNERSHIP_MODE_ENFORCE)
    result = asyncio.run(gate.execute("bash", {"command": "git status > /dev/null"}, _context("a")))
    assert not result.is_error, result.data
    assert [item["tool"] for item in executed] == ["bash"]


def test_cd_scoped_relative_write_is_checked_against_the_cwd() -> None:
    gate, executed, _ = _gate(OWNERSHIP_MODE_ENFORCE)
    result = asyncio.run(
        gate.execute("bash", {"command": "cd ledger && rm money.py"}, _context("a"))
    )
    assert result.is_error is True
    assert OWNERSHIP_DENIED_FOREIGN_PATH in result.data
    assert executed == []


def test_t5_multi_target_rollback_is_denied_on_the_second_path() -> None:
    gate, executed, _ = _gate(OWNERSHIP_MODE_ENFORCE)
    result = asyncio.run(
        gate.execute(
            "bash",
            {"command": "git checkout -- ledger/entries.py ledger/money.py"},
            _context("a"),
        )
    )
    assert result.is_error is True
    assert OWNERSHIP_DENIED_FOREIGN_PATH in result.data
    assert executed == []


def test_t6_workspace_wide_destruction_is_denied() -> None:
    gate, executed, _ = _gate(OWNERSHIP_MODE_ENFORCE)
    for command in ("git reset --hard", "git clean -fd", "git stash"):
        result = asyncio.run(gate.execute("bash", {"command": command}, _context("a")))
        assert result.is_error is True, command
        assert OWNERSHIP_DENIED_WORKSPACE_WIDE in result.data
    assert executed == []


def test_t7_unclassifiable_shell_is_denied_not_silently_allowed() -> None:
    gate, executed, _ = _gate(OWNERSHIP_MODE_ENFORCE)
    result = asyncio.run(
        gate.execute(
            "bash",
            {"command": "python3 -c \"open('ledger/money.py','w').write('bad')\""},
            _context("a"),
        )
    )
    assert result.is_error is True
    assert OWNERSHIP_UNCLASSIFIABLE_SHELL in result.data
    assert executed == []


def test_t9_concurrent_workers_do_not_cross_identities() -> None:
    gate, executed, policy = _gate(OWNERSHIP_MODE_ENFORCE)

    async def run() -> list[ToolResult]:
        calls = []
        for _ in range(5):
            calls.append(
                gate.execute(
                    "file_write", {"path": "ledger/entries.py", "content": "a"}, _context("a")
                )
            )
            calls.append(
                gate.execute(
                    "file_write", {"path": "ledger/money.py", "content": "b"}, _context("b")
                )
            )
            calls.append(
                gate.execute(
                    "file_write", {"path": "ledger/money.py", "content": "x"}, _context("a")
                )
            )
            calls.append(
                gate.execute(
                    "file_write", {"path": "ledger/entries.py", "content": "y"}, _context("b")
                )
            )
        return await asyncio.gather(*calls)

    results = asyncio.run(run())
    allowed = [item for item in results if not item.is_error]
    denied = [item for item in results if item.is_error]
    assert len(allowed) == 10
    assert len(denied) == 10
    assert len(executed) == 10
    assert policy.evidence.denied_calls == 10
    assert all(OWNERSHIP_DENIED_FOREIGN_PATH in item.data for item in denied)


def test_unknown_worker_is_denied() -> None:
    gate, executed, _ = _gate(OWNERSHIP_MODE_ENFORCE)
    stranger = _context("a", run_id="other-run")
    result = asyncio.run(
        gate.execute("file_write", {"path": "ledger/entries.py", "content": "x"}, stranger)
    )
    assert result.is_error is True
    assert OWNERSHIP_UNKNOWN_WORKER in result.data
    assert executed == []


def test_t14_resume_revalidation_does_not_trust_an_old_allow() -> None:
    gate, executed, _ = _gate(OWNERSHIP_MODE_ENFORCE)
    allowed = asyncio.run(
        gate.evaluate("file_write", {"path": "ledger/entries.py", "content": "x"}, _context("a"))
    )
    assert allowed.outcome == "ALLOW"
    denied = asyncio.run(
        gate.evaluate(
            "file_write",
            {"path": "ledger/money.py", "content": "x"},
            _context("a"),
            trajectory_phase="resume_revalidation",
        )
    )
    assert denied.outcome == "DENY"
    result = asyncio.run(gate.execute_revalidated(denied, _context("a")))
    assert result.is_error is True
    assert executed == []


def test_t15_off_mode_installs_no_enforcer() -> None:
    enforcer, policy = build_ownership_enforcer(_manifest(), mode=OWNERSHIP_MODE_OFF)
    assert enforcer is None
    assert policy is None


def test_t16_audit_mode_changes_no_result() -> None:
    audit_gate, audit_calls, audit_policy = _gate(OWNERSHIP_MODE_AUDIT)
    result = asyncio.run(
        audit_gate.execute("bash", {"command": "git checkout -- ledger/money.py"}, _context("a"))
    )
    assert not result.is_error
    assert result.data == "bash:ok"
    assert len(audit_calls) == 1
    assert audit_policy.evidence.checked_calls == 1
    assert json.dumps(audit_policy.evidence_snapshot(), sort_keys=True)


def test_ownership_never_upgrades_an_m3_deny() -> None:
    gate, executed, policy = _gate(OWNERSHIP_MODE_ENFORCE)
    gate.policy = ActionPolicy(name="deny", default="DENY", rules=())
    result = asyncio.run(
        gate.execute("file_write", {"path": "ledger/entries.py", "content": "x"}, _context("a"))
    )
    assert result.is_error is True
    assert executed == []
    assert policy.evidence.checked_calls == 0
