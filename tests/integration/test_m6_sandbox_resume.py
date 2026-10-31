"""M6 x M5: a lost sandbox can never be replaced to resume an approved action."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from anycode.identity.context import ExecutionContext
from anycode.types import AgentInfo, ToolSecurityPolicy, ToolUseContext
from tests.helpers.m6_harness import M6World

from atlasos.actions import ActionPolicy, ActionRule
from atlasos.sandbox import (
    ProjectSandboxBinding,
    ProjectSandboxManager,
    SandboxLostError,
    SandboxProjectConfig,
    SandboxWorkspaceTools,
)
from atlasos.sandbox.models import trusted_sandbox_spec


async def test_m6_11_m5_lost_sandbox_fails_closed(tmp_path: Path) -> None:
    from tests.helpers.fake_sandbox_provider import FakeSandboxProvider

    provider = FakeSandboxProvider(tmp_path / "provider")
    config = SandboxProjectConfig(
        provider_name="fake",
        image="fake@sha256:0123456789abcdef",
    )
    manager = ProjectSandboxManager(provider, config, run_id="run-m6")
    execution_context = ExecutionContext(
        principal="user:test",
        tenant_scope="tenant-a",
        classification="internal",
        attributes={
            "run_id": "run-m6",
            "task_id": "task-m6",
            "worker_id": "worker-m6",
            "profile_id": "coding_worker",
            "operating_scope": "coding",
            "worker_profile": "coding_worker",
        },
    )
    spec = trusted_sandbox_spec(
        run_id="run-m6",
        task_id="task-m6",
        worker_id="worker-m6",
        context=execution_context,
        config=config,
    )
    created = await provider.create(spec)
    assert created.ok and created.handle is not None
    binding = ProjectSandboxBinding(
        run_id="run-m6",
        task_id="task-m6",
        worker_id="worker-m6",
        provider_name="fake",
        sandbox_handle=created.handle,
        workspace_root="/workspace/project",
        source_commit="0" * 40,
        source_digest="sha256:test",
        bundle_digest="sha256:test",
        state="READY",
        created_at=datetime.now(UTC),
    )
    manager.bindings["task-m6"] = binding
    tools = SandboxWorkspaceTools(manager, "task-m6", provider)
    policy = ActionPolicy(
        name="m6-sandbox",
        default="REQUIRE_HUMAN",
        rules=(
            ActionRule(id="allow-read", decision="ALLOW", action="filesystem.read"),
            ActionRule(id="allow-search", decision="ALLOW", action="control.search"),
            ActionRule(id="human-write", decision="REQUIRE_HUMAN", action="filesystem.write"),
        ),
    )
    world = M6World(tmp_path / "world")
    runtime = world.runtime(policy=policy, sandbox_tools=tools)
    context = ToolUseContext(
        agent=AgentInfo(name="coding_worker:worker-m6", role="worker", model="fake"),
        execution_context=execution_context,
        security_policy=ToolSecurityPolicy(workspace_root="/workspace/project"),
    )
    calls = [
        world.call(
            0,
            "write-call",
            "file_write",
            {"path": "/workspace/project/fix.py", "content": "fixed\n"},
        )
    ]
    suspension = await world.suspend(runtime, calls, context=context)
    assert suspension.sandbox_required is True
    assert suspension.sandbox_binding_digest is not None
    before_writes = provider.write_calls
    assert before_writes == 0

    world.approve(runtime, suspension)
    provider.lose(binding.sandbox_handle)
    await manager.mark_lost("task-m6")
    outcome = await world.resume(runtime, suspension, context=context)
    assert outcome.status == "stale"
    assert "M5" in (outcome.reason or "")
    assert provider.write_calls == before_writes
    assert runtime.m6_metrics is not None
    assert runtime.m6_metrics.m5_resume_violations == 1
    assert (
        provider.workspaces.get(binding.sandbox_handle.id, tmp_path / "absent")
        / "workspace/project/fix.py"
    ).exists() is False
    with pytest.raises(SandboxLostError):
        await manager.ensure_binding(
            task_id="task-m6",
            source_repo=str(tmp_path / "unused"),
            context=execution_context,
            worker_id="worker-m6",
        )
    assert provider.create_count == 1
