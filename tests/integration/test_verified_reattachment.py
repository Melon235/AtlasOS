"""Stage B verified sandbox reattachment tests."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from tests.integration.test_project_sandbox import (
    context,
    exec_context,
    setup_stack,
)

from atlasos.sandbox import (
    ProjectSandboxManager,
    SandboxAttachFailed,
    SandboxBindingError,
    SandboxConfigMismatch,
    SandboxContinuityMismatch,
    SandboxCreatedAtMismatch,
    SandboxLegacyContinuityUnavailable,
    SandboxLostError,
    SandboxMetadataMismatch,
    SandboxNetworkPolicyUnavailable,
    SandboxNetworkPolicyViolation,
    SandboxProviderUnavailable,
    SandboxRemoteNotAttachable,
    SandboxWorkspaceTools,
    SQLiteSandboxBindingStore,
    compute_sandbox_continuity_digest,
)


def _manager_b(provider, manager, db_path: Path, *, config=None) -> ProjectSandboxManager:
    return ProjectSandboxManager(
        provider,
        config or manager.config,
        run_id=manager.run_id,
        binding_store=SQLiteSandboxBindingStore(db_path),
    )


async def test_b1_verified_reattachment_success(tmp_path: Path) -> None:
    db_path = tmp_path / "sandbox_bindings.db"
    store = SQLiteSandboxBindingStore(db_path)
    repo, provider, manager, binding, tools, runtime = await setup_stack(
        tmp_path, binding_store=store
    )
    del repo, runtime
    durable_before = store.get_for_task("run-m5", "task-m5")
    assert durable_before is not None
    before = compute_sandbox_continuity_digest(durable_before)
    await tools.file_write(
        type("V", (), {"path": "/workspace/project/marker.txt", "content": "preserved\n"})(),
        context(),
    )
    before_policy_reads = provider.network_policy_read_calls
    manager_b = _manager_b(provider, manager, db_path)
    reattached = await manager_b.reattach_verified(task_id="task-m5", context=exec_context())
    assert reattached.binding_id == binding.binding_id
    assert reattached.sandbox_handle.id == binding.sandbox_handle.id
    assert reattached.state == "READY"
    assert reattached.config_digest == binding.config_digest
    assert reattached.capabilities_digest == binding.capabilities_digest
    assert manager_b.require_ready("task-m5") is reattached
    assert provider.create_count == 1
    assert provider.attach_calls == 1
    assert provider.network_policy_read_calls == before_policy_reads + 1
    durable_after = store.get_for_task("run-m5", "task-m5")
    assert durable_after is not None
    assert durable_after.state == "READY"
    assert durable_after.revision == 1
    after = compute_sandbox_continuity_digest(durable_after)
    assert after == before

    new_tools = SandboxWorkspaceTools(manager_b, "task-m5", provider)
    read = await new_tools.file_read(
        type("V", (), {"path": "/workspace/project/marker.txt", "offset": None, "limit": None})(),
        context(),
    )
    assert read.is_error is False
    assert "preserved" in str(read.data)


async def test_b1a_allow_egress_blocks_reattachment_binding(tmp_path: Path) -> None:
    db_path = tmp_path / "sandbox_bindings.db"
    store = SQLiteSandboxBindingStore(db_path)
    repo, provider, manager, binding, tools, runtime = await setup_stack(
        tmp_path, binding_store=store
    )
    del repo, tools, runtime
    sid = binding.sandbox_handle.id
    provider.network_policies[sid] = {
        "default_action": "deny",
        "egress": [{"action": "allow", "target": "example.com"}],
    }
    manager_b = _manager_b(provider, manager, db_path)
    with pytest.raises(SandboxNetworkPolicyViolation, match="non-deny"):
        await manager_b.reattach_verified(task_id="task-m5", context=exec_context())
    durable = store.get(binding.binding_id)
    assert durable is not None
    assert durable.state == "READY"
    assert durable.revision == 1
    assert manager_b.bindings == {}
    assert provider.attach_calls == 1
    assert provider.create_count == 1


async def test_b1b_default_allow_blocks_reattachment_binding(tmp_path: Path) -> None:
    db_path = tmp_path / "sandbox_bindings.db"
    store = SQLiteSandboxBindingStore(db_path)
    repo, provider, manager, binding, tools, runtime = await setup_stack(
        tmp_path, binding_store=store
    )
    del repo, tools, runtime
    provider.network_policies[binding.sandbox_handle.id] = {
        "default_action": "allow",
        "egress": [],
    }
    manager_b = _manager_b(provider, manager, db_path)
    with pytest.raises(SandboxNetworkPolicyViolation, match="default action"):
        await manager_b.reattach_verified(task_id="task-m5", context=exec_context())
    durable = store.get(binding.binding_id)
    assert durable is not None
    assert durable.state == "READY"
    assert manager_b.bindings == {}
    assert provider.attach_calls == 1
    assert provider.create_count == 1


async def test_b1c_network_policy_read_failure_fails_closed(tmp_path: Path) -> None:
    db_path = tmp_path / "sandbox_bindings.db"
    store = SQLiteSandboxBindingStore(db_path)
    repo, provider, manager, binding, tools, runtime = await setup_stack(
        tmp_path, binding_store=store
    )
    del repo, tools, runtime
    provider.network_policy_error = SandboxNetworkPolicyUnavailable("policy read unavailable")
    manager_b = _manager_b(provider, manager, db_path)
    with pytest.raises(SandboxNetworkPolicyUnavailable, match="unavailable"):
        await manager_b.reattach_verified(task_id="task-m5", context=exec_context())
    durable = store.get(binding.binding_id)
    assert durable is not None
    assert durable.state == "READY"
    assert durable.revision == 1
    assert manager_b.bindings == {}
    assert provider.attach_calls == 1
    assert provider.create_count == 1


async def test_b2_lost_never_reattaches(tmp_path: Path) -> None:
    db_path = tmp_path / "sandbox_bindings.db"
    store = SQLiteSandboxBindingStore(db_path)
    repo, provider, manager, binding, tools, runtime = await setup_stack(
        tmp_path, binding_store=store
    )
    del repo, tools, runtime
    await manager.mark_lost("task-m5")
    manager_b = _manager_b(provider, manager, db_path)
    before_inspect = provider.inspect_calls
    with pytest.raises(SandboxLostError, match="LOST"):
        await manager_b.reattach_verified(task_id="task-m5", context=exec_context())
    assert provider.inspect_calls == before_inspect
    assert provider.attach_calls == 0
    assert provider.create_count == 1
    durable_lost = store.get(binding.binding_id)
    assert durable_lost is not None
    assert durable_lost.state == "LOST"


async def test_b3_closed_never_reattaches(tmp_path: Path) -> None:
    db_path = tmp_path / "sandbox_bindings.db"
    store = SQLiteSandboxBindingStore(db_path)
    repo, provider, manager, binding, tools, runtime = await setup_stack(
        tmp_path, binding_store=store
    )
    del repo, binding, tools, runtime
    await manager.close("task-m5")
    manager_b = _manager_b(provider, manager, db_path)
    with pytest.raises(SandboxBindingError, match="CLOSED"):
        await manager_b.reattach_verified(task_id="task-m5", context=exec_context())
    assert provider.attach_calls == 0
    assert provider.create_count == 1


async def test_b4_control_plane_mismatch_never_inspects_or_attaches(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "sandbox_bindings.db"
    store = SQLiteSandboxBindingStore(db_path)
    repo, provider, manager, binding, tools, runtime = await setup_stack(
        tmp_path, binding_store=store
    )
    del repo, binding, tools, runtime
    provider.control_plane_digest = "sha256:other-control-plane"
    manager_b = _manager_b(provider, manager, db_path)
    before_inspect = provider.inspect_calls
    with pytest.raises(SandboxContinuityMismatch, match="control-plane"):
        await manager_b.reattach_verified(task_id="task-m5", context=exec_context())
    assert provider.inspect_calls == before_inspect
    assert provider.attach_calls == 0
    assert provider.create_count == 1
    durable_ready = store.get_for_task("run-m5", "task-m5")
    assert durable_ready is not None
    assert durable_ready.state == "READY"


async def test_b5_config_mismatch_never_inspects_or_attaches(tmp_path: Path) -> None:
    db_path = tmp_path / "sandbox_bindings.db"
    store = SQLiteSandboxBindingStore(db_path)
    repo, provider, manager, binding, tools, runtime = await setup_stack(
        tmp_path, binding_store=store
    )
    del repo, binding, tools, runtime
    different = manager.config.model_copy(update={"image": "fake@sha256:different"})
    manager_b = _manager_b(provider, manager, db_path, config=different)
    before_inspect = provider.inspect_calls
    with pytest.raises(SandboxConfigMismatch, match="config"):
        await manager_b.reattach_verified(task_id="task-m5", context=exec_context())
    assert provider.inspect_calls == before_inspect
    assert provider.attach_calls == 0
    assert provider.create_count == 1


async def test_b6_metadata_mismatch_never_attaches(tmp_path: Path) -> None:
    db_path = tmp_path / "sandbox_bindings.db"
    store = SQLiteSandboxBindingStore(db_path)
    repo, provider, manager, binding, tools, runtime = await setup_stack(
        tmp_path, binding_store=store
    )
    del repo, tools, runtime
    provider.remote_metadata[binding.sandbox_handle.id]["atlas.run_id"] = "other-run"
    manager_b = _manager_b(provider, manager, db_path)
    with pytest.raises(SandboxMetadataMismatch, match="metadata"):
        await manager_b.reattach_verified(task_id="task-m5", context=exec_context())
    assert provider.attach_calls == 0
    assert provider.create_count == 1
    durable_ready = store.get(binding.binding_id)
    assert durable_ready is not None
    assert durable_ready.state == "READY"


async def test_b7_created_at_mismatch_never_attaches(tmp_path: Path) -> None:
    db_path = tmp_path / "sandbox_bindings.db"
    store = SQLiteSandboxBindingStore(db_path)
    repo, provider, manager, binding, tools, runtime = await setup_stack(
        tmp_path, binding_store=store
    )
    del repo, tools, runtime
    sid = binding.sandbox_handle.id
    provider.remote_created_at[sid] = provider.remote_created_at[sid] + timedelta(seconds=1)
    manager_b = _manager_b(provider, manager, db_path)
    with pytest.raises(SandboxCreatedAtMismatch, match="created_at"):
        await manager_b.reattach_verified(task_id="task-m5", context=exec_context())
    assert provider.attach_calls == 0
    assert provider.create_count == 1
    durable_ready = store.get(binding.binding_id)
    assert durable_ready is not None
    assert durable_ready.state == "READY"


async def test_b8a_terminal_provider_state_marks_durable_lost(tmp_path: Path) -> None:
    db_path = tmp_path / "sandbox_bindings.db"
    store = SQLiteSandboxBindingStore(db_path)
    repo, provider, manager, binding, tools, runtime = await setup_stack(
        tmp_path, binding_store=store
    )
    del repo, tools, runtime
    provider.remote_state[binding.sandbox_handle.id] = "TERMINATED"
    manager_b = _manager_b(provider, manager, db_path)
    with pytest.raises(SandboxLostError, match="LOST"):
        await manager_b.reattach_verified(task_id="task-m5", context=exec_context())
    durable = store.get(binding.binding_id)
    assert durable is not None
    assert durable.state == "LOST"
    assert provider.attach_calls == 0
    assert provider.create_count == 1


async def test_b8b_non_attachable_state_preserves_durable_ready(tmp_path: Path) -> None:
    db_path = tmp_path / "sandbox_bindings.db"
    store = SQLiteSandboxBindingStore(db_path)
    repo, provider, manager, binding, tools, runtime = await setup_stack(
        tmp_path, binding_store=store
    )
    del repo, tools, runtime
    provider.remote_state[binding.sandbox_handle.id] = "PAUSED"
    manager_b = _manager_b(provider, manager, db_path)
    with pytest.raises(SandboxRemoteNotAttachable, match="PAUSED"):
        await manager_b.reattach_verified(task_id="task-m5", context=exec_context())
    durable = store.get(binding.binding_id)
    assert durable is not None
    assert durable.state == "READY"
    assert provider.attach_calls == 0
    assert provider.create_count == 1


async def test_b8_remote_not_found_marks_durable_lost(tmp_path: Path) -> None:
    db_path = tmp_path / "sandbox_bindings.db"
    store = SQLiteSandboxBindingStore(db_path)
    repo, provider, manager, binding, tools, runtime = await setup_stack(
        tmp_path, binding_store=store
    )
    del repo, tools, runtime
    sid = binding.sandbox_handle.id
    provider.remote_state.pop(sid)
    manager_b = _manager_b(provider, manager, db_path)
    with pytest.raises(SandboxLostError, match="missing"):
        await manager_b.reattach_verified(task_id="task-m5", context=exec_context())
    durable = store.get(binding.binding_id)
    assert durable is not None
    assert durable.state == "LOST"
    assert durable.revision == 2
    assert provider.attach_calls == 0
    assert provider.create_count == 1


async def test_b9_provider_unavailable_keeps_durable_ready(tmp_path: Path) -> None:
    db_path = tmp_path / "sandbox_bindings.db"
    store = SQLiteSandboxBindingStore(db_path)
    repo, provider, manager, binding, tools, runtime = await setup_stack(
        tmp_path, binding_store=store
    )
    del repo, tools, runtime
    provider.inspect_error = SandboxProviderUnavailable("temporary control-plane outage")
    manager_b = _manager_b(provider, manager, db_path)
    with pytest.raises(SandboxProviderUnavailable, match="outage"):
        await manager_b.reattach_verified(task_id="task-m5", context=exec_context())
    durable = store.get(binding.binding_id)
    assert durable is not None
    assert durable.state == "READY"
    assert durable.revision == 1
    assert provider.attach_calls == 0
    assert provider.create_count == 1
    assert manager_b.bindings == {}


async def test_b10_attach_failure_keeps_durable_ready(tmp_path: Path) -> None:
    db_path = tmp_path / "sandbox_bindings.db"
    store = SQLiteSandboxBindingStore(db_path)
    repo, provider, manager, binding, tools, runtime = await setup_stack(
        tmp_path, binding_store=store
    )
    del repo, binding, tools, runtime
    provider.attach_error = SandboxAttachFailed("transient connect failure")
    manager_b = _manager_b(provider, manager, db_path)
    with pytest.raises(SandboxAttachFailed, match="transient"):
        await manager_b.reattach_verified(task_id="task-m5", context=exec_context())
    durable = store.get_for_task("run-m5", "task-m5")
    assert durable is not None
    assert durable.state == "READY"
    assert provider.attach_calls == 1
    assert provider.create_count == 1
    assert manager_b.bindings == {}


async def test_b11_legacy_binding_without_evidence_fails_closed(tmp_path: Path) -> None:
    db_path = tmp_path / "sandbox_bindings.db"
    source_store = SQLiteSandboxBindingStore(tmp_path / "source.db")
    repo, provider, manager, binding, tools, runtime = await setup_stack(
        tmp_path, binding_store=source_store
    )
    del repo, tools, runtime
    durable = source_store.get(binding.binding_id)
    assert durable is not None
    legacy = durable.model_copy(
        update={
            "provider_control_plane_digest": None,
            "provider_created_at": None,
            "provider_metadata_digest": None,
        }
    )
    legacy_store = SQLiteSandboxBindingStore(db_path)
    legacy_store.create(legacy)
    manager_b = _manager_b(provider, manager, db_path)
    before_inspect = provider.inspect_calls
    with pytest.raises(SandboxLegacyContinuityUnavailable, match="continuity"):
        await manager_b.reattach_verified(task_id="task-m5", context=exec_context())
    assert provider.inspect_calls == before_inspect
    assert provider.attach_calls == 0
    assert provider.create_count == 1


async def test_b_local_detach_does_not_destroy_remote(tmp_path: Path) -> None:
    store = SQLiteSandboxBindingStore(tmp_path / "sandbox_bindings.db")
    repo, provider, manager, binding, tools, runtime = await setup_stack(
        tmp_path, binding_store=store
    )
    del repo, manager, tools, runtime
    sid = binding.sandbox_handle.id
    await provider.attach_existing(sid)
    await provider.detach_local(sid)
    assert sid not in provider.attached
    assert provider.destroy_count == 0
    assert provider.remote_state[sid] == "RUNNING"
    assert sid in provider.workspaces
