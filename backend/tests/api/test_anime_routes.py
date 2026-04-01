from __future__ import annotations

from pathlib import Path
import sys

import pytest
from fastapi import HTTPException

REPO_ROOT = Path(__file__).resolve().parents[3]
BACKEND_ROOT = REPO_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.api.routes.anime import delete_series as delete_series_route
from app.api.routes.anime import get_series_state as get_series_state_route
from app.api.routes.anime import hydrate_series as hydrate_series_route
from app.api.routes.anime import HydrateSeriesRequest
from app.library_types import LibraryType
from app.services.library_hydration_service import LibraryHydrationService
from app.services.library_hydration_service import SeriesDeleteBlockedError


@pytest.mark.asyncio
async def test_delete_series_route_returns_structured_conflict_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blocked_error = SeriesDeleteBlockedError(
        library_type=LibraryType.ANIME,
        series_id="series-1",
        referencing_projects=[
            {
                "project_id": "project-1",
                "anime_title": "Demo",
                "phase": "matching",
                "scheduled_at": None,
                "upload_completed_at": None,
            }
        ],
    )

    async def fake_delete_series(
        cls,
        *,
        library_type: LibraryType,
        series_id: str,
    ) -> dict[str, object]:
        raise blocked_error

    monkeypatch.setattr(
        LibraryHydrationService,
        "delete_series",
        classmethod(fake_delete_series),
    )

    with pytest.raises(HTTPException) as exc_info:
        await delete_series_route("series-1", LibraryType.ANIME)

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "series_delete_blocked"
    assert exc_info.value.detail["referencing_projects"][0]["project_id"] == "project-1"


@pytest.mark.asyncio
async def test_get_series_state_route_returns_described_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_describe_series(
        cls,
        library_type: LibraryType,
        series_id: str,
    ) -> dict[str, object]:
        return {
            "series_id": series_id,
            "hydration_status": "index_ready",
            "operation": {"type": "hydrate", "status": "running", "progress": 0.5},
        }

    monkeypatch.setattr(
        LibraryHydrationService,
        "describe_series",
        classmethod(fake_describe_series),
    )

    result = await get_series_state_route("series-1", LibraryType.ANIME)

    assert result["series_id"] == "series-1"
    assert result["operation"]["status"] == "running"


@pytest.mark.asyncio
async def test_hydrate_series_route_enqueues_background_hydration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_enqueue_hydrate_series(
        cls,
        *,
        library_type: LibraryType,
        series_id: str,
        episode_keys: list[str] | None = None,
        full_series: bool = False,
    ) -> dict[str, object]:
        assert episode_keys == ["ep-1"]
        assert full_series is False
        return {
            "series_id": series_id,
            "operation": {"type": "hydrate", "status": "pending", "progress": 0.0},
        }

    monkeypatch.setattr(
        LibraryHydrationService,
        "enqueue_hydrate_series",
        classmethod(fake_enqueue_hydrate_series),
    )

    result = await hydrate_series_route(
        "series-1",
        HydrateSeriesRequest(
            library_type=LibraryType.ANIME,
            episode_keys=["ep-1"],
            full_series=False,
        ),
    )

    assert result["series_id"] == "series-1"
    assert result["operation"]["status"] == "pending"
