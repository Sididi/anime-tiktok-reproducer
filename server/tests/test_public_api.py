"""Tests for /api/avatars/*."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import httpx
import respx
from fastapi.testclient import TestClient


def _make_app(monkeypatch, example_yaml: Path, example_env, tmp_server_dir: Path):
    monkeypatch.setenv("ATR_TIKTOK_SERVER_CONFIG_PATH", str(example_yaml))
    monkeypatch.setenv("ATR_TIKTOK_SERVER_AVATARS_DIR", str(tmp_server_dir / "avatars"))
    monkeypatch.setenv("ATR_TIKTOK_SERVER_DATA_DIR", str(tmp_server_dir / "data"))
    from app.main import create_app  # noqa: PLC0415

    return create_app()


def test_returns_avatar_bytes(
    monkeypatch, example_yaml: Path, example_env, tmp_server_dir: Path
):
    app = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    with TestClient(app) as client:
        r = client.get("/api/avatars/anime_fr.jpg")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/")
    assert "cache-control" in {k.lower() for k in r.headers}
    assert len(r.content) > 0


def test_404_on_missing(
    monkeypatch, example_yaml: Path, example_env, tmp_server_dir: Path
):
    app = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    with TestClient(app) as client:
        r = client.get("/api/avatars/missing.png")
    assert r.status_code == 404


def test_path_traversal_rejected(
    monkeypatch, example_yaml: Path, example_env, tmp_server_dir: Path
):
    app = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    with TestClient(app) as client:
        r = client.get("/api/avatars/..%2F..%2Fetc%2Fpasswd")
    assert r.status_code in (400, 404)


def test_prepared_instagram_video_served_by_token(
    monkeypatch, example_yaml: Path, example_env, tmp_server_dir: Path
):
    app = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    prepared_dir = tmp_server_dir / "data" / "instagram-prepared"
    prepared_dir.mkdir(parents=True)
    video = prepared_dir / "ig-job-token_123.mp4"
    video.write_bytes(b"prepared mp4 bytes")

    with TestClient(app) as client:
        r = client.get("/api/instagram/prepared/ig-job/token_123.mp4")
        head = client.head("/api/instagram/prepared/ig-job/token_123.mp4")

    assert r.status_code == 200
    assert r.headers["content-type"].startswith("video/mp4")
    assert r.content == b"prepared mp4 bytes"
    assert head.status_code == 200
    assert head.headers["content-length"] == str(len(b"prepared mp4 bytes"))


def test_prepared_instagram_video_rejects_bad_token(
    monkeypatch, example_yaml: Path, example_env, tmp_server_dir: Path
):
    app = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    prepared_dir = tmp_server_dir / "data" / "instagram-prepared"
    prepared_dir.mkdir(parents=True)
    (prepared_dir / "ig-job-token_123.mp4").write_bytes(b"prepared mp4 bytes")

    with TestClient(app) as client:
        r = client.get("/api/instagram/prepared/ig-job/..%2Ftoken_123.mp4")

    assert r.status_code == 404


@respx.mock
def test_video_proxy_streams_job_video(
    monkeypatch, example_yaml: Path, example_env, tmp_server_dir: Path
):
    from app.models.job import Job, PlatformStatus  # noqa: PLC0415

    app = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    source_url = "https://drive.usercontent.google.com/download?id=file_123&export=download&confirm=t"
    project_id = "video-proxy-job"
    asyncio.run(
        app.state.job_store.create(
            Job(
                project_id=project_id,
                job_id="j_video",
                account_id="anime_fr",
                device_id="iphone",
                anime_title="Video Proxy",
                description="desc",
                drive_video_url=source_url,
                slot_time=datetime.now(tz=UTC),
                platforms_requested=["instagram"],
                platform_statuses={"instagram": PlatformStatus(status="pending")},
                discord_message_id=None,
                reminder_message_id=None,
            )
        )
    )

    with TestClient(app) as client:
        upstream = respx.get(source_url).mock(
            return_value=httpx.Response(
                200,
                content=b"video bytes",
                headers={"content-type": "video/mp4", "content-length": "11"},
            )
        )

        r = client.get(f"/api/videos/{project_id}")

    assert r.status_code == 200
    assert r.headers["content-type"].startswith("video/mp4")
    assert r.content == b"video bytes"
    assert upstream.called
