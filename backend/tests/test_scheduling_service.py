from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models import Project
from app.services.account_service import AccountService
from app.services.project_service import ProjectService
from app.services.scheduling_service import SchedulingService


def test_instagram_shared_pool_uses_canonical_slot_not_jitter(
    tmp_path: Path, monkeypatch,
):
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
    accounts_config = tmp_path / "accounts.yaml"
    accounts_config.write_text(
        """\
accounts:
  anime_fr_2:
    name: "Anime v2"
    language: "fr"
    device: "poco"
    slots: ["22:00"]
    meta:
      facebook_page_id: "fb_page"
      facebook_page_access_token: "token"
      instagram_business_account_id: "ig_shared"
  anime_fr_4:
    name: "Anime v4"
    language: "fr"
    device: "poco"
    slots: ["22:00"]
    meta:
      facebook_page_id: "fb_page"
      facebook_page_access_token: "token"
      instagram_business_account_id: "ig_shared"
""",
        encoding="utf-8",
    )
    monkeypatch.setattr("app.services.project_service.settings.projects_dir", projects_dir)
    monkeypatch.setattr(
        "app.services.account_service.settings.accounts_config_path", accounts_config
    )
    monkeypatch.setattr(
        SchedulingService,
        "_earliest_allowed_publish_time",
        classmethod(lambda cls: datetime(2026, 4, 29, 0, 0, tzinfo=timezone.utc)),
    )
    monkeypatch.setattr(
        "app.services.scheduling_service.datetime",
        _FixedDateTime,
    )
    AccountService.invalidate()

    (projects_dir / "p1").mkdir()
    (projects_dir / "p2").mkdir()
    ProjectService.save(Project(id="p1", anime_name="One", output_language="fr"))
    ProjectService.save(Project(id="p2", anime_name="Two", output_language="fr"))

    first = SchedulingService.reserve_all_platform_slots(
        "p1", "anime_fr_2", ["instagram"]
    )
    second = SchedulingService.reserve_all_platform_slots(
        "p2", "anime_fr_4", ["instagram"]
    )

    assert first["instagram"][0] == datetime(2026, 4, 29, 22, 0, tzinfo=timezone.utc)
    assert second["instagram"][0] == datetime(2026, 4, 30, 22, 0, tzinfo=timezone.utc)
    assert first["instagram"][1].minute != second["instagram"][1].minute or (
        first["instagram"][1].date() != second["instagram"][1].date()
    )


class _FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        current = datetime(2026, 4, 29, 12, 0, tzinfo=timezone.utc)
        return current if tz is None else current.astimezone(tz)
