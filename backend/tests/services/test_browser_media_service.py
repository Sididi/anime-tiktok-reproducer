from pathlib import Path
import sys

import pytest

sys.path.append(str(Path(__file__).resolve().parents[2]))

pytest.importorskip("PIL")

from app.services.browser_media_service import BrowserMediaService


def test_ensure_preview_proxy_reuses_valid_cached_preview(monkeypatch, tmp_path):
    source_path = tmp_path / "source.mkv"
    source_path.write_bytes(b"source")
    preview_path = tmp_path / "cached-preview.mp4"
    preview_path.write_bytes(b"preview")

    monkeypatch.setattr(
        BrowserMediaService,
        "is_browser_preview_compatible_sync",
        classmethod(lambda cls, path, *, include_audio: False),
    )
    monkeypatch.setattr(
        BrowserMediaService,
        "get_preview_dir",
        classmethod(lambda cls, profile: tmp_path),
    )
    monkeypatch.setattr(
        BrowserMediaService,
        "get_preview_path_sync",
        classmethod(
            lambda cls, path, *, profile, include_audio: preview_path,
        ),
    )
    monkeypatch.setattr(
        BrowserMediaService,
        "_is_valid_preview_sync",
        classmethod(lambda cls, path: path == preview_path and path.exists()),
    )

    def fail_run(*args, **kwargs):
        raise AssertionError("ffmpeg should not run when cached preview is valid")

    monkeypatch.setattr("app.services.browser_media_service.subprocess.run", fail_run)

    resolved = BrowserMediaService.ensure_preview_proxy_sync(
        source_path,
        profile=BrowserMediaService.PROJECT_PROFILE,
        include_audio=True,
    )

    assert resolved == preview_path


@pytest.mark.asyncio
async def test_resolve_preview_path_prefers_cached_preview_without_generation(
    monkeypatch,
    tmp_path,
):
    source_path = tmp_path / "source.mkv"
    source_path.write_bytes(b"source")
    preview_path = tmp_path / "cached-preview.mp4"
    preview_path.write_bytes(b"preview")

    monkeypatch.setattr(
        BrowserMediaService,
        "is_browser_preview_compatible_sync",
        classmethod(lambda cls, path, *, include_audio: False),
    )
    monkeypatch.setattr(
        BrowserMediaService,
        "get_preview_path_sync",
        classmethod(
            lambda cls, path, *, profile, include_audio: preview_path,
        ),
    )
    monkeypatch.setattr(
        BrowserMediaService,
        "_is_valid_preview_sync",
        classmethod(lambda cls, path: path == preview_path and path.exists()),
    )

    resolved = await BrowserMediaService.resolve_preview_path(
        source_path,
        profile=BrowserMediaService.SOURCE_PROFILE,
        include_audio=False,
        allow_generate=False,
    )

    assert resolved == preview_path
