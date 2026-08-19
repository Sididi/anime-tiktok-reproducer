"""PFM status sync: resolves backend-owned TikTok posts around publish time."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models import Project
from app.models.project import TikTokPfmState
from app.services.pfm_status_sync_service import PfmStatusSyncService
from app.services.post_for_me_client import PfmPublishOutcome, PostForMeClient
from app.services.project_service import ProjectService


@pytest.fixture()
def projects_dir(tmp_path, monkeypatch):
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
    monkeypatch.setattr(
        "app.services.project_service.settings.projects_dir", projects_dir
    )
    return projects_dir


def _save(pid: str, state: TikTokPfmState) -> Project:
    project = Project(id=pid, anime_name=pid, tiktok_pfm=state)
    ProjectService.get_project_dir(pid).mkdir(parents=True, exist_ok=True)
    ProjectService.save(project)
    return project


def test_sync_marks_published_and_persists_result(projects_dir, monkeypatch):
    _save(
        "p1",
        TikTokPfmState(
            post_id="sp_1",
            stage="post_scheduled",
            social_account_id="spc_1",
            scheduled_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        ),
    )
    monkeypatch.setattr(
        PostForMeClient,
        "fetch_outcome",
        classmethod(
            lambda cls, post_id, social_account_id=None: PfmPublishOutcome(
                success=True, url="https://www.tiktok.com/@u/video/1"
            )
        ),
    )
    PfmStatusSyncService.sync_once()
    saved = ProjectService.load("p1")
    assert saved.tiktok_pfm.stage == "published"
    assert saved.tiktok_pfm.url == "https://www.tiktok.com/@u/video/1"
    entry = next(
        e
        for e in saved.upload_last_result["platforms"]
        if e["platform"] == "tiktok"
    )
    assert entry["status"] == "uploaded"
    assert entry["source"] == "pfm"


def test_sync_skips_not_yet_due_posts(projects_dir, monkeypatch):
    _save(
        "p1",
        TikTokPfmState(
            post_id="sp_1",
            stage="post_scheduled",
            scheduled_at=datetime.now(timezone.utc) + timedelta(days=3),
        ),
    )
    calls: list[str] = []
    monkeypatch.setattr(
        PostForMeClient,
        "fetch_outcome",
        classmethod(lambda cls, post_id, social_account_id=None: calls.append(post_id)),
    )
    PfmStatusSyncService.sync_once()
    assert calls == []


def test_sync_pending_past_slot_promotes_to_processing(projects_dir, monkeypatch):
    _save(
        "p1",
        TikTokPfmState(
            post_id="sp_1",
            stage="post_scheduled",
            scheduled_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        ),
    )
    monkeypatch.setattr(
        PostForMeClient,
        "fetch_outcome",
        classmethod(lambda cls, post_id, social_account_id=None: None),
    )
    PfmStatusSyncService.sync_once()
    saved = ProjectService.load("p1")
    # Past the publish instant with no result yet: the post is processing —
    # reschedule paths must treat it as immutable.
    assert saved.tiktok_pfm.stage == "post_created"


def test_sync_failure_persists_and_pings(projects_dir, monkeypatch):
    _save(
        "p1",
        TikTokPfmState(
            post_id="sp_1",
            stage="post_created",
        ),
    )
    monkeypatch.setattr(
        PostForMeClient,
        "fetch_outcome",
        classmethod(
            lambda cls, post_id, social_account_id=None: PfmPublishOutcome(
                success=False, detail="boom [reached_active_user_cap]"
            )
        ),
    )
    pings: list[str] = []
    monkeypatch.setattr(
        "app.services.discord_service.DiscordService.post_message",
        classmethod(lambda cls, content: pings.append(content)),
    )
    PfmStatusSyncService.sync_once()
    saved = ProjectService.load("p1")
    assert saved.tiktok_pfm.stage == "failed"
    entry = next(
        e
        for e in saved.upload_last_result["platforms"]
        if e["platform"] == "tiktok"
    )
    assert entry["status"] == "failed"
    assert pings and "reached_active_user_cap" in pings[0]
