from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import project_manager as project_manager_module


def _build_test_app() -> FastAPI:
    app = FastAPI()
    app.include_router(project_manager_module.router, prefix="/api")
    return app


def test_project_manager_upload_enqueue_route_returns_job_json(monkeypatch) -> None:
    async def fake_enqueue_upload(**kwargs):
        assert kwargs["project_id"] == "project-1"
        assert kwargs["account_id"] == "acct-1"
        return SimpleNamespace(
            model_dump=lambda mode="json": {
                "job_id": "upload-job-1",
                "project_id": "project-1",
                "account_id": "acct-1",
                "status": "queued",
                "phase": "queued",
                "message": "Upload queued",
                "error": None,
                "result": None,
                "created_at": "2026-04-12T10:00:00Z",
                "updated_at": "2026-04-12T10:00:00Z",
            }
        )

    stub_queue = SimpleNamespace(
        enqueue_upload=fake_enqueue_upload,
        list_jobs=lambda: [],
        stream_all_jobs=None,
    )
    monkeypatch.setattr(project_manager_module, "project_upload_queue", stub_queue)

    client = TestClient(_build_test_app())
    response = client.post(
        "/api/project-manager/projects/project-1/upload",
        json={"account_id": "acct-1"},
    )

    assert response.status_code == 200
    assert response.json()["job_id"] == "upload-job-1"
    assert response.json()["status"] == "queued"


def test_project_manager_upload_jobs_list_route_returns_jobs(monkeypatch) -> None:
    stub_queue = SimpleNamespace(
        enqueue_upload=None,
        list_jobs=lambda: [
            SimpleNamespace(
                model_dump=lambda mode="json": {
                    "job_id": "upload-job-1",
                    "project_id": "project-1",
                    "account_id": None,
                    "status": "running",
                    "phase": "platform_upload",
                    "message": "Uploading to social platforms...",
                    "error": None,
                    "result": None,
                    "created_at": "2026-04-12T10:00:00Z",
                    "updated_at": "2026-04-12T10:00:10Z",
                }
            )
        ],
        stream_all_jobs=None,
    )
    monkeypatch.setattr(project_manager_module, "project_upload_queue", stub_queue)

    client = TestClient(_build_test_app())
    response = client.get("/api/project-manager/upload-jobs")

    assert response.status_code == 200
    assert response.json()["jobs"][0]["phase"] == "platform_upload"


def test_project_manager_upload_jobs_stream_route_emits_sse(monkeypatch) -> None:
    async def fake_stream_all_jobs():
        yield {
            "job_id": "upload-job-1",
            "project_id": "project-1",
            "account_id": None,
            "status": "complete",
            "phase": "complete",
            "message": "Upload complete.",
            "error": None,
            "result": {"ok": True},
            "created_at": "2026-04-12T10:00:00Z",
            "updated_at": "2026-04-12T10:00:20Z",
        }

    stub_queue = SimpleNamespace(
        enqueue_upload=None,
        list_jobs=lambda: [],
        stream_all_jobs=fake_stream_all_jobs,
    )
    monkeypatch.setattr(project_manager_module, "project_upload_queue", stub_queue)

    client = TestClient(_build_test_app())
    with client.stream("GET", "/api/project-manager/upload-jobs/stream") as response:
        body = "".join(response.iter_text())

    assert response.status_code == 200
    assert "data:" in body
    assert "\"status\": \"complete\"" in body
