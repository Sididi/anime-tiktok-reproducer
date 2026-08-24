"""Tests for CepLinkService (Premiere Link client + durable retry)."""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
import pytest
import respx

from app.models import Project
from app.services.cep_link_service import CepLinkService
from app.services.project_service import ProjectService

BASE = "https://tiktok.sididi.tv"
LAUNCH_URL = f"{BASE}/api/internal/cep/launches"


@pytest.fixture(autouse=True)
def _vps_env(monkeypatch):
    monkeypatch.setattr("app.services.discord_service.settings.tiktok_server_base_url", BASE)
    monkeypatch.setattr(
        "app.services.discord_service.settings.tiktok_server_internal_token", "internal_secret"
    )
    monkeypatch.setattr("app.services.cep_link_service.settings.cep_link_enabled", True)


@pytest.fixture
def project_dir(tmp_path: Path, monkeypatch):
    pdir = tmp_path / "projects"
    pdir.mkdir()
    monkeypatch.setattr("app.services.project_service.settings.projects_dir", pdir)
    return pdir


def _project(pid: str = "p1") -> Project:
    project = Project(id=pid)
    project.anime_name = "One Piece"
    return project


def _seed(pid: str, *, requested_at: datetime, retries: int = 0, last_attempt: datetime | None = None) -> Project:
    project = _project(pid)
    project.cep_launch_request = {
        "project_id": pid,
        "requested_at": requested_at.isoformat(),
        "anime_title": "One Piece",
        "discord_message_id": "m1",
        "discord_content": "hello",
        "retries": retries,
        "last_error": "boom",
        "last_attempt_at": (last_attempt or datetime.now(timezone.utc)).isoformat(),
    }
    ProjectService.get_project_dir(project.id).mkdir(parents=True, exist_ok=True)
    ProjectService.save(project)
    return project


# ---- request_launch ---------------------------------------------------------


@respx.mock
def test_request_launch_posts_payload_with_bearer():
    route = respx.post(LAUNCH_URL).mock(
        return_value=httpx.Response(
            202, json={"launch_id": "l_1", "status": "pending", "connected": True, "delivered": True}
        )
    )
    project = _project()
    ok = CepLinkService.request_launch(project, discord_message_id="m1", discord_content="hello\nworld")
    assert ok is True
    assert project.cep_launch_request is None
    sent = route.calls.last.request
    assert sent.headers["Authorization"] == "Bearer internal_secret"
    body = json.loads(sent.content)
    assert body["project_id"] == "p1"
    assert body["anime_title"] == "One Piece"
    assert body["discord_message_id"] == "m1"
    assert body["discord_content"] == "hello\nworld"
    datetime.fromisoformat(body["requested_at"])  # ISO timestamp


@respx.mock
def test_request_launch_parks_request_on_http_error():
    respx.post(LAUNCH_URL).mock(return_value=httpx.Response(503, text="down"))
    project = _project()
    assert CepLinkService.request_launch(project, discord_message_id=None, discord_content="c") is False
    parked = project.cep_launch_request
    assert parked["project_id"] == "p1"
    assert parked["retries"] == 0
    assert "503" in parked["last_error"]
    assert parked["discord_message_id"] is None
    datetime.fromisoformat(parked["last_attempt_at"])


@respx.mock
def test_request_launch_parks_request_on_connect_error():
    respx.post(LAUNCH_URL).mock(side_effect=httpx.ConnectError("unreachable"))
    project = _project()
    assert CepLinkService.request_launch(project, discord_message_id="m1", discord_content="c") is False
    assert project.cep_launch_request["retries"] == 0


@respx.mock
def test_request_launch_noop_when_disabled(monkeypatch):
    monkeypatch.setattr("app.services.cep_link_service.settings.cep_link_enabled", False)
    route = respx.post(LAUNCH_URL).mock(return_value=httpx.Response(202, json={}))
    project = _project()
    assert CepLinkService.request_launch(project, discord_message_id="m1", discord_content="c") is False
    assert project.cep_launch_request is None
    assert not route.called


def test_is_configured_requires_vps(monkeypatch):
    assert CepLinkService.is_configured() is True
    monkeypatch.setattr("app.services.discord_service.settings.tiktok_server_base_url", None)
    assert CepLinkService.is_configured() is False


# ---- delete_launch ----------------------------------------------------------


@respx.mock
def test_delete_launch_tolerates_404_and_errors():
    route = respx.delete(f"{BASE}/api/internal/cep/launches/p1").mock(return_value=httpx.Response(404))
    assert CepLinkService.delete_launch("p1") is True
    assert route.called
    respx.delete(f"{BASE}/api/internal/cep/launches/p2").mock(side_effect=httpx.ConnectError("down"))
    assert CepLinkService.delete_launch("p2") is None  # swallowed


# ---- retry loop -------------------------------------------------------------


@respx.mock
def test_retry_once_clears_entry_on_success(project_dir):
    route = respx.post(LAUNCH_URL).mock(return_value=httpx.Response(202, json={"launch_id": "l_2"}))
    now = datetime.now(timezone.utc)
    _seed("p1", requested_at=now - timedelta(minutes=30), last_attempt=now - timedelta(minutes=10))

    asyncio.run(CepLinkService.retry_once())

    assert ProjectService.load("p1").cep_launch_request is None
    body = json.loads(route.calls.last.request.content)
    assert set(body) == {"project_id", "requested_at", "anime_title", "discord_message_id", "discord_content"}
    assert body["discord_message_id"] == "m1"


@respx.mock
def test_retry_once_respects_backoff(project_dir):
    route = respx.post(LAUNCH_URL).mock(return_value=httpx.Response(202, json={}))
    now = datetime.now(timezone.utc)
    _seed("p1", requested_at=now, retries=0, last_attempt=now)  # 1 min backoff not elapsed

    asyncio.run(CepLinkService.retry_once())

    assert not route.called
    assert ProjectService.load("p1").cep_launch_request["retries"] == 0


@respx.mock
def test_retry_once_increments_and_alerts_at_threshold(project_dir):
    respx.post(LAUNCH_URL).mock(return_value=httpx.Response(503, text="down"))
    now = datetime.now(timezone.utc)
    _seed("p1", requested_at=now - timedelta(hours=2), retries=4, last_attempt=now - timedelta(hours=1))

    with patch("app.services.reschedule_retry_service._post_discord_alert") as alert:
        asyncio.run(CepLinkService.retry_once())

    entry = ProjectService.load("p1").cep_launch_request
    assert entry["retries"] == 5
    assert "503" in entry["last_error"]
    alert.assert_called_once()
    assert "[premiere-link] project=p1" in alert.call_args.args[0]


@respx.mock
def test_retry_once_drops_requests_older_than_ttl(project_dir):
    route = respx.post(LAUNCH_URL).mock(return_value=httpx.Response(202, json={}))
    now = datetime.now(timezone.utc)
    _seed("p1", requested_at=now - timedelta(days=8), last_attempt=now - timedelta(days=1))

    asyncio.run(CepLinkService.retry_once())

    assert not route.called
    assert ProjectService.load("p1").cep_launch_request is None


@respx.mock
def test_retry_once_skips_projects_without_request(project_dir):
    route = respx.post(LAUNCH_URL).mock(return_value=httpx.Response(202, json={}))
    project = _project("p1")
    ProjectService.get_project_dir(project.id).mkdir(parents=True, exist_ok=True)
    ProjectService.save(project)
    asyncio.run(CepLinkService.retry_once())
    assert not route.called
