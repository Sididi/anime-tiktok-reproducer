from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models import PlatformSchedule, Project, ProjectPhase
from app.services.project_duplication_service import (
    DuplicationVariant,
    ProjectDuplicationService,
    UploadRestrictionService,
)
from app.services.project_service import ProjectService


@pytest.fixture()
def projects_dir(tmp_path: Path, monkeypatch) -> Path:
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
    monkeypatch.setattr(
        "app.services.project_service.settings.projects_dir", projects_dir
    )
    monkeypatch.setattr(
        "app.services.template_service.TemplateService.get",
        classmethod(lambda cls, key: _fake_template(key)),
    )
    return projects_dir


def _fake_template(key: str):
    if key not in {"classic", "zoomed", "squared"}:
        raise ValueError(f"Unknown template '{key}'")
    return object()


def _make_mother(projects_dir: Path, **overrides) -> Project:
    project = Project(
        id="mother000001",
        anime_name="Naruto",
        series_id="naruto",
        phase=ProjectPhase.SCRIPT_RESTRUCTURE,
        output_language="fr",
        template="classic",
        tts_speed=1.1,
        voice_key="some_voice",
        scheduled_account_id="acc_fr_1",
        drive_folder_id="drive123",
        **overrides,
    )
    project_dir = projects_dir / project.id
    project_dir.mkdir()
    ProjectService.save(project)
    return project


def _populate_mother_dir(projects_dir: Path, project_id: str) -> Path:
    project_dir = projects_dir / project_id
    (project_dir / "scenes.json").write_text("{}")
    (project_dir / "matches.json").write_text("{}")
    (project_dir / "transcription.json").write_text("{}")
    (project_dir / "raw_scene_detection.json").write_text("{}")
    (project_dir / "tiktok.mp4").write_bytes(b"fake video")
    # Script-phase outputs that must NOT be copied.
    (project_dir / "new_script.json").write_text("{}")
    (project_dir / "new_tts.wav").write_bytes(b"fake tts")
    (project_dir / "metadata.json").write_text("{}")
    (project_dir / "video_overlay.json").write_text("{}")
    (project_dir / "tts_parts").mkdir()
    (project_dir / "tts_parts" / "part1.mp3").write_bytes(b"x")
    (project_dir / "output").mkdir()
    (project_dir / "output" / "final.mp4").write_bytes(b"x")
    (project_dir / "playback_cache_v3").mkdir()
    (project_dir / "script_automation_runs").mkdir()
    return project_dir


def test_duplicate_copies_pipeline_state_and_resets_script_phase(projects_dir):
    mother = _make_mother(projects_dir)
    _populate_mother_dir(projects_dir, mother.id)

    created = ProjectDuplicationService.duplicate(
        mother.id,
        [
            DuplicationVariant(language="fr", template="zoomed"),
            DuplicationVariant(language="en", template="classic"),
        ],
    )

    assert len(created) == 2
    fr_dup, en_dup = created
    assert fr_dup.output_language == "fr" and fr_dup.template == "zoomed"
    assert en_dup.output_language == "en" and en_dup.template == "classic"

    for duplicate in created:
        assert duplicate.mother_project_id == mother.id
        assert duplicate.phase == ProjectPhase.SCRIPT_RESTRUCTURE
        # Template-resolvable overrides cleared, other settings inherited.
        assert duplicate.voice_key is None
        assert duplicate.video_overlay is None
        assert duplicate.tts_speed == 1.1
        assert duplicate.anime_name == "Naruto"
        # Output/scheduling state reset.
        assert duplicate.scheduled_account_id is None
        assert duplicate.platform_schedules == {}
        assert duplicate.drive_folder_id is None
        assert duplicate.upload_completed_at is None

        dup_dir = projects_dir / duplicate.id
        assert (dup_dir / "scenes.json").exists()
        assert (dup_dir / "matches.json").exists()
        assert (dup_dir / "transcription.json").exists()
        assert (dup_dir / "tiktok.mp4").exists()
        assert not (dup_dir / "new_script.json").exists()
        assert not (dup_dir / "new_tts.wav").exists()
        assert not (dup_dir / "metadata.json").exists()
        assert not (dup_dir / "video_overlay.json").exists()
        assert not (dup_dir / "tts_parts").exists()
        assert not (dup_dir / "output").exists()
        assert not (dup_dir / "playback_cache_v3").exists()

        # Reloadable from disk with the new identity.
        reloaded = ProjectService.load(duplicate.id)
        assert reloaded is not None
        assert reloaded.mother_project_id == mother.id


def test_duplicate_of_duplicate_links_back_to_root(projects_dir):
    mother = _make_mother(projects_dir)
    _populate_mother_dir(projects_dir, mother.id)

    first = ProjectDuplicationService.duplicate(
        mother.id, [DuplicationVariant(language="fr", template="zoomed")]
    )[0]
    second = ProjectDuplicationService.duplicate(
        first.id, [DuplicationVariant(language="en", template="classic")]
    )[0]

    assert second.mother_project_id == mother.id


