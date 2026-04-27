"""Tests for app.auth.dependencies."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.auth.dependencies import require_internal_token
from app.config import Settings


def _make_settings(example_yaml: Path, avatars_dir: Path) -> Settings:
    return Settings.load(config_path=example_yaml, avatars_dir=avatars_dir)


@pytest.fixture
def app(example_yaml: Path, example_env, tmp_server_dir: Path) -> FastAPI:
    settings = _make_settings(example_yaml, tmp_server_dir / "avatars")
    a = FastAPI()
    a.state.settings = settings

    @a.get("/internal", dependencies=[Depends(require_internal_token)])
    async def internal_route():
        return {"ok": True}

    return a


def test_internal_route_rejects_missing_auth(app: FastAPI):
    client = TestClient(app)
    r = client.get("/internal")
    assert r.status_code == 401


def test_internal_route_rejects_wrong_token(app: FastAPI):
    client = TestClient(app)
    r = client.get("/internal", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401


def test_internal_route_accepts_correct_token(app: FastAPI):
    client = TestClient(app)
    r = client.get("/internal", headers={"Authorization": "Bearer internal_secret"})
    assert r.status_code == 200
