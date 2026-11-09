"""Loopback-first FastAPI/SSE interaction layer for AtlasOS."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from atlasos.app.service import (
    ApplicationError,
    ApprovalAuthorityUnavailable,
    AtlasApplicationService,
    RecoveryAuthorityUnavailable,
)

STATIC = Path(__file__).resolve().parent / "static"
INDEX = STATIC / "index.html"


class RunRequest(BaseModel):
    task: str = Field(min_length=1, max_length=20_000)
    mode: str = "workflow"
    workspace: str | None = None
    max_turns: int = Field(default=12, ge=1, le=30)


def create_app(*, runs_root: str | Path) -> FastAPI:
    service = AtlasApplicationService(runs_root=runs_root)
    app = FastAPI(title="AtlasOS", version="0.0.1")

    @app.exception_handler(ApplicationError)
    async def application_error(_request: Request, error: ApplicationError) -> JSONResponse:
        return JSONResponse(
            status_code=getattr(error, "status_code", 400),
            content={"error": str(error), "kind": type(error).__name__},
        )

    @app.exception_handler(ApprovalAuthorityUnavailable)
    async def approval_error(
        _request: Request, error: ApprovalAuthorityUnavailable
    ) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={"error": str(error), "kind": "ApprovalAuthorityUnavailable"},
        )

    @app.exception_handler(RecoveryAuthorityUnavailable)
    async def recovery_error(
        _request: Request, error: RecoveryAuthorityUnavailable
    ) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={"error": str(error), "kind": "RecoveryAuthorityUnavailable"},
        )

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "service": "atlasos-app", "loopback_only_by_default": True}

    @app.post("/api/runs")
    def submit(body: RunRequest) -> dict[str, Any]:
        return service.submit(
            body.task, mode=body.mode, workspace=body.workspace, max_turns=body.max_turns
        )

    @app.get("/api/runs")
    def runs(limit: int = Query(50, ge=1, le=200)) -> list[dict[str, Any]]:
        return service.list_runs(limit=limit)

    @app.get("/api/runs/{run_id}")
    def detail(run_id: str) -> dict[str, Any]:
        return service.detail(run_id)

    @app.get("/api/runs/{run_id}/result")
    def result(run_id: str) -> dict[str, Any]:
        return service.result(run_id)

    @app.get("/api/runs/{run_id}/events")
    def events(run_id: str) -> list[dict[str, Any]]:
        return service.events(run_id)

    @app.get("/api/runs/{run_id}/stream")
    def stream(
        run_id: str,
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
        after: int = Query(0, ge=0),
    ) -> StreamingResponse:
        start = int(last_event_id) if last_event_id and last_event_id.isdigit() else after

        def generate() -> Any:
            for event in service.stream_events(run_id, after=start):
                payload = json.dumps(event, ensure_ascii=False, sort_keys=True)
                yield f"id: {event['id']}\nevent: {event['type']}\ndata: {payload}\n\n"

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/approvals")
    def approvals() -> list[dict[str, Any]]:
        return service.list_approvals()

    @app.get("/api/runs/{run_id}/approvals")
    def approvals_for_run(run_id: str) -> list[dict[str, Any]]:
        return service.list_approvals(run_id)

    @app.post("/api/approvals/{approval_id}/approve")
    def approve(approval_id: str) -> dict[str, Any]:
        return service.approve(approval_id)

    @app.post("/api/approvals/{approval_id}/reject")
    def reject(approval_id: str) -> dict[str, Any]:
        return service.reject(approval_id)

    @app.get("/api/runs/{run_id}/recovery")
    def recovery(run_id: str) -> dict[str, Any]:
        return service.recovery(run_id)

    @app.post("/api/runs/{run_id}/resume")
    def resume(run_id: str) -> dict[str, Any]:
        return service.resume(run_id)

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(INDEX)

    @app.get("/runs/{run_id}")
    def run_page(run_id: str) -> FileResponse:
        del run_id
        return FileResponse(INDEX)

    @app.get("/approvals")
    def approvals_page() -> FileResponse:
        return FileResponse(INDEX)

    @app.get("/history")
    def history_page() -> FileResponse:
        return FileResponse(INDEX)

    return app
