from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi.testclient import TestClient

from app.models import PlatformSchedule, Project
from app.services.account_service import AccountService
from app.services.project_service import ProjectService
from app.services.scheduling_service import SchedulingService


_NOW = datetime(2026, 5, 7, 12, 0, tzinfo=timezone.utc)


class _FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return _NOW if tz is None else _NOW.astimezone(tz)


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
    cfg = tmp_path / "accounts.yaml"
    cfg.write_text("""\
accounts:
  acc_a:
    name: "A"
    language: "fr"
    device: "poco"
    slots: ["14:00", "18:00"]
    youtube:
      refresh_token: "tok"
    tiktok:
      slots: ["12:00", "14:00", "18:00"]
""", encoding="utf-8")
    monkeypatch.setattr("app.services.project_service.settings.projects_dir", projects_dir)
    monkeypatch.setattr("app.services.account_service.settings.accounts_config_path", cfg)
    monkeypatch.setattr("app.services.scheduling_service.datetime", _FixedDateTime)
    AccountService.invalidate()

    from app.main import app  # noqa: PLC0415
    with TestClient(app) as c:
        yield c
    AccountService.invalidate()


def test_list_events_returns_filtered_events(client):
    project = Project(id="p1", anime_name="Show",
        scheduled_account_id="acc_a",
        platform_schedules={
            "tiktok": PlatformSchedule(
                slot=datetime(2026, 5, 7, 18, 0, tzinfo=timezone.utc),
                scheduled_at=datetime(2026, 5, 7, 18, 5, tzinfo=timezone.utc),
            )
        }
    )
    ProjectService.get_project_dir(project.id).mkdir(parents=True, exist_ok=True)
    ProjectService.save(project)
    r = client.get("/api/scheduling/events", params={"range_start": _NOW.isoformat()})
    assert r.status_code == 200
    events = r.json()["events"]
    assert any(e["project_id"] == "p1" and e["platform"] == "tiktok" for e in events)


def test_free_slots_endpoint(client):
    r = client.get("/api/scheduling/free-slots", params={
        "account_id": "acc_a", "platform": "tiktok",
        "after": _NOW.isoformat(), "limit": 4,
    })
    assert r.status_code == 200
    slots = r.json()["slots"]
    assert len(slots) == 4
    assert all("slot" in s and "available" in s for s in slots)


def test_resolve_anchor_endpoint(client):
    p = Project(id="p1")
    ProjectService.get_project_dir(p.id).mkdir(parents=True, exist_ok=True)
    ProjectService.save(p)
    r = client.post("/api/scheduling/resolve-anchor", json={
        "project_id": "p1",
        "account_id": "acc_a",
        "tiktok_slot": datetime(2026, 5, 7, 14, 0, tzinfo=timezone.utc).isoformat(),
    })
    assert r.status_code == 200
    body = r.json()
    assert "tiktok" in body["resolved"]
    assert body["conflicts"] == []


def test_reserve_anchor_endpoint(client):
    p = Project(id="p1")
    ProjectService.get_project_dir(p.id).mkdir(parents=True, exist_ok=True)
    ProjectService.save(p)
    r = client.post("/api/scheduling/projects/p1/reserve-anchor", json={
        "account_id": "acc_a",
        "tiktok_slot": datetime(2026, 5, 7, 14, 0, tzinfo=timezone.utc).isoformat(),
    })
    assert r.status_code == 200
    schedules = r.json()["platform_schedules"]
    assert "tiktok" in schedules


