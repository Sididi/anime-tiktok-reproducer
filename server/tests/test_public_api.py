"""Tests for /api/avatars/*."""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient


def _make_app(monkeypatch, example_yaml: Path, example_env, tmp_server_dir: Path):
    monkeypatch.setenv("ATR_TIKTOK_SERVER_CONFIG_PATH", str(example_yaml))
    monkeypatch.setenv("ATR_TIKTOK_SERVER_AVATARS_DIR", str(tmp_server_dir / "avatars"))
    monkeypatch.setenv("ATR_TIKTOK_SERVER_DATA_DIR", str(tmp_server_dir / "data"))
    from app.main import create_app

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
