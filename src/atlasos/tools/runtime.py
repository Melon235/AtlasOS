"""Unified Atlas tool runtime.

One AnyCode ``ToolRegistry`` contains every executable tool. ``ToolCatalog`` decides which are
native-visible; ``ToolSearchIndex`` powers ``tool.search``; ``RunToolState`` gates
``dispatch_action``. Real execution is always delegated to AnyCode's ``ToolExecutor``.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import time
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from anycode.hitl.approval import ApprovalManager
from anycode.identity.policy import PolicyEnforcer
from anycode.mcp.bridge import discover_and_register
from anycode.mcp.client import MCPClient
from anycode.tools.built_in import BUILT_IN_TOOLS
from anycode.tools.executor import ToolExecutor
from anycode.tools.idempotency import InMemoryToolIdempotencyStore
from anycode.types import (
    LLMToolDef,
    MCPServerConfig,
    ToolDefinition,
    ToolResult,
    ToolUseContext,
)
from pydantic import BaseModel, ConfigDict

from atlasos.actions import (
    ActionCatalog,
    ActionPolicy,
    ActionReviewer,
    ActionSafetyGate,
    canonical_digest,
    infer_tool_source,
)
from atlasos.actions import (
    tool_binding_digest as compute_tool_binding_digest,
)
from atlasos.approvals.coordinator import ApprovalCoordinator
from atlasos.approvals.store import SQLiteApprovalStore
from atlasos.ledger import ActionLedgerStore, DurableActionBridge
from atlasos.sandbox.tools import SandboxWorkspaceTools
from atlasos.suspension.batch import BatchCall, BatchPreflight
from atlasos.suspension.coordinator import RuntimeSuspensionCoordinator
from atlasos.suspension.metrics import M6Metrics
from atlasos.suspension.store import SQLiteSuspensionStore
from atlasos.tools.catalog import (
    CORE_TOOL_DISPATCH,
    CORE_TOOL_SEARCH,
    ToolCatalog,
)
from atlasos.tools.conversation import ConversationSnapshotHook, ConversationStore
from atlasos.tools.dispatch import (
    AtlasDispatchExecutor,
    DispatchActionInput,
    ToolSearchInput,
)
from atlasos.tools.registry import ProviderSafeToolRegistry
from atlasos.tools.search import (
    ToolSearchIndex,
    ToolSearchMatch,
    ToolSearchUnavailable,
    build_search_index,
)
from atlasos.tools.state import RunToolStateStore
from atlasos.trajectory.payloads import (
    EXECUTION_INTEGRITY_MODES,
    EXECUTION_INTEGRITY_OFF,
    ToolPayloadCorpus,
)

if TYPE_CHECKING:
    from atlasos.tool_result_offload import ToolResultIngressRuntime
    from atlasos.trajectory.recorder import TrajectoryRecorder


class ToolSearchObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    query: str
    returned_tool_ids: tuple[str, ...]
    latency_ms: float


class ToolDispatchObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_id: str
    disclosed: bool
    available: bool
    success: bool = False
    error_code: str | None = None


class ToolRuntimeMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    native_tool_names: tuple[str, ...]
    native_provider_tool_names: tuple[str, ...]
    native_tool_surface_hash: str
    native_tool_schema_chars: int

    search_count: int
    dispatch_count: int
    dispatch_failure_count: int
    search_latency_ms: float

    disclosed_tool_names: tuple[str, ...]
    searches: tuple[ToolSearchObservation, ...]
    dispatches: tuple[ToolDispatchObservation, ...]
    mcp_servers: tuple[str, ...]


class AtlasToolRuntime:
    """Run-scoped tool catalog, search index, disclosure state, and executor."""

    def __init__(
        self,
        run_id: str,
        *,
        state_path: str | Path | None = None,
        conversation_path: str | Path | None = None,
        external_tools: Sequence[ToolDefinition] = (),
        search_index: ToolSearchIndex | None = None,
        search_backend: str = "keyword",
        embedding_model: str | None = None,
        max_results: int = 3,
        idempotency_store: Any | None = None,
        action_policy: ActionPolicy | None = None,
        action_catalog: ActionCatalog | None = None,
        action_reviewer: ActionReviewer | None = None,
        action_approval_manager: ApprovalManager | None = None,
        action_external_policy: PolicyEnforcer | None = None,
        action_ledger: ActionLedgerStore | None = None,
        runtime_instance_id: str | None = None,
        sandbox_tools: SandboxWorkspaceTools | None = None,
        suspension_store: SQLiteSuspensionStore | None = None,
        approval_store: SQLiteApprovalStore | None = None,
        trajectory_recorder: TrajectoryRecorder | None = None,
        tool_result_offloader: ToolResultIngressRuntime | None = None,
        execution_integrity_mode: str = EXECUTION_INTEGRITY_OFF,
        tool_payload_corpus: ToolPayloadCorpus | None = None,
        external_denial_retry_safe: bool = False,
    ) -> None:
        if action_ledger is not None and action_policy is None:
            raise ValueError("action_ledger requires action_policy; M4 runs only after M3")
        m6_enabled = suspension_store is not None or approval_store is not None
        if m6_enabled and (suspension_store is None or approval_store is None):
            raise ValueError("M6 requires both suspension_store and approval_store")
        if m6_enabled and action_policy is None:
            raise ValueError("M6 requires action_policy; M6 runs only with M3")
        if m6_enabled and action_ledger is None:
            raise ValueError("M6 requires action_ledger; M6 settlement requires M4")
        if m6_enabled and action_approval_manager is not None:
            raise ValueError("M6 replaces current-run HITL; action_approval_manager must be None")
        if max_results < 1:
            raise ValueError("max_results must be >= 1")
        if execution_integrity_mode not in EXECUTION_INTEGRITY_MODES:
            raise ValueError(f"unknown execution integrity mode {execution_integrity_mode!r}")
        if tool_payload_corpus is not None and execution_integrity_mode == EXECUTION_INTEGRITY_OFF:
            raise ValueError("tool_payload_corpus requires execution_integrity_mode='record'")
        self.execution_integrity_mode = execution_integrity_mode
        self.tool_payload_corpus = tool_payload_corpus
        self.run_id = run_id
        self.runtime_instance_id = runtime_instance_id or f"runtime-{uuid.uuid4()}"
        self.action_ledger = action_ledger
        self.durable_bridge: DurableActionBridge | None = None
        self.max_results = max_results
        self.registry = ProviderSafeToolRegistry()
        self.state_store = RunToolStateStore(state_path)
        self.state = self.state_store.load(run_id=run_id)
        self.conversation_store = ConversationStore(conversation_path, run_id=run_id)
        self._search_backend = search_backend
        self._embedding_model = embedding_model
        self._custom_index = search_index is not None
        self._search_observations: list[ToolSearchObservation] = []
        self._dispatch_observations: list[ToolDispatchObservation] = []
        self._mcp_clients: dict[str, MCPClient] = {}
        self._mcp_servers: list[str] = []
        self._sandbox_tools = sandbox_tools
        self.suspension_store = suspension_store
        self.approval_store = approval_store
        self.trajectory_recorder = trajectory_recorder
        self.m6_metrics = M6Metrics() if m6_enabled else None
        self.approval_coordinator: ApprovalCoordinator | None = None
        self.suspension_coordinator: RuntimeSuspensionCoordinator | None = None

        self._tool_sources: dict[str, str] = {}
        atlas_tools = self._build_atlas_tools()
        core_defs = {tool.name: tool for tool in BUILT_IN_TOOLS}
        self._tool_sources.update({tool.name: "anycode:builtin" for tool in BUILT_IN_TOOLS})
        if sandbox_tools is not None:
            sandbox_defs = sandbox_tools.tool_definitions()
            core_defs.update({tool.name: tool for tool in sandbox_defs})
            self._tool_sources.update({tool.name: "atlas:sandbox" for tool in sandbox_defs})
        core_defs.update({tool.name: tool for tool in atlas_tools})
        self._tool_sources.update({tool.name: "atlas:control" for tool in atlas_tools})
        self.catalog = ToolCatalog(core_tool_names=frozenset(core_defs))
        for name in sorted(core_defs):
            self.registry.register(core_defs[name])

        self._register_searchable_tools(external_tools)
        for external in external_tools:
            if external.name not in self._tool_sources:
                self._tool_sources[external.name] = infer_tool_source(external)
        self._index = search_index or build_search_index(
            self.registry,
            self.catalog,
            backend=search_backend,
            model_name=embedding_model,
        )
        raw_executor = ToolExecutor(
            self.registry,
            idempotency_store=idempotency_store or InMemoryToolIdempotencyStore(),
        )
        base_executor: Any = raw_executor
        if action_ledger is not None:
            self.durable_bridge = DurableActionBridge(
                raw_executor,
                self.registry,
                action_ledger,
                runtime_instance_id=self.runtime_instance_id,
                catalog=action_catalog,
                trajectory_recorder=trajectory_recorder,
            )
            base_executor = self.durable_bridge
        self.action_safety_gate: ActionSafetyGate | None = None
        if action_policy is not None:
            self.action_safety_gate = ActionSafetyGate(
                base_executor,
                self.registry,
                policy=action_policy,
                catalog=action_catalog,
                reviewer=action_reviewer,
                approval_manager=action_approval_manager,
                external_policy=action_external_policy,
                tool_sources=self._tool_sources,
                trajectory_recorder=trajectory_recorder,
                external_denial_retry_safe=external_denial_retry_safe,
            )
            self._inner_executor = cast(Any, self.action_safety_gate)
        else:
            self._inner_executor = base_executor
        self.executor = AtlasDispatchExecutor(
            cast(ToolExecutor, self._inner_executor),
            self,
            offloader=tool_result_offloader,
        )
        if m6_enabled:
            assert suspension_store is not None and approval_store is not None
            assert self.m6_metrics is not None
            self.approval_coordinator = ApprovalCoordinator(
                approval_store,
                metrics=self.m6_metrics,
                trajectory_recorder=trajectory_recorder,
            )
            self.suspension_coordinator = RuntimeSuspensionCoordinator(
                self,
                suspension_store,
                self.approval_coordinator,
                run_id=run_id,
                runtime_instance_id=self.runtime_instance_id,
                trajectory_recorder=trajectory_recorder,
            )

    # -- tool construction --

    def _build_atlas_tools(self) -> tuple[ToolDefinition, ...]:
        async def search_tool(
            validated: ToolSearchInput,
            context: ToolUseContext,
        ) -> ToolResult:
            del context
            return await self.search(validated.query)

        async def dispatch_tool(
            validated: DispatchActionInput,
            context: ToolUseContext,
        ) -> ToolResult:
            return await self.dispatch(
                validated.tool_id,
                validated.arguments,
                context,
                idempotency_key=context.idempotency_key,
            )

        return (
            ToolDefinition(
                name=CORE_TOOL_SEARCH,
                description=(
                    "Search the external tool catalog for a capability. Returns up to three "
                    "matching tool schemas; matched tools become dispatchable for this run."
                ),
                input_model=ToolSearchInput,
                execute=search_tool,
                side_effecting=False,
                idempotency_key_field=None,
            ),
            ToolDefinition(
                name=CORE_TOOL_DISPATCH,
                description=(
                    "Invoke a previously disclosed external tool by tool_id with a JSON object "
                    "of arguments. Tools must be disclosed by tool.search first."
                ),
                input_model=DispatchActionInput,
                execute=dispatch_tool,
                side_effecting=False,
                idempotency_key_field=None,
            ),
        )

    def _register_searchable_tools(self, tools: Sequence[ToolDefinition]) -> None:
        new_names: set[str] = set()
        for tool in sorted(tools, key=lambda item: item.name):
            if tool.name in self.catalog.core_tool_names:
                raise ValueError(f"searchable tool {tool.name!r} collides with a core tool")
            if self.registry.has(tool.name):
                raise ValueError(f"tool {tool.name!r} is already registered")
            self.registry.register(tool)
            new_names.add(tool.name)
        if new_names:
            self.catalog = self.catalog.with_searchable(frozenset(new_names))

    def replace_search_index(self, index: ToolSearchIndex) -> None:
        """Test/experiment hook: install an explicit deterministic search index."""

        self._custom_index = True
        self._index = index

    def _rebuild_index(self) -> None:
        if self._custom_index:
            return
        self._index = build_search_index(
            self.registry,
            self.catalog,
            backend=self._search_backend,
            model_name=self._embedding_model,
        )

    # -- MCP lifecycle --

    async def add_mcp_server(self, config: MCPServerConfig) -> tuple[str, ...]:
        client = MCPClient(config)
        await client.connect()
        try:
            names = await discover_and_register(client, config.name, self.registry)
        except BaseException:
            await client.disconnect()
            raise
        searchable = frozenset(name for name in names if name not in self.catalog.core_tool_names)
        self.catalog = self.catalog.with_searchable(searchable)
        for name in names:
            self._tool_sources.setdefault(name, f"mcp:{config.name}")
        self._mcp_clients[config.name] = client
        self._mcp_servers.append(config.name)
        self._rebuild_index()
        self.catalog.validate(self.registry)
        return tuple(sorted(searchable))

    async def connect_mcp_servers(self, configs: Sequence[MCPServerConfig]) -> None:
        for config in configs:
            await self.add_mcp_server(config)

    async def close(self) -> None:
        for client in self._mcp_clients.values():
            with contextlib.suppress(Exception):
                await client.disconnect()
        self._mcp_clients.clear()

    # -- M6 binding / suspension helpers --

    def tool_source(self, logical_name: str) -> str:
        tool = self.registry.get(logical_name)
        if tool is None:
            raise KeyError(f"tool {logical_name!r} is not registered")
        return self._tool_sources.get(logical_name) or infer_tool_source(tool)

    def tool_binding_digest(self, logical_name: str) -> str:
        tool = self.registry.get(logical_name)
        if tool is None:
            raise KeyError(f"tool {logical_name!r} is not registered")
        provider_name = self.registry.provider_name(logical_name)
        return compute_tool_binding_digest(
            tool,
            source=self.tool_source(logical_name),
            provider_name=provider_name,
        )

    def sandbox_binding_digest(self, task_id: str | None) -> str | None:
        if task_id is None or self._sandbox_tools is None:
            return None
        manager = self._sandbox_tools.manager
        if not manager.has_binding(task_id):
            return None
        return manager.continuity_digest(task_id)

    def sandbox_required(self, task_id: str | None) -> bool:
        if task_id is None or self._sandbox_tools is None:
            return False
        return self._sandbox_tools.task_id == task_id or self._sandbox_tools.manager.has_binding(
            task_id
        )

    async def preflight_batch(
        self,
        calls: Sequence[BatchCall],
        context: ToolUseContext,
        *,
        model_response_id: str | None = None,
        execution_scope: str | None = None,
    ) -> BatchPreflight:
        if self.suspension_coordinator is None:
            raise RuntimeError("M6 is not enabled on this AtlasToolRuntime")
        return await self.suspension_coordinator.preflight_batch(
            calls,
            context,
            model_response_id=model_response_id,
            execution_scope=execution_scope,
        )

    def m6_metrics_snapshot(self) -> dict[str, int] | None:
        if self.m6_metrics is None:
            return None
        return self.m6_metrics.snapshot()

    # -- native surface --

    def native_tool_defs(self) -> tuple[LLMToolDef, ...]:
        definitions = {
            logical: definition
            for definition in self.registry.to_tool_defs()
            if (logical := self.registry.logical_name(definition.name)) is not None
        }
        return tuple(definitions[name] for name in self.catalog.native_tool_names)

    @property
    def native_tool_names(self) -> tuple[str, ...]:
        return self.catalog.native_tool_names

    @property
    def native_provider_tool_names(self) -> tuple[str, ...]:
        return tuple(definition.name for definition in self.native_tool_defs())

    @property
    def agent_tools(self) -> tuple[str, ...]:
        return self.native_provider_tool_names

    def logical_tool_name(self, provider_name: str) -> str | None:
        return self.registry.logical_name(provider_name)

    def native_surface_hash(self) -> str:
        payload = [
            {
                "name": definition.name,
                "description": definition.description,
                "input_schema": definition.input_schema,
            }
            for definition in self.native_tool_defs()
        ]
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def native_schema_chars(self) -> int:
        payload = [
            {
                "name": definition.name,
                "description": definition.description,
                "input_schema": definition.input_schema,
            }
            for definition in self.native_tool_defs()
        ]
        return len(json.dumps(payload, sort_keys=True, separators=(",", ":")))

    # -- search / disclosure --

    async def search(self, query: str, *, top_k: int | None = None) -> ToolResult:
        started = time.perf_counter()
        limit = min(top_k or self.max_results, self.max_results)
        try:
            matches = self._index.search(query, top_k=limit)
        except ToolSearchUnavailable as error:
            return ToolResult(data=f"tool_search_unavailable: {error}", is_error=True)
        selected: list[tuple[str, ToolDefinition]] = []
        for match in matches:
            tool = self.registry.get(match.tool_id)
            if tool is not None:
                selected.append((match.tool_id, tool))
        names = {tool_id for tool_id, _ in selected}
        already = set(self.state.sorted_names)
        changed = self.state.disclose(names)
        if changed:
            self.state_store.save(self.state, run_id=self.run_id)
        recorder = self.trajectory_recorder
        if recorder is not None and changed:
            from atlasos.trajectory.context import current_call_trajectory
            from atlasos.trajectory.models import TrajectoryCorrelation

            call_context = current_call_trajectory()
            for tool_id in sorted(names - already):
                tool = self.registry.get(tool_id)
                if tool is None:
                    continue
                try:  # noqa: SIM105
                    recorder.record_tool_disclosed(
                        tool_id=tool_id,
                        tool_name=tool.name,
                        disclosure_source="tool_search",
                        query_digest=canonical_digest(query),
                        correlation=TrajectoryCorrelation(
                            model_response_id=(
                                call_context.model_response_id if call_context is not None else None
                            ),
                            call_id=(call_context.call_id if call_context is not None else None),
                            tool_id=tool_id,
                        ),
                    )
                except Exception:  # noqa: BLE001 - evidence is optional
                    pass
        latency_ms = (time.perf_counter() - started) * 1000.0
        self._search_observations.append(
            ToolSearchObservation(
                query=query,
                returned_tool_ids=tuple(tool_id for tool_id, _ in selected),
                latency_ms=latency_ms,
            )
        )
        schemas = {entry["name"]: entry["input_schema"] for entry in self.registry.to_llm_tools()}
        payload = {
            "matches": [
                {
                    "tool_id": tool_id,
                    "description": tool.description,
                    "input_schema": schemas[tool_id],
                }
                for tool_id, tool in selected
            ]
        }
        return ToolResult(data=json.dumps(payload, sort_keys=True), is_error=False)

    # -- dispatch --

    async def dispatch(
        self,
        tool_id: str,
        arguments: dict[str, Any],
        context: ToolUseContext,
        *,
        idempotency_key: str | None = None,
    ) -> ToolResult:
        disclosed = self.state.is_dispatchable(tool_id)
        available = self.registry.has(tool_id)
        if not disclosed:
            result = ToolResult(
                data=f"tool_not_disclosed: {tool_id!r} has not been disclosed by tool.search",
                is_error=True,
            )
            self._record_dispatch(tool_id, disclosed, available, result)
            return result
        if not available:
            result = ToolResult(
                data=f"tool_unavailable: {tool_id!r} is disclosed for this run but unavailable",
                is_error=True,
            )
            self._record_dispatch(tool_id, disclosed, available, result)
            return result
        if tool_id == CORE_TOOL_DISPATCH:
            result = ToolResult(
                data="tool_unavailable: dispatch_action cannot dispatch itself",
                is_error=True,
            )
            self._record_dispatch(tool_id, disclosed, available, result)
            return result
        result = await self._inner_executor.execute(
            tool_id,
            arguments,
            context,
            idempotency_key=idempotency_key,
        )
        self._record_dispatch(tool_id, disclosed, available, result)
        return result

    def _record_dispatch(
        self,
        tool_id: str,
        disclosed: bool,
        available: bool,
        result: ToolResult,
    ) -> None:
        code: str | None = None
        if result.is_error:
            text = result.data
            code = text.split(":", 1)[0]
        self._dispatch_observations.append(
            ToolDispatchObservation(
                tool_id=tool_id,
                disclosed=disclosed,
                available=available,
                success=not bool(result.is_error),
                error_code=code,
            )
        )

    # -- observations --

    def metrics(self) -> ToolRuntimeMetrics:
        return ToolRuntimeMetrics(
            native_tool_names=self.native_tool_names,
            native_provider_tool_names=self.native_provider_tool_names,
            native_tool_surface_hash=self.native_surface_hash(),
            native_tool_schema_chars=self.native_schema_chars(),
            search_count=len(self._search_observations),
            dispatch_count=len(self._dispatch_observations),
            dispatch_failure_count=sum(
                1 for observation in self._dispatch_observations if not observation.success
            ),
            search_latency_ms=sum(
                observation.latency_ms for observation in self._search_observations
            ),
            disclosed_tool_names=self.state.sorted_names,
            searches=tuple(self._search_observations),
            dispatches=tuple(self._dispatch_observations),
            mcp_servers=tuple(sorted(self._mcp_servers)),
        )

    # -- test/experiment helpers --

    def disclosure_snapshot(self) -> dict[str, Any]:
        return self.state.snapshot(run_id=self.run_id)

    def conversation_hook(self) -> ConversationSnapshotHook:
        return ConversationSnapshotHook(self.conversation_store)

    def search_matches(self, query: str, *, top_k: int = 3) -> list[ToolSearchMatch]:
        return self._index.search(query, top_k=top_k)
