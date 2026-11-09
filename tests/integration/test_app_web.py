from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from atlasos.app.web import create_app


def _seed(root: Path) -> None:
    run = root / "app-web"
    run.mkdir(parents=True)
    (run / "run.json").write_text(
        json.dumps(
            {
                "run_id": "app-web",
                "mode": "workflow",
                "task": "web task",
                "created_at": "2026-11-09T00:00:00+00:00",
                "updated_at": "2026-11-09T00:00:01+00:00",
                "status": "COMPLETED",
            }
        ),
        encoding="utf-8",
    )
    (run / "result.json").write_text(
        json.dumps({"final_output": "web answer", "metrics": {"tool_calls": 0}}),
        encoding="utf-8",
    )
    (run / "events.jsonl").write_text(
        json.dumps({"id": 1, "type": "run_submitted", "timestamp": "2026-11-09T00:00:00Z"}) + "\n",
        encoding="utf-8",
    )


def test_pages_and_history_are_local_projections(tmp_path: Path) -> None:
    _seed(tmp_path)
    client = TestClient(create_app(runs_root=tmp_path))
    assert client.get("/api/health").json()["ok"] is True
    index = client.get("/")
    assert index.status_code == 200
    assert "AtlasOS" in index.text
    # The approval UI must not contain an automatic approval request.
    assert "setInterval" not in index.text
    runs = client.get("/api/runs")
    assert runs.status_code == 200
    assert runs.json()[0]["run_id"] == "app-web"
    assert client.get("/api/runs/app-web").json()["final_output"] == "web answer"
    assert client.get("/api/runs/app-web/events").json()[0]["id"] == 1
    assert client.get("/runs/app-web").status_code == 200
    assert client.get("/history").status_code == 200
    assert client.get("/approvals").status_code == 200


def test_invalid_submission_is_4xx_and_does_not_create_run(tmp_path: Path) -> None:
    client = TestClient(create_app(runs_root=tmp_path))
    response = client.post("/api/runs", json={"task": "x", "mode": "dynamic_supervisor"})
    assert response.status_code == 400
    response = client.post(
        "/api/runs", json={"task": "x", "mode": "coding", "workspace": "D:/outside"}
    )
    assert response.status_code == 422
    assert not any(path.is_dir() and path.name.startswith("app-") for path in tmp_path.iterdir())


def test_approval_resolution_is_refused_without_live_authority(tmp_path: Path) -> None:
    client = TestClient(create_app(runs_root=tmp_path))
    response = client.post("/api/approvals/not-a-real-approval/approve")
    assert response.status_code == 409
    assert (
        "authoritative approval" in response.json()["error"]
        or "trusted suspension" in response.json()["error"]
    )