def test_patch_platform_endpoint(client):
    p = Project(id="p1")
    ProjectService.get_project_dir(p.id).mkdir(parents=True, exist_ok=True)
    ProjectService.save(p)
    SchedulingService.reserve_anchor(
        "p1", "acc_a", datetime(2026, 5, 7, 14, 0, tzinfo=timezone.utc)
    )
    r = client.patch(
        "/api/scheduling/projects/p1/platforms/youtube",
        json={"new_slot": datetime(2026, 5, 8, 14, 0, tzinfo=timezone.utc).isoformat()},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["slot"].startswith("2026-05-08T14:00:00")


def test_delete_platform_endpoint(client):
    p = Project(id="p1")
    ProjectService.get_project_dir(p.id).mkdir(parents=True, exist_ok=True)
    ProjectService.save(p)
    SchedulingService.reserve_anchor(
        "p1", "acc_a", datetime(2026, 5, 7, 14, 0, tzinfo=timezone.utc)
    )
    r = client.delete("/api/scheduling/projects/p1/platforms/youtube")
    assert r.status_code == 204
    project = ProjectService.load("p1")
    assert "youtube" not in project.platform_schedules


def test_delete_all_endpoint(client):
    p = Project(id="p1")
    ProjectService.get_project_dir(p.id).mkdir(parents=True, exist_ok=True)
    ProjectService.save(p)
    SchedulingService.reserve_anchor(
        "p1", "acc_a", datetime(2026, 5, 7, 14, 0, tzinfo=timezone.utc)
    )
    r = client.delete("/api/scheduling/projects/p1/all")
    assert r.status_code == 204
    project = ProjectService.load("p1")
    assert project.platform_schedules == {}


def test_cascade_preview_endpoint(client):
    other = Project(id="other", scheduled_account_id="acc_a",
        anime_name="Other",
        platform_schedules={
            "tiktok": PlatformSchedule(
                slot=datetime(2026, 5, 7, 14, 0, tzinfo=timezone.utc),
                scheduled_at=datetime(2026, 5, 7, 14, 6, tzinfo=timezone.utc),
            )
        }
    )
    ProjectService.get_project_dir(other.id).mkdir(parents=True, exist_ok=True)
    ProjectService.save(other)
    urgent = Project(id="urgent", anime_name="Urgent")
    ProjectService.get_project_dir(urgent.id).mkdir(parents=True, exist_ok=True)
    ProjectService.save(urgent)

    r = client.post("/api/scheduling/projects/urgent/cascade-preview",
                    json={"account_id": "acc_a"})
    assert r.status_code == 200
    body = r.json()
    tt = next(p for p in body["per_platform"] if p["platform"] == "tiktok")
    assert len(tt["displaced"]) == 1


def test_cascade_apply_endpoint(client):
    other = Project(id="other", scheduled_account_id="acc_a",
        anime_name="Other",
        platform_schedules={
            "tiktok": PlatformSchedule(
                slot=datetime(2026, 5, 7, 14, 0, tzinfo=timezone.utc),
                scheduled_at=datetime(2026, 5, 7, 14, 6, tzinfo=timezone.utc),
            )
        }
    )
    ProjectService.get_project_dir(other.id).mkdir(parents=True, exist_ok=True)
    ProjectService.save(other)
    urgent = Project(id="urgent", anime_name="Urgent")
    ProjectService.get_project_dir(urgent.id).mkdir(parents=True, exist_ok=True)
    ProjectService.save(urgent)

    r = client.post("/api/scheduling/projects/urgent/cascade-apply",
                    json={"account_id": "acc_a"})
    assert r.status_code == 200
    other = ProjectService.load("other")
    assert other.platform_schedules["tiktok"].slot == datetime(
        2026, 5, 7, 18, 0, tzinfo=timezone.utc
    )


def test_reschedule_pending_endpoint(client):
    project = Project(id="p1")
    project.reschedule_pending = {
        "youtube": {
            "target_scheduled_at": datetime(2026, 5, 7, 14, 0, tzinfo=timezone.utc),
            "retries": 2,
            "last_error": "503",
            "last_attempt_at": datetime(2026, 5, 7, 14, 5, tzinfo=timezone.utc),
        }
    }
    ProjectService.get_project_dir(project.id).mkdir(parents=True, exist_ok=True)
    ProjectService.save(project)
    r = client.get("/api/scheduling/reschedule-pending")
    assert r.status_code == 200
    items = r.json()["items"]
    assert any(i["project_id"] == "p1" and i["platform"] == "youtube" for i in items)


def test_router_disabled_when_flag_off(client, monkeypatch):
    monkeypatch.setattr(
        "app.api.routes.scheduling.app_settings.scheduling_v2_enabled", False
    )
    r = client.get("/api/scheduling/events")
    assert r.status_code == 503


from datetime import timedelta


def _mk_project(pid: str, **kwargs) -> Project:
    p = Project(id=pid, **kwargs)
    ProjectService.get_project_dir(p.id).mkdir(parents=True, exist_ok=True)
    ProjectService.save(p)
    return p


def test_reserve_manual_route_and_planning_flag(client):
    _mk_project("p1", anime_name="Show")
    at = _NOW + timedelta(hours=3)
    resp = client.post(
        "/api/scheduling/projects/p1/reserve-manual",
        json={"account_id": "acc_a", "at": at.isoformat(), "platforms": ["tiktok"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["platform_schedules"]["tiktok"]["manual"] is True
    assert body["platform_schedules"]["tiktok"]["slot"] == at.isoformat()
    assert "notification_status" in body

    events = client.get(
        "/api/scheduling/events", params={"range_start": _NOW.isoformat()}
    ).json()["events"]
    ev = next(e for e in events if e["project_id"] == "p1")
    assert ev["manual"] is True


def test_reserve_manual_route_rejects_too_close(client):
    _mk_project("p1")
    at = _NOW + timedelta(minutes=5)
    resp = client.post(
        "/api/scheduling/projects/p1/reserve-manual",
        json={"account_id": "acc_a", "at": at.isoformat(), "platforms": ["tiktok"]},
    )
    assert resp.status_code == 422
    assert "slot_too_close" in resp.text


def _seed_pool_b_c():
    """projB @ 2026-05-08 12:00, projC @ 14:00, 18:00 free, plus 'me'."""
    slot1 = datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc)
    slot2 = datetime(2026, 5, 8, 14, 0, tzinfo=timezone.utc)
    _mk_project("projB", anime_name="B", scheduled_account_id="acc_a",
        platform_schedules={"tiktok": PlatformSchedule(slot=slot1, scheduled_at=slot1)})
    _mk_project("projC", anime_name="C", scheduled_account_id="acc_a",
        platform_schedules={"tiktok": PlatformSchedule(slot=slot2, scheduled_at=slot2)})
    _mk_project("me")
    return slot1


def test_switch_preview_and_apply(client):
    slot1 = _seed_pool_b_c()
    resp = client.post(
        "/api/scheduling/projects/me/switch-preview",
        json={"account_id": "acc_a", "platform": "tiktok", "slot": slot1.isoformat()},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["occupant_project_id"] == "projB"
    assert len(body["cascade"]["displaced"]) == 2      # B->14 pushes C->18
    assert len(body["next_free"]["displaced"]) == 1    # B jumps to 18

    resp = client.post(
        "/api/scheduling/projects/me/switch-apply",
        json={
            "account_id": "acc_a", "platform": "tiktok", "slot": slot1.isoformat(),
            "mode": "next_free", "expected_occupant_id": "projB",
        },
    )
    assert resp.status_code == 200
    assert "projB" in resp.json()["notification_status"]
    assert ProjectService.load("me").platform_schedules["tiktok"].slot == slot1


def test_switch_apply_stale_occupant_409(client):
    slot1 = _seed_pool_b_c()
    resp = client.post(
        "/api/scheduling/projects/me/switch-apply",
        json={
            "account_id": "acc_a", "platform": "tiktok", "slot": slot1.isoformat(),
            "mode": "cascade", "expected_occupant_id": "wrong",
        },
    )
    assert resp.status_code == 409


def test_reserve_anchor_with_steals_route(client):
    slot1 = _seed_pool_b_c()
    resp = client.post(
        "/api/scheduling/projects/me/reserve-anchor",
        json={
            "account_id": "acc_a",
            "tiktok_slot": slot1.isoformat(),
            "steals": {
                "tiktok": {"mode": "cascade", "expected_occupant_id": "projB"}
            },
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["platform_schedules"]["tiktok"]["slot"] == slot1.isoformat()
    assert "projB" in body["notification_status"]["tiktok"]
    # stale occupant -> 409, nothing moved
    resp = client.post(
        "/api/scheduling/projects/projC/reserve-anchor",
        json={
            "account_id": "acc_a",
            "tiktok_slot": slot1.isoformat(),
            "steals": {"tiktok": {"mode": "cascade", "expected_occupant_id": "wrong"}},
        },
    )
    assert resp.status_code == 409


def _save_project_with_tiktok(pid, scheduled_at):
    project = Project(
        id=pid, anime_name="Show", scheduled_account_id="acc_a",
        platform_schedules={
            "tiktok": PlatformSchedule(slot=scheduled_at, scheduled_at=scheduled_at),
        },
    )
    ProjectService.get_project_dir(project.id).mkdir(parents=True, exist_ok=True)
    ProjectService.save(project)
    return project


def test_patch_platform_locked_returns_423(client):
    # tiktok at _NOW + 5min → lock window opened at _NOW - 5min → locked now
    locked_at = datetime(2026, 5, 7, 12, 5, tzinfo=timezone.utc)
    _save_project_with_tiktok("plock", locked_at)
    r = client.patch(
        "/api/scheduling/projects/plock/platforms/tiktok",
        json={"new_slot": datetime(2026, 5, 7, 18, 0, tzinfo=timezone.utc).isoformat()},
    )
    assert r.status_code == 423
    assert "timing_locked" in r.text


def test_patch_anchor_locked_returns_423(client):
    locked_at = datetime(2026, 5, 7, 12, 5, tzinfo=timezone.utc)
    _save_project_with_tiktok("plock2", locked_at)
    r = client.patch(
        "/api/scheduling/projects/plock2/anchor",
        json={"tiktok_slot": datetime(2026, 5, 7, 18, 0, tzinfo=timezone.utc).isoformat()},
    )
    assert r.status_code == 423


def test_events_include_timing_locked_flag(client):
    _save_project_with_tiktok("plocked", datetime(2026, 5, 7, 12, 5, tzinfo=timezone.utc))
    _save_project_with_tiktok("pfree", datetime(2026, 5, 7, 18, 0, tzinfo=timezone.utc))
    r = client.get("/api/scheduling/events", params={"range_start": _NOW.isoformat()})
    assert r.status_code == 200
    events = {e["project_id"]: e for e in r.json()["events"]}
    assert events["plocked"]["timing_locked"] is True
    assert events["pfree"]["timing_locked"] is False
