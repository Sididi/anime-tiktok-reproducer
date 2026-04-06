from __future__ import annotations

import pytest

from app.library_types import LibraryType
from app.services.account_service import AccountService
from app.services.integration_health_service import IntegrationHealthService


def test_startup_state_transitions_from_pending_to_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(IntegrationHealthService, "_cached_result", None)
    IntegrationHealthService.initialize_startup_state(
        integration_enabled=True,
        storage_box_enabled=True,
        library_types=[LibraryType.ANIME],
    )

    monkeypatch.setattr(
        IntegrationHealthService,
        "_check_discord",
        classmethod(lambda cls: {"status": "ok", "detail": "ok"}),
    )
    monkeypatch.setattr(
        IntegrationHealthService,
        "_check_google_drive",
        classmethod(lambda cls: {"status": "ok", "detail": "ok"}),
    )
    monkeypatch.setattr(
        IntegrationHealthService,
        "_check_llm_api",
        classmethod(lambda cls: {"status": "ok", "detail": "ok"}),
    )
    monkeypatch.setattr(
        IntegrationHealthService,
        "_check_elevenlabs_api",
        classmethod(lambda cls: {"status": "ok", "detail": "ok"}),
    )
    monkeypatch.setattr(
        IntegrationHealthService,
        "_check_youtube",
        classmethod(lambda cls: {"status": "ok", "detail": "ok"}),
    )
    monkeypatch.setattr(
        IntegrationHealthService,
        "_check_meta",
        classmethod(lambda cls: {"status": "ok", "detail": "ok"}),
    )
    monkeypatch.setattr(AccountService, "list_accounts", classmethod(lambda cls: []))

    pending_result = IntegrationHealthService.run_startup_health_check()
    assert pending_result["startup"]["integration_checks"]["status"] == "ok"
    assert pending_result["startup"]["storage_box_catalogs"]["status"] == "pending"
    assert pending_result["status"] == "pending"

    final_result = IntegrationHealthService.update_catalog_warmup(
        library_type=LibraryType.ANIME,
        status="ok",
        detail="Catalog warmup complete.",
    )

    assert final_result["startup"]["storage_box_catalogs"]["status"] == "ok"
    assert final_result["status"] == "ok"
    assert final_result["summary_status"] == "ok"
