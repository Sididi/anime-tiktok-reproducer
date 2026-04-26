"""Tests for app.auth.dependencies."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.auth.dependencies import require_device_token, require_internal_token
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

    @a.get("/mobile")
    async def mobile_route(device_id: str = Depends(require_device_token)):
        return {"device_id": device_id}

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


def test_mobile_route_returns_resolved_device(app: FastAPI):
    client = TestClient(app)
    r = client.get("/mobile", headers={"Authorization": "Bearer mobile_secret"})
    assert r.status_code == 200
    assert r.json() == {"device_id": "iphone_13_pro"}


def test_mobile_route_rejects_unknown_token(app: FastAPI):
    client = TestClient(app)
    r = client.get("/mobile", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401
