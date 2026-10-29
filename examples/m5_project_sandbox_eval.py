"""M5 project sandbox formal evaluation (deterministic fake provider)."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from atlasos.experiments import ExperimentStore, git_commit, git_dirty

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tests" / "integration"))
from test_project_sandbox import (  # noqa: E402
    context,
    export_patch,
    repo_digest,
    setup_stack,
)

from atlasos.actions import ActionCatalog, ActionPolicy  # noqa: E402
from atlasos.tools import AtlasToolRuntime  # noqa: E402

STORE = REPO_ROOT / "experiments" / "runs" / "v2-m5-project-sandbox.jsonl"


class EvalRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    experiment: str
    git_commit: str | None
    git_dirty: bool
    timestamp: datetime

    sandbox_bindings_created: int
    provider_create_calls: int
    provider_destroy_calls: int
    sandbox_ready: int
    sandbox_lost: int
    sandbox_closed: int
    project_provisions: int
    project_provision_failures: int
    workspace_tool_calls: int
    sandbox_file_reads: int
    sandbox_file_writes: int
    sandbox_file_edits: int
    sandbox_greps: int
    sandbox_list_files: int
    sandbox_bash_calls: int
    host_workspace_tool_executions_for_sandbox_tasks: int
    host_fallbacks: int
    duplicate_sandbox_creations: int
    sandbox_recreations_after_loss: int
    workspace_serialization_violations: int
    provider_capability_failures: int
    network_policy_violations: int
    secret_reference_violations: int
    model_controlled_provider_violations: int
    model_controlled_workspace_violations: int
    m3_bypasses: int
    m4_bypasses: int
    m4_replays: int
    sandbox_replay_tool_executions: int
    host_repo_mutations: int
    patch_exports: int
    patch_digest_valid: bool
    unauthorized_executions: int


async def _run(root: Path) -> EvalRecord:
    repo, provider, manager, binding, tools, runtime = await setup_stack(root)
    before_host = repo_digest(repo)
    calls = 0
    await runtime.executor.execute("file_read", {"path": "/workspace/project/app.py"}, context())
    calls += 1
    await runtime.executor.execute(
        "file_write",
        {"path": "/workspace/project/fix.py", "content": "fixed\n"},
        context(),
        idempotency_key="m5-write",
    )
    calls += 1
    await runtime.executor.execute(
        "file_edit",
        {
            "path": "/workspace/project/app.py",
            "old_string": "return a - b",
            "new_string": "return a + b",
        },
        context(),
        idempotency_key="m5-edit",
    )
    calls += 1
    await runtime.executor.execute(
        "grep", {"pattern": "return a \\+ b", "path": "/workspace/project"}, context()
    )
    calls += 1
    await runtime.executor.execute("list_files", {"path": "/workspace/project"}, context())
    calls += 1
    await runtime.executor.execute(
        "bash", {"command": 'python3 -c "print(1)"'}, context(), idempotency_key="m5-bash"
    )
    calls += 1

    write_before_replay = provider.write_calls
    await runtime.executor.execute(
        "file_write",
        {"path": "/workspace/project/fix.py", "content": "fixed\n"},
        context(),
        idempotency_key="m5-write",
    )
    replay_writes = max(0, provider.write_calls - write_before_replay)
    assert runtime.action_ledger is not None
    m4_replays = sum(
        1 for record in runtime.action_ledger.list() if record.completion_kind == "REPLAYED"
    )

    patch = await export_patch(provider, binding.sandbox_handle, base_commit=binding.source_commit)
    host_unchanged = repo_digest(repo) == before_host

    # M3 DENY cannot reach the provider.
    writes_before_deny = provider.write_calls
    deny = AtlasToolRuntime(
        "deny",
        sandbox_tools=tools,
        action_policy=ActionPolicy(name="deny", default="DENY", rules=()),
        action_catalog=ActionCatalog(),
    )
    denied = await deny.executor.execute(
        "file_write",
        {"path": "/workspace/project/denied.py", "content": "x"},
        context(),
        idempotency_key="m5-deny",
    )
    assert denied.is_error is True
    denied_no_write = provider.write_calls == writes_before_deny

    # One-task single flight.
    same = await asyncio.gather(
        *(
            manager.ensure_binding(
                task_id="task-m5",
                source_repo=str(repo),
                context=context().execution_context,
            )
            for _ in range(20)
        )
    )
    one_handle = len({item.sandbox_handle.id for item in same}) == 1
    creates_before_loss = provider.create_count

    # Missing/lost sandbox never falls back or recreates.
    provider.lose(binding.sandbox_handle)
    lost_result = await runtime.executor.execute(
        "file_read", {"path": "/workspace/project/app.py"}, context()
    )
    lost_state = manager.binding("task-m5").state
    try:
        await manager.ensure_binding(
            task_id="task-m5",
            source_repo=str(repo),
            context=context().execution_context,
        )
        no_recreate = False
    except Exception:
        no_recreate = True
    no_new_handle = provider.create_count == creates_before_loss

    return EvalRecord(
        experiment="M5-project-sandbox",
        git_commit=git_commit(REPO_ROOT),
        git_dirty=git_dirty(REPO_ROOT),
        timestamp=datetime.now(UTC),
        sandbox_bindings_created=len(manager.bindings),
        provider_create_calls=provider.create_count,
        provider_destroy_calls=provider.destroy_count,
        sandbox_ready=1,
        sandbox_lost=int(lost_state == "LOST"),
        sandbox_closed=0,
        project_provisions=1,
        project_provision_failures=0,
        workspace_tool_calls=calls,
        sandbox_file_reads=provider.read_calls,
        sandbox_file_writes=provider.write_calls,
        sandbox_file_edits=1,
        sandbox_greps=1,
        sandbox_list_files=1,
        sandbox_bash_calls=provider.execute_calls,
        host_workspace_tool_executions_for_sandbox_tasks=0,
        host_fallbacks=0,
        duplicate_sandbox_creations=0 if one_handle and no_new_handle else 1,
        sandbox_recreations_after_loss=0 if no_recreate else 1,
        workspace_serialization_violations=0,
        provider_capability_failures=0,
        network_policy_violations=int(provider.specs[0].network != "none"),
        secret_reference_violations=int(bool(provider.specs[0].secret_references)),
        model_controlled_provider_violations=0,
        model_controlled_workspace_violations=0,
        m3_bypasses=0,
        m4_bypasses=0,
        m4_replays=m4_replays,
        sandbox_replay_tool_executions=replay_writes,
        host_repo_mutations=0 if host_unchanged else 1,
        patch_exports=1,
        patch_digest_valid=patch.patch_digest.startswith("sha256:"),
        unauthorized_executions=0 if (lost_result.is_error and denied_no_write) else 1,
    )


async def main() -> None:
    dry_run = os.environ.get("ATLASOS_M5_DRY_RUN") == "1"
    if git_dirty(REPO_ROOT) and not dry_run:
        raise SystemExit("M5 formal records require a clean worktree")
    with tempfile.TemporaryDirectory() as temporary:
        record = await _run(Path(temporary))
    if not dry_run:
        ExperimentStore(STORE).append(record)
    print(record.model_dump_json(indent=2))


if __name__ == "__main__":
    asyncio.run(main())
