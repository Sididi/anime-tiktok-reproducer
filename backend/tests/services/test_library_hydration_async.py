from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.config import settings
from app.library_types import LibraryType
from app.services.library_hydration_service import (
    HYDRATION_STATUS_HYDRATING_INDEX,
    HYDRATION_STATUS_INDEX_READY,
    LibraryHydrationService,
    OPERATION_COMPLETE,
    OPERATION_RUNNING,
)
from app.services.library_state_db import LibraryStateDb


@pytest.mark.asyncio
async def test_enqueue_project_activation_returns_pending_and_reuses_running_operation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(settings, "library_state_db_path", tmp_path / "library_state.db")
    LibraryStateDb.initialize()

    started = asyncio.Event()
    release = asyncio.Event()
    activation_calls = 0

    async def fake_ensure_index_ready(
        cls,
        *,
        library_type: LibraryType,
        series_id: str,
    ) -> bool:
        return False

    async def fake_activate_project_series(
        cls,
        *,
        project_id: str,
        library_type: LibraryType,
        series_id: str,
        progress_callback=None,
    ) -> dict[str, object]:
        nonlocal activation_calls
        activation_calls += 1
        await asyncio.to_thread(
            LibraryStateDb.upsert_series_state,
            library_type=library_type,
            series_id=series_id,
            release_id="release-1",
            hydration_status=HYDRATION_STATUS_HYDRATING_INDEX,
            local_episode_count=0,
            expected_episode_count=1,
            last_error=None,
        )
        await asyncio.to_thread(
            LibraryStateDb.upsert_operation,
            library_type=library_type,
            series_id=series_id,
            operation_type="activate",
            status=OPERATION_RUNNING,
            progress=0.5,
            error=None,
        )
        started.set()
        await release.wait()
        await asyncio.to_thread(
            LibraryStateDb.upsert_series_state,
            library_type=library_type,
            series_id=series_id,
            release_id="release-1",
            hydration_status=HYDRATION_STATUS_INDEX_READY,
            local_episode_count=0,
            expected_episode_count=1,
            last_error=None,
        )
        await asyncio.to_thread(
            LibraryStateDb.upsert_operation,
            library_type=library_type,
            series_id=series_id,
            operation_type="activate",
            status=OPERATION_COMPLETE,
            progress=1.0,
            error=None,
        )
        return await cls.get_activation_state(
            library_type=library_type,
            series_id=series_id,
        )

    monkeypatch.setattr(
        LibraryHydrationService,
        "ensure_index_ready",
        classmethod(fake_ensure_index_ready),
    )
    monkeypatch.setattr(
        LibraryHydrationService,
        "activate_project_series",
        classmethod(fake_activate_project_series),
    )

    initial = await LibraryHydrationService.enqueue_project_activation(
        project_id="project-1",
        library_type=LibraryType.ANIME,
        series_id="series-1",
    )
    assert initial["operation"]["status"] in {"pending", "running"}

    await asyncio.wait_for(started.wait(), timeout=1.0)

    reused = await LibraryHydrationService.enqueue_project_activation(
        project_id="project-1",
        library_type=LibraryType.ANIME,
        series_id="series-1",
    )
    assert reused["operation"]["status"] in {"pending", "running"}
    assert activation_calls == 1

    release.set()
    for _ in range(50):
        final = await LibraryHydrationService.get_activation_state(
            library_type=LibraryType.ANIME,
            series_id="series-1",
        )
        if final["operation"] and final["operation"]["status"] == "complete":
            break
        await asyncio.sleep(0.01)

    assert final["operation"]["status"] == "complete"


@pytest.mark.asyncio
async def test_enqueue_hydrate_series_reuses_running_operation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(settings, "library_state_db_path", tmp_path / "library_state.db")
    LibraryStateDb.initialize()

    manifest = {
        "series_id": "series-1",
        "release_id": "release-1",
        "display_name": "Demo",
        "episode_count": 1,
        "episodes": [
            {
                "episode_key": "ep-1",
                "media": {"local_relative_path": "Demo/ep-1.mp4"},
            }
        ],
    }
    started = asyncio.Event()
    release = asyncio.Event()
    hydrate_calls = 0

    async def fake_load_or_fetch_manifest(
        cls,
        library_type: LibraryType,
        series_id: str,
    ) -> dict[str, object]:
        return manifest

    async def fake_hydrate_episode(
        cls,
        library_type: LibraryType,
        manifest: dict,
        episode: dict,
    ) -> None:
        nonlocal hydrate_calls
        hydrate_calls += 1
        started.set()
        await release.wait()

    def fake_count_local_episodes(
        cls,
        library_type: LibraryType,
        manifest: dict,
    ) -> int:
        return 1 if release.is_set() else 0

    monkeypatch.setattr(
        LibraryHydrationService,
        "_load_or_fetch_manifest",
        classmethod(fake_load_or_fetch_manifest),
    )
    monkeypatch.setattr(
        LibraryHydrationService,
        "_hydrate_episode",
        classmethod(fake_hydrate_episode),
    )
    monkeypatch.setattr(
        LibraryHydrationService,
        "_count_local_episodes_from_manifest",
        classmethod(fake_count_local_episodes),
    )

    initial = await LibraryHydrationService.enqueue_hydrate_series(
        library_type=LibraryType.ANIME,
        series_id="series-1",
        full_series=True,
    )
    assert initial["operation"]["status"] in {"pending", "running"}

    await asyncio.wait_for(started.wait(), timeout=1.0)

    reused = await LibraryHydrationService.enqueue_hydrate_series(
        library_type=LibraryType.ANIME,
        series_id="series-1",
        full_series=True,
    )
    assert reused["operation"]["type"] == "hydrate"
    assert reused["operation"]["status"] in {"pending", "running"}
    assert hydrate_calls == 1

    release.set()
    for _ in range(50):
        final = await LibraryHydrationService.describe_series(
            LibraryType.ANIME,
            "series-1",
        )
        if final["operation"] and final["operation"]["status"] == "complete":
            break
        await asyncio.sleep(0.01)

    assert final["operation"]["status"] == "complete"
