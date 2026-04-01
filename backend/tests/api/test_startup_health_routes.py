from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from app import main as main_module
from app.services.integration_health_service import IntegrationHealthService


def test_health_route_returns_while_startup_readiness_is_pending(
    monkeypatch,
) -> None:
    async def fake_async_noop(*_args, **_kwargs) -> None:
        return None

    pending_event = asyncio.Event()

    async def fake_pending_catalog_warmup() -> None:
        await pending_event.wait()

    async def fake_pending_integration_health() -> None:
        await pending_event.wait()

    monkeypatch.setattr(IntegrationHealthService, "_cached_result", None)
    monkeypatch.setattr(main_module.AccountService, "load", lambda: None)
    monkeypatch.setattr(main_module.ProjectService, "sync_all_project_pins", lambda: None)
    monkeypatch.setattr(main_module.LibraryHydrationService, "startup_cleanup", fake_async_noop)
    monkeypatch.setattr(main_module.project_startup_queue, "startup_cleanup", fake_async_noop)
    monkeypatch.setattr(main_module.StorageBoxSftpClient, "close_pool", fake_async_noop)
    monkeypatch.setattr(main_module.settings, "storage_box_enabled", True)
    monkeypatch.setattr(main_module.settings, "integration_startup_health_check_enabled", True)
    monkeypatch.setattr(main_module, "_run_storage_box_catalog_warmup", fake_pending_catalog_warmup)
    monkeypatch.setattr(main_module, "_run_integration_health_check_background", fake_pending_integration_health)

    with TestClient(main_module.app) as client:
        health = client.get("/health")
        readiness = client.get("/api/integrations/health")

        assert health.status_code == 200
        assert health.json() == {"status": "ok"}
        assert readiness.status_code == 200
        assert readiness.json()["status"] == "pending"
