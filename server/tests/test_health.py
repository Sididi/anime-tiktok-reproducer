"""Tests for /healthz."""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient


def test_healthz_returns_status_ok(
    monkeypatch, example_yaml: Path, example_env, tmp_server_dir: Path
):
    monkeypatch.setenv("ATR_TIKTOK_SERVER_CONFIG_PATH", str(example_yaml))
    monkeypatch.setenv("ATR_TIKTOK_SERVER_AVATARS_DIR", str(tmp_server_dir / "avatars"))
    monkeypatch.setenv("ATR_TIKTOK_SERVER_DATA_DIR", str(tmp_server_dir / "data"))
    from app.main import create_app

    app = create_app()
    with TestClient(app) as client:
        r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "jobs_pending" in body