def test_duplicate_starts_without_an_elevenlabs_seed(projects_dir):
    mother = _make_mother(projects_dir)
    mother.elevenlabs_seed = 123456789
    ProjectService.save(mother)

    duplicate = ProjectDuplicationService.duplicate(
        mother.id, [DuplicationVariant(language="fr", template="zoomed")]
    )[0]

    assert duplicate.elevenlabs_seed is None


def test_duplicate_rejects_unknown_template(projects_dir):
    mother = _make_mother(projects_dir)
    with pytest.raises(ValueError, match="Unknown template"):
        ProjectDuplicationService.duplicate(
            mother.id, [DuplicationVariant(language="fr", template="nope")]
        )
    # Nothing created.
    assert [p.id for p in ProjectService.list_all()] == [mother.id]


def _save_project(projects_dir: Path, project: Project) -> Project:
    (projects_dir / project.id).mkdir(exist_ok=True)
    ProjectService.save(project)
    return project


def test_family_members_include_mother_and_siblings(projects_dir):
    mother = _make_mother(projects_dir)
    dup_a = _save_project(
        projects_dir,
        Project(id="dupa00000001", mother_project_id=mother.id, output_language="fr"),
    )
    dup_b = _save_project(
        projects_dir,
        Project(id="dupb00000001", mother_project_id=mother.id, output_language="en"),
    )
    _save_project(projects_dir, Project(id="unrelated0001", output_language="fr"))

    member_ids = {m.id for m in UploadRestrictionService.family_members(dup_a)}
    assert member_ids == {mother.id, dup_b.id}

    member_ids = {m.id for m in UploadRestrictionService.family_members(mother)}
    assert member_ids == {dup_a.id, dup_b.id}


def test_account_rule_blocks_any_family_member_account_forever(projects_dir):
    mother = _make_mother(
        projects_dir,
        upload_completed_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
    )
    dup = _save_project(
        projects_dir,
        Project(id="dupa00000001", mother_project_id=mother.id, output_language="en"),
    )

    now = datetime(2026, 7, 6, tzinfo=timezone.utc)
    # Mother uploaded via acc_fr_1 -> blocked for the duplicate, even in
    # another language and long after the 30-day window.
    with pytest.raises(ValueError, match="acc_fr_1"):
        UploadRestrictionService.validate_upload(dup, "acc_fr_1", [now])
    # Other accounts pass (different language -> no 30-day window either).
    UploadRestrictionService.validate_upload(dup, "acc_en_1", [now])


def test_30_day_rule_is_per_language_and_symmetric(projects_dir):
    mother = _make_mother(projects_dir)
    slot = datetime(2026, 8, 1, 18, 0, tzinfo=timezone.utc)
    _save_project(
        projects_dir,
        Project(
            id="dupfr0000001",
            mother_project_id=mother.id,
            output_language="fr",
            scheduled_account_id="acc_fr_2",
            platform_schedules={
                "tiktok": PlatformSchedule(slot=slot, scheduled_at=slot)
            },
        ),
    )
    dup_fr = _save_project(
        projects_dir,
        Project(id="dupfr0000002", mother_project_id=mother.id, output_language="fr"),
    )
    dup_en = _save_project(
        projects_dir,
        Project(id="dupen0000001", mother_project_id=mother.id, output_language="en"),
    )

    # Same language: blocked 30 days after AND before the sibling's slot.
    with pytest.raises(ValueError, match="30 days"):
        UploadRestrictionService.validate_upload(dup_fr, None, [slot + timedelta(days=10)])
    with pytest.raises(ValueError, match="30 days"):
        UploadRestrictionService.validate_upload(dup_fr, None, [slot - timedelta(days=10)])
    # Outside the window: allowed.
    UploadRestrictionService.validate_upload(dup_fr, None, [slot + timedelta(days=31)])
    # Different language: never blocked by the FR sibling.
    UploadRestrictionService.validate_upload(dup_en, None, [slot + timedelta(days=1)])


def test_describe_exposes_windows_and_blocked_accounts(projects_dir):
    mother = _make_mother(
        projects_dir,
        upload_completed_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
    )
    dup = _save_project(
        projects_dir,
        Project(id="dupfr0000001", mother_project_id=mother.id, output_language="fr"),
    )

    described = UploadRestrictionService.describe(dup)
    assert described["mother_project_id"] == mother.id
    assert described["family_project_ids"] == [mother.id]
    assert described["blocked_accounts"] == [
        {"account_id": "acc_fr_1", "linked_project_id": mother.id}
    ]
    assert described["min_spacing_days"] == 30
    assert len(described["blocked_windows"]) == 1
    window = described["blocked_windows"][0]
    assert window["linked_project_id"] == mother.id
    assert window["start"] == "2026-04-01T00:00:00+00:00"
    assert window["end"] == "2026-05-31T00:00:00+00:00"


def test_projects_without_family_have_no_restrictions(projects_dir):
    lone = _save_project(
        projects_dir,
        Project(id="lone00000001", output_language="fr"),
    )
    UploadRestrictionService.validate_upload(
        lone, "acc_fr_1", [datetime.now(timezone.utc)]
    )
    described = UploadRestrictionService.describe(lone)
    assert described["family_project_ids"] == []
    assert described["blocked_accounts"] == []
    assert described["blocked_windows"] == []
