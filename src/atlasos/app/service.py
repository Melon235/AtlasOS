"""Thin application service shared by the AtlasOS CLI and Web UI.

The service validates inputs, launches existing execution paths, projects their persisted
results, and serves events. It does not schedule tasks, execute tools, approve actions, or own
recovery state. The authoritative stores and runtimes remain below this layer.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from atlasos.config import (
    AtlasConfig,
    ContextConfig,
    ContextTransformConfig,
    PlacementConfig,
    Pricing,
    ProviderConfig,
)
from atlasos.context.requirements import ContextRequirement
from atlasos.runtime import AtlasRuntime
from atlasos.workflow import AtlasAgentSpec, AtlasTaskSpec, AtlasWorkflow

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_WORKSPACE_ROOT = REPO_ROOT / "experiments" / "fixtures"
DEFAULT_CODING_WORKSPACE = DEFAULT_WORKSPACE_ROOT / "uat_repo"
TERMINAL = {"COMPLETED", "FAILED", "CANCELLED", "RECOVERY_REQUIRED", "RECOVERY_UNCERTAIN"}


class ApplicationError(RuntimeError):
    """Invalid application request; the API maps this to 4xx."""

    status_code = 400


class WorkspaceNotSupported(ApplicationError):
    status_code = 422


class ApprovalAuthorityUnavailable(ApplicationError):
    status_code = 409


class RecoveryAuthorityUnavailable(ApplicationError):
    status_code = 409


@dataclass
class RunRecord:
    run_id: str
    mode: str
    task: str
    workspace: str | None
    created_at: str
    status: str = "RUNNING"
    updated_at: str = ""
    events: list[dict[str, Any]] = field(default_factory=list)
    result: dict[str, Any] | None = None
    session: dict[str, Any] | None = None
    error: str | None = None
    failure_class: str | None = None
    atlas_run_id: str | None = None
    uat_run_id: str | None = None
    cleanup_error: str | None = None
    recovery_uncertain: bool = False
    thread: threading.Thread | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _pricing() -> Pricing:
    return Pricing(
        cache_hit_input_per_million=0.006,
        cache_miss_input_per_million=0.300,
        output_per_million=1.200,
        snapshot_date=datetime(2026, 11, 9).date(),
    )


def _workflow(task: str) -> AtlasWorkflow:
    return AtlasWorkflow(
        name="atlas-app",
        agents=(
            AtlasAgentSpec(
                name="researcher",
                role="researcher",
                system_prompt=(
                    "You are a precise engineering researcher. Produce a compact plan grounded "
                    "only in the request. Do not claim to have run code."
                ),
                capabilities=frozenset({"research"}),
            ),
            AtlasAgentSpec(
                name="reviewer",
                role="reviewer",
                system_prompt=(
                    "You review the researcher's plan and produce the final answer. Surface "
                    "assumptions and unresolved risks. Never invent execution evidence."
                ),
                capabilities=frozenset({"review"}),
            ),
        ),
        tasks=(
            AtlasTaskSpec(
                task_id="research",
                title="research",
                description=(
                    "Analyze the following request and produce a concise plan with concrete "
                    f"steps:\n\n{task}"
                ),
                assignee="researcher",
                required_capabilities=frozenset({"research"}),
            ),
            AtlasTaskSpec(
                task_id="review",
                title="review",
                description=(
                    "Review the dependency output. Produce the final answer for the user, "
                    "including assumptions and risks. Do not invent tool output."
                ),
                assignee="reviewer",
                depends_on=("research",),
                required_capabilities=frozenset({"review"}),
                required_context=(ContextRequirement("dep:research", source_task_id="research"),),
            ),
        ),
    )


class AtlasApplicationService:
    def __init__(
        self,
        *,
        runs_root: str | Path,
        workspace_root: str | Path = DEFAULT_WORKSPACE_ROOT,
        repo_root: str | Path = REPO_ROOT,
        coding_workspace: str | Path = DEFAULT_CODING_WORKSPACE,
        sandbox_provider: str = "opensandbox",
    ) -> None:
        self.runs_root = Path(runs_root).resolve()
        self.workspace_root = Path(workspace_root).resolve()
        self.repo_root = Path(repo_root).resolve()
        self.coding_workspace = Path(coding_workspace).resolve()
        self.sandbox_provider = sandbox_provider
        self._records: dict[str, RunRecord] = {}
        self.runs_root.mkdir(parents=True, exist_ok=True)
        load_dotenv(self.repo_root / ".env", override=False)

    # ---------------------------------------------------------------- submission
    def submit(
        self,
        task: str,
        *,
        mode: str = "workflow",
        workspace: str | None = None,
        max_turns: int = 12,
    ) -> dict[str, Any]:
        task = task.strip()
        if not task:
            raise ApplicationError("task must not be empty")
        if mode not in {"workflow", "coding"}:
            raise ApplicationError("mode must be 'workflow' or 'coding'")
        if not (1 <= max_turns <= 30):
            raise ApplicationError("max_turns must be between 1 and 30")
        selected: str | None = None
        if mode == "coding":
            selected = str(self._resolve_workspace(workspace))
        run_id = f"app-{uuid.uuid4().hex[:12]}"
        record = RunRecord(
            run_id=run_id,
            mode=mode,
            task=task,
            workspace=selected,
            created_at=_now(),
            updated_at=_now(),
        )
        run_dir = self.runs_root / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        self._records[run_id] = record
        self._persist(record)
        thread = threading.Thread(
            target=self._execute,
            args=(record, max_turns),
            name=f"atlas-app-{run_id}",
            daemon=True,
        )
        record.thread = thread
        thread.start()
        return self.summary(record)

    def _resolve_workspace(self, workspace: str | None) -> Path:
        if not workspace:
            candidate = self.coding_workspace
        else:
            path = Path(workspace)
            candidate = path if path.is_absolute() else self.workspace_root / path
        candidate = candidate.resolve()
        if not candidate.is_dir():
            raise WorkspaceNotSupported(f"workspace is not a directory: {candidate}")
        try:
            candidate.relative_to(self.workspace_root)
        except ValueError as error:
            raise WorkspaceNotSupported(f"workspace must be under {self.workspace_root}") from error
        if not (candidate / "csv_parser.py").is_file():
            raise WorkspaceNotSupported(
                "coding mode currently supports UAT-compatible workspaces with csv_parser.py"
            )
        return candidate

    def _execute(self, record: RunRecord, max_turns: int) -> None:
        try:
            self._event(record, "run_submitted", {"mode": record.mode, "task": record.task})
            if record.mode == "workflow":
                self._execute_workflow(record)
            else:
                self._execute_coding(record, max_turns)
        except Exception as error:  # noqa: BLE001 - surfaced as run failure
            record.error = f"{type(error).__name__}: {error}"
            record.status = "FAILED"
            record.failure_class = "APPLICATION_ERROR"
            self._event(record, "run_failed", {"error": record.error})
        finally:
            record.updated_at = _now()
            self._persist(record)

    def _execute_workflow(self, record: RunRecord) -> None:
        atlas_root = self.runs_root / record.run_id / "atlas"
        atlas_root.mkdir(parents=True, exist_ok=True)

        def hook(event: Any) -> None:
            data = getattr(event, "data", None)
            payload: dict[str, Any] = {"event_type": str(event.type)}
            if data is not None:
                payload["title"] = getattr(data, "title", None)
                payload["assignee"] = getattr(data, "assignee", None)
            self._event(record, "runtime_event", payload)

        config = AtlasConfig(
            provider=ProviderConfig(model="deepseek-chat"),
            context=ContextConfig(
                policy="task_scoped",
                renderer="stable_deterministic",
                transform=ContextTransformConfig(name="identity"),
            ),
            placement=PlacementConfig(policy="bypass"),
            pricing=_pricing(),
        )
        runtime = AtlasRuntime(
            config,
            runs_root=atlas_root,
            event_hook=hook,
            repo_root=self.repo_root,
        )
        result = asyncio.run(
            runtime.run(_workflow(record.task), experiment="atlas-app", persist=True)
        )
        record.atlas_run_id = result.run_id
        record.result = result.model_dump(mode="json")
        record.status = "COMPLETED" if result.status == "succeeded" else "FAILED"
        if result.status != "succeeded":
            record.failure_class = "RUNTIME_FAILED"
        self._event(record, "run_completed" if result.status == "succeeded" else "run_failed", {})
        self._persist(record)

    def _execute_coding(self, record: RunRecord, max_turns: int) -> None:
        coding_root = self.runs_root / record.run_id / "coding"
        coding_root.mkdir(parents=True, exist_ok=True)
        command = [
            sys.executable,
            "-m",
            "experiments.uat_cli",
            record.task,
            "--fixture",
            str(record.workspace),
            "--runs-root",
            str(coding_root),
            "--provider",
            "deepseek",
            "--model",
            "deepseek-chat",
            "--max-turns",
            str(max_turns),
            "--sandbox-provider",
            self.sandbox_provider,
            "--require-real-sandbox",
        ]
        env = {
            **os.environ,
            "PYTHONPATH": os.pathsep.join([str(self.repo_root / "src"), str(self.repo_root)]),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        self._event(record, "coding_started", {"workspace": record.workspace})
        process = subprocess.Popen(  # noqa: S603 - fixed command
            command,
            cwd=self.repo_root,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        stdout, stderr = process.communicate()
        session: dict[str, Any] | None = None
        for line in reversed((stdout or "").splitlines()):
            if line.strip().startswith("{"):
                try:
                    session = json.loads(line)
                except json.JSONDecodeError:
                    continue
                break
        if session is None:
            record.error = (stderr or stdout or "coding session produced no JSON")[-4000:]
            record.status = "FAILED"
            record.failure_class = "CODING_RUNTIME_ERROR"
            self._event(record, "run_failed", {"error": record.error})
            return
        record.session = session
        record.uat_run_id = str(session.get("run_id") or "")
        record.cleanup_error = (session.get("sandbox") or {}).get("cleanup_error")
        failure_class = str(session.get("failure_class") or "")
        if "SANDBOX_LOST" in failure_class.upper():
            record.status = "SANDBOX_LOST"
        elif record.cleanup_error:
            record.status = "CLEANUP_UNRESOLVED"
        elif session.get("status") == "completed":
            record.status = "COMPLETED"
        else:
            record.status = "FAILED"
        record.failure_class = failure_class or None
        record.error = session.get("failure_message")
        self._event(
            record,
            "run_completed" if record.status == "COMPLETED" else "run_failed",
            {"status": record.status, "failure_class": record.failure_class},
        )
        self._persist(record)

    # ---------------------------------------------------------------- projections
    def _event(self, record: RunRecord, event_type: str, data: dict[str, Any]) -> None:
        with record.lock:
            event = {
                "id": len(record.events) + 1,
                "type": event_type,
                "timestamp": _now(),
                "data": data,
            }
            record.events.append(event)
            path = self.runs_root / record.run_id / "events.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, sort_keys=True) + "\n")
        record.updated_at = event["timestamp"]

    def _persist(self, record: RunRecord) -> None:
        path = self.runs_root / record.run_id / "run.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "run_id": record.run_id,
            "mode": record.mode,
            "task": record.task,
            "workspace": record.workspace,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
            "status": record.status,
            "error": record.error,
            "failure_class": record.failure_class,
            "atlas_run_id": record.atlas_run_id,
            "uat_run_id": record.uat_run_id,
            "cleanup_error": record.cleanup_error,
            "recovery_uncertain": record.recovery_uncertain,
        }
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        if record.result is not None:
            (self.runs_root / record.run_id / "result.json").write_text(
                json.dumps(record.result, indent=2, sort_keys=True), encoding="utf-8"
            )
        if record.session is not None:
            (self.runs_root / record.run_id / "session.json").write_text(
                json.dumps(record.session, indent=2, sort_keys=True), encoding="utf-8"
            )

    def _load(self, run_id: str) -> RunRecord:
        for record in getattr(self, "_records", {}).values():
            if record.run_id == run_id:
                self._refresh(record)
                return record
        path = self.runs_root / run_id / "run.json"
        if not path.is_file():
            raise ApplicationError(f"unknown run: {run_id}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        record = RunRecord(
            run_id=payload["run_id"],
            mode=payload["mode"],
            task=payload["task"],
            workspace=payload.get("workspace"),
            created_at=payload["created_at"],
            updated_at=payload.get("updated_at") or payload["created_at"],
            status=payload.get("status") or "FAILED",
            error=payload.get("error"),
            failure_class=payload.get("failure_class"),
            atlas_run_id=payload.get("atlas_run_id"),
            uat_run_id=payload.get("uat_run_id"),
            cleanup_error=payload.get("cleanup_error"),
            recovery_uncertain=bool(payload.get("recovery_uncertain")),
        )
        result_path = self.runs_root / run_id / "result.json"
        if result_path.is_file():
            record.result = json.loads(result_path.read_text(encoding="utf-8"))
        session_path = self.runs_root / run_id / "session.json"
        if session_path.is_file():
            record.session = json.loads(session_path.read_text(encoding="utf-8"))
        events_path = self.runs_root / run_id / "events.jsonl"
        if events_path.is_file():
            record.events = [
                json.loads(line)
                for line in events_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        getattr(self, "_records", {})[run_id] = record
        return record

    def _refresh(self, record: RunRecord) -> None:
        if record.thread is not None and record.thread.is_alive():
            record.status = "RUNNING"
        if record.status in {"SANDBOX_LOST", "CLEANUP_UNRESOLVED"}:
            return
        pending = self.list_approvals(record.run_id)
        if pending:
            record.status = "WAITING_APPROVAL"
            return
        recovery = self.recovery(record.run_id)
        if recovery["state"] == "RECOVERY_REQUIRED":
            record.status = "RECOVERY_REQUIRED"
        elif record.recovery_uncertain:
            record.status = "RECOVERY_UNCERTAIN"
        elif record.status in TERMINAL:
            return

    def summary(self, record: RunRecord) -> dict[str, Any]:
        self._refresh(record)
        return {
            "run_id": record.run_id,
            "mode": record.mode,
            "task": record.task,
            "workspace": record.workspace,
            "status": record.status,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
            "failure_class": record.failure_class,
            "error": record.error,
            "atlas_run_id": record.atlas_run_id,
            "uat_run_id": record.uat_run_id,
            "cleanup_error": record.cleanup_error,
        }

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for path in self.runs_root.iterdir():
            if not path.is_dir() or not (path / "run.json").is_file():
                continue
            try:
                items.append(self.summary(self._load(path.name)))
            except ApplicationError:
                continue
        items.sort(key=lambda item: item["created_at"], reverse=True)
        return items[:limit]

    def detail(self, run_id: str) -> dict[str, Any]:
        record = self._load(run_id)
        summary = self.summary(record)
        metrics: dict[str, Any] = {}
        tasks: list[dict[str, Any]] = []
        tool_calls: list[dict[str, Any]] = []
        final_output = ""
        model = "deepseek-chat"
        artifacts: list[dict[str, Any]] = []
        if record.result is not None:
            metrics = record.result.get("metrics") or {}
            tasks = [
                {
                    "task_id": item.get("task_id"),
                    "title": item.get("title"),
                    "assignee": item.get("assignee") or item.get("agent"),
                    "status": item.get("status"),
                    "output": item.get("output"),
                    "prompt_tokens": item.get("prompt_tokens"),
                    "completion_tokens": item.get("completion_tokens"),
                    "duration_ms": item.get("duration_ms"),
                    "depends_on": ["research"] if item.get("task_id") == "review" else [],
                }
                for item in record.result.get("tasks") or []
            ]
            final_output = str(record.result.get("final_output") or "")
            model = str(record.result.get("model") or model)
            tool_calls = list(record.result.get("tool_calls") or [])
        if record.session is not None:
            final_output = str(record.session.get("final_output") or final_output)
            model = str(record.session.get("model") or model)
            tool_calls = list(record.session.get("tool_calls") or [])
            tasks = [
                {
                    "task_id": record.session.get("task_id"),
                    "title": "coding task",
                    "assignee": record.session.get("worker_id"),
                    "status": record.session.get("status"),
                    "output": record.session.get("final_output"),
                }
            ]
            metrics = {
                "prompt_tokens": sum(int(item.get("tokens") or 0) for item in tool_calls),
                "tool_calls": len(tool_calls),
            }
            export = record.session.get("workspace_export")
            if export:
                artifacts.append(
                    {
                        "kind": "workspace_export",
                        "path": str(export),
                        "patch_changed_files": record.session.get("patch_changed_files"),
                    }
                )
        return {
            **summary,
            "model": model,
            "metrics": metrics,
            "artifacts": artifacts,
            "tasks": tasks,
            "workers": [
                {
                    "worker_id": item.get("assignee") or item.get("worker_id"),
                    "task_id": item.get("task_id"),
                    "status": item.get("status"),
                    "title": item.get("title"),
                }
                for item in tasks
            ],
            "tool_calls": tool_calls[-50:],
            "final_output": final_output,
            "events": record.events[-200:],
            "approvals": self.list_approvals(run_id),
            "recovery": self.recovery(run_id),
        }

    def result(self, run_id: str) -> dict[str, Any]:
        detail = self.detail(run_id)
        return {
            "run_id": detail["run_id"],
            "status": detail["status"],
            "final_output": detail["final_output"],
            "metrics": detail["metrics"],
            "tasks": detail["tasks"],
            "failure_class": detail["failure_class"],
            "error": detail["error"],
        }

    def events(self, run_id: str, after: int = 0) -> list[dict[str, Any]]:
        record = self._load(run_id)
        self._refresh(record)
        return [event for event in record.events if int(event["id"]) > after]

    def stream_events(self, run_id: str, after: int = 0):
        sent = after
        terminal_sent = False
        while True:
            record = self._load(run_id)
            self._refresh(record)
            for event in self.events(run_id, sent):
                sent = max(sent, int(event["id"]))
                yield event
            if record.status in TERMINAL or record.status == "SANDBOX_LOST":
                if terminal_sent:
                    return
                terminal_sent = True
            time.sleep(0.25)

    # ---------------------------------------------------------------- approvals/recovery
    @property
    def _approval_db(self) -> Path:
        return self.runs_root / "approvals.db"

    @property
    def _suspension_db(self) -> Path:
        return self.runs_root / "suspension.db"

    def list_approvals(self, run_id: str | None = None) -> list[dict[str, Any]]:
        if not self._approval_db.is_file():
            return []
        from atlasos.approvals.store import SQLiteApprovalStore

        store = SQLiteApprovalStore(self._approval_db)
        values = store.list_all()
        items = [item.model_dump(mode="json") for item in values]
        if run_id is None:
            return items
        filtered = []
        for item in items:
            text = json.dumps(item)
            if run_id in text:
                filtered.append(item)
        return filtered

    def _resolve_approval(self, approval_id: str, decision: str) -> dict[str, Any]:
        if not self._approval_db.is_file():
            raise ApprovalAuthorityUnavailable("no authoritative approval store is present")
        from atlasos.approvals.coordinator import ApprovalCoordinator
        from atlasos.approvals.models import ReviewerIdentity
        from atlasos.approvals.store import SQLiteApprovalStore
        from atlasos.suspension.store import SQLiteSuspensionStore

        store = SQLiteApprovalStore(self._approval_db)
        challenge = store.get(approval_id)
        if challenge is None:
            raise ApprovalAuthorityUnavailable(f"unknown approval: {approval_id}")
        if not self._suspension_db.is_file():
            raise ApprovalAuthorityUnavailable(
                "approval is not attached to an active trusted suspension"
            )
        suspension = SQLiteSuspensionStore(self._suspension_db).get(challenge.suspension_id)
        if suspension is None or suspension.state != "ACTIVE":
            raise ApprovalAuthorityUnavailable(
                "approval is not attached to an active trusted suspension"
            )
        if not any(
            invocation.approval_id == approval_id for invocation in suspension.pending_invocations
        ):
            raise ApprovalAuthorityUnavailable(
                "approval is not one of the suspension's pending human decisions"
            )
        coordinator = ApprovalCoordinator(store)
        resolution = coordinator.resolve(
            approval_id,
            expected_version=challenge.version,
            decision=decision,  # type: ignore[arg-type]
            reviewer=ReviewerIdentity(actor="user:web"),
            reason="explicit local UI decision",
        )
        if resolution.status not in {"resolved", "already_resolved"}:
            raise ApprovalAuthorityUnavailable(f"approval resolution refused: {resolution.status}")
        resolved = resolution.challenge or challenge
        return {
            "approval_id": resolved.approval_id,
            "state": resolved.state,
            "version": resolved.version,
            "actor": resolved.actor,
            "resolution_status": resolution.status,
        }

    def approve(self, approval_id: str) -> dict[str, Any]:
        return self._resolve_approval(approval_id, "APPROVED")

    def reject(self, approval_id: str) -> dict[str, Any]:
        return self._resolve_approval(approval_id, "REJECTED")

    def recovery(self, run_id: str | None = None) -> dict[str, Any]:
        if not self._suspension_db.is_file():
            return {"state": "NONE", "suspensions": []}
        from atlasos.suspension.store import SQLiteSuspensionStore

        store = SQLiteSuspensionStore(self._suspension_db)
        active = store.list_active()
        values = [item.public_projection() for item in active]
        if run_id is not None:
            values = [item for item in values if run_id in json.dumps(item)]
        return {
            "state": "RECOVERY_REQUIRED" if values else "NONE",
            "suspensions": values,
        }

    def resume(self, run_id: str) -> dict[str, Any]:
        recovery = self.recovery(run_id)
        if recovery["state"] == "NONE":
            raise RecoveryAuthorityUnavailable("no active trusted suspension for this run")
        raise RecoveryAuthorityUnavailable(
            "trusted Stage D recovery requires the operator recovery harness; the application "
            "service will not reattach or destroy a sandbox without the M5/M6 authority"
        )
