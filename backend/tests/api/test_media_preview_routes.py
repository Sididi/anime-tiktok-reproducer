from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

sys.path.append(str(Path(__file__).resolve().parents[2]))

pytest.importorskip("fastapi")
pytest.importorskip("PIL")

from fastapi import HTTPException
from fastapi.responses import FileResponse

from app.api.routes.matching import get_matches_playback_clip_by_id
from app.api.routes.video import (
    get_project_video_preview,
    get_source_video_preview,
    warm_project_video_preview,
)


@pytest.mark.asyncio
async def test_get_project_video_preview_uses_preview_headers(monkeypatch, tmp_path):
    video_path = tmp_path / "project.mp4"
    video_path.write_bytes(b"project")
    preview_path = tmp_path / "project-preview.mp4"
    preview_path.write_bytes(b"preview")

    monkeypatch.setattr(
        "app.api.routes.video.ProjectService.load",
        lambda project_id: SimpleNamespace(video_path=str(video_path)),
    )

    async def fake_resolve_preview_path(path, *, profile, include_audio, allow_generate):
        assert path == video_path
        assert profile == "project"
        assert include_audio is True
        assert allow_generate is True
        return preview_path

    monkeypatch.setattr(
        "app.api.routes.video.BrowserMediaService.resolve_preview_path",
        fake_resolve_preview_path,
    )

    response = await get_project_video_preview("project-1")

    assert isinstance(response, FileResponse)
    assert Path(response.path) == preview_path
    assert response.headers["cache-control"] == "public, max-age=3600, stale-while-revalidate=86400"
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["cross-origin-resource-policy"] == "cross-origin"
    assert response.headers.get("etag")


@pytest.mark.asyncio
async def test_warm_project_video_preview_reports_ready(monkeypatch, tmp_path):
    video_path = tmp_path / "project.mp4"
    video_path.write_bytes(b"project")
    calls: list[tuple[str, bool]] = []

    monkeypatch.setattr(
        "app.api.routes.video.ProjectService.load",
        lambda project_id: SimpleNamespace(video_path=str(video_path)),
    )

    async def fake_trigger_preview_generation(path, *, profile, include_audio):
        assert path == video_path
        calls.append((profile, include_audio))

    async def fake_wait_for_preview(
        path,
        *,
        profile,
        include_audio,
        timeout_seconds,
        poll_interval_seconds,
    ):
        assert path == video_path
        assert profile == "project"
        assert include_audio is True
        assert timeout_seconds == 0.15
        assert poll_interval_seconds == 0.05
        return tmp_path / "project-preview.mp4"

    monkeypatch.setattr(
        "app.api.routes.video.BrowserMediaService.trigger_preview_generation",
        fake_trigger_preview_generation,
    )
    monkeypatch.setattr(
        "app.api.routes.video.BrowserMediaService.wait_for_preview",
        fake_wait_for_preview,
    )

    response = await warm_project_video_preview("project-1")

    assert response == {"status": "warming", "ready": True}
    assert calls == [("project", True)]


@pytest.mark.asyncio
async def test_get_matches_playback_clip_by_id_uses_immutable_headers(
    monkeypatch,
    tmp_path,
):
    clip_path = tmp_path / "prepared-clip.mp4"
    clip_path.write_bytes(b"clip")

    monkeypatch.setattr(
        "app.api.routes.matching.ProjectService.load",
        lambda project_id: SimpleNamespace(id=project_id),
    )
    monkeypatch.setattr(
        "app.api.routes.matching.MatchPlaybackService.get_clip_path_by_id",
        lambda project_id, clip_id: clip_path,
    )

    response = await get_matches_playback_clip_by_id("project-1", "clip-123")

    assert isinstance(response, FileResponse)
    assert Path(response.path) == clip_path
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["cross-origin-resource-policy"] == "cross-origin"
    assert response.headers.get("etag")


@pytest.mark.asyncio
async def test_get_source_video_preview_returns_warming_error_when_proxy_not_ready(
    monkeypatch,
    tmp_path,
):
    source_dir = tmp_path / "sources"
    source_dir.mkdir()
    source_path = source_dir / "episode.mp4"
    source_path.write_bytes(b"episode")

    monkeypatch.setattr(
        "app.api.routes.video.ProjectService.load",
        lambda project_id: SimpleNamespace(
            source_paths=[str(source_dir)],
            library_type="anime",
        ),
    )
    monkeypatch.setattr(
        "app.api.routes.video.BrowserMediaService.is_browser_preview_compatible_sync",
        classmethod(lambda cls, path, *, include_audio: False),
    )

    async def fake_resolve_preview_path(*args, **kwargs):
        return source_path

    monkeypatch.setattr(
        "app.api.routes.video.BrowserMediaService.resolve_preview_path",
        fake_resolve_preview_path,
    )

    calls: list[str] = []

    async def fake_trigger_preview_generation(path, *, profile, include_audio):
        assert path == source_path
        calls.append(profile)

    async def fake_wait_for_preview(
        path,
        *,
        profile,
        include_audio,
        timeout_seconds,
        poll_interval_seconds,
    ):
        assert path == source_path
        return None

    monkeypatch.setattr(
        "app.api.routes.video.BrowserMediaService.trigger_preview_generation",
        fake_trigger_preview_generation,
    )
    monkeypatch.setattr(
        "app.api.routes.video.BrowserMediaService.wait_for_preview",
        fake_wait_for_preview,
    )

    with pytest.raises(HTTPException) as exc:
        await get_source_video_preview("project-1", "episode")

    assert exc.value.status_code == 503
    assert exc.value.detail == "Source preview warming"
    assert exc.value.headers == {"Retry-After": "1"}
    assert calls == ["source"]
