"""Project duplication (script phase) and upload restrictions between duplicates.

A duplicated project copies the mother project's pipeline state up to the
script phase (video, scenes, matches, transcription, raw-scene data) and
starts the script phase from scratch with a preset template + output language.

Every duplicate records the family root in ``mother_project_id`` (never a
chain: duplicating a duplicate links back to the original root). The family
(root + all duplicates) is subject to upload restrictions:

- an account can NEVER upload two projects of the same family, ever;
- two same-language projects of the same family must be published at least
  ``MIN_SPACING_DAYS`` days apart (both directions), across all accounts.
"""
from __future__ import annotations

import logging
import os
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ..models import Project, ProjectPhase
from .project_service import ProjectService

logger = logging.getLogger(__name__)

# Script-phase outputs and caches: never copied — duplicates regenerate
# everything language/template dependent from scratch.
_EXCLUDED_FILES = {
    "project.json",  # rewritten from the model
    "new_script.json",
    "new_tts.wav",
    "preview.wav",
    "metadata.json",
    "metadata.html",
    "tts_alignment_manifest.json",
    "video_overlay.json",
}
_EXCLUDED_DIRS = {
    "output",
    "playback_cache_v3",
    "script_automation_runs",
    "tts_parts",
}
# Large immutable media: hardlink instead of copying when possible. Never
# hardlink JSON/state files — they are rewritten in place and would leak
# mutations into the mother project through the shared inode.
_HARDLINK_SUFFIXES = {".mp4", ".mkv", ".webm", ".mov", ".wav", ".m4a", ".mp3"}


@dataclass
class DuplicationVariant:
    language: str
    template: str


class ProjectDuplicationService:
    """Duplicate a project into template/language variants."""

    @classmethod
    def duplicate(
        cls, project_id: str, variants: list[DuplicationVariant]
    ) -> list[Project]:
        source = ProjectService.load(project_id)
        if source is None:
            raise ValueError("Project not found")
        if not variants:
            raise ValueError("At least one duplication variant is required")

        from .template_service import TemplateService

        for variant in variants:
            if not variant.language or not variant.language.strip():
                raise ValueError("Duplication variant language must be non-empty")
            TemplateService.get(variant.template)  # raises ValueError if unknown

        family_root = source.mother_project_id or source.id
        created: list[Project] = []
        for variant in variants:
            created.append(cls._duplicate_one(source, family_root, variant))
        return created

    @classmethod
    def _duplicate_one(
        cls, source: Project, family_root: str, variant: DuplicationVariant
    ) -> Project:
        new_id = uuid.uuid4().hex[:12]
        source_dir = ProjectService.get_project_dir(source.id)
        target_dir = ProjectService.get_project_dir(new_id)

        try:
            cls._copy_project_dir(source_dir, target_dir)
            duplicate = source.model_copy(
                update={
                    "id": new_id,
                    "mother_project_id": family_root,
                    "phase": ProjectPhase.SCRIPT_RESTRUCTURE,
                    "created_at": datetime.now(),
                    "updated_at": datetime.now(),
                    # Preset parameters for this variant.
                    "output_language": variant.language.strip().lower(),
                    "template": variant.template,
                    # Template-resolvable overrides are cleared so the preset
                    # template governs voice/music/LLM/min speed.
                    "voice_key": None,
                    "music_key": None,
                    "llm_preset": None,
                    "min_playback_speed": None,
                    "video_overlay": None,
                    # Output / integration / scheduling state starts fresh.
                    "drive_folder_id": None,
                    "drive_folder_url": None,
                    "drive_export_uploaded_once": False,
                    "generation_discord_message_id": None,
                    "final_upload_discord_message_id": None,
                    "upload_completed_at": None,
                    "upload_last_result": None,
                    "scheduled_at": None,
                    "scheduled_account_id": None,
                    "scheduled_slot": None,
                    "platform_schedules": {},
                    "reschedule_pending": {},
                }
            )
            ProjectService.save(duplicate)
            logger.info(
                "Duplicated project %s -> %s (language=%s template=%s root=%s)",
                source.id,
                new_id,
                variant.language,
                variant.template,
                family_root,
            )
            return duplicate
        except Exception:
            shutil.rmtree(target_dir, ignore_errors=True)
            raise

    @classmethod
    def _copy_project_dir(cls, source_dir, target_dir) -> None:
        target_dir.mkdir(parents=True, exist_ok=False)
        for entry in source_dir.iterdir():
            if entry.is_dir():
                if entry.name in _EXCLUDED_DIRS:
                    continue
                shutil.copytree(entry, target_dir / entry.name)
                continue
            if entry.name in _EXCLUDED_FILES:
                continue
            dest = target_dir / entry.name
            if entry.suffix.lower() in _HARDLINK_SUFFIXES:
                try:
                    os.link(entry, dest)
                    continue
                except OSError:
                    pass  # cross-device or unsupported FS: fall back to copy
            shutil.copy2(entry, dest)


class UploadRestrictionService:
    """Upload restrictions between linked duplicated projects."""

    MIN_SPACING_DAYS = 30

    @classmethod
    def family_members(
        cls, project: Project, all_projects: list[Project] | None = None
    ) -> list[Project]:
        """Other projects of the same duplication family (root included)."""
        root = project.mother_project_id or project.id
        projects = ProjectService.list_all() if all_projects is None else all_projects
        return [
            p
            for p in projects
            if p.id != project.id and (p.mother_project_id or p.id) == root
        ]

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @classmethod
    def _publish_datetimes(cls, member: Project) -> list[datetime]:
        """Datetimes at which a member is (or was) published on socials."""
        datetimes = [
            cls._as_utc(schedule.slot)
            for schedule in (member.platform_schedules or {}).values()
        ]
        if member.upload_completed_at is not None:
            datetimes.append(cls._as_utc(member.upload_completed_at))
        return datetimes

    @classmethod
    def blocked_account_ids(
        cls, project: Project, members: list[Project] | None = None
    ) -> dict[str, str]:
        """Accounts permanently blocked for `project`: {account_id: member_id}.

        Any account that uploaded (or is scheduled to upload) a family member
        can never upload this project.
        """
        members = cls.family_members(project) if members is None else members
        blocked: dict[str, str] = {}
        for member in members:
            if not member.scheduled_account_id:
                continue
            if member.upload_completed_at is None and not member.platform_schedules:
                continue
            blocked[member.scheduled_account_id] = member.id
        return blocked

    @classmethod
    def blocked_windows(
        cls, project: Project, members: list[Project] | None = None
    ) -> list[tuple[datetime, datetime, str]]:
        """(start, end, member_id) windows blocked by the 30-day language rule.

        Per-language: only family members sharing `project.output_language`
        contribute. Each publish datetime blocks +/- MIN_SPACING_DAYS.
        """
        language = project.output_language
        if not language:
            return []
        members = cls.family_members(project) if members is None else members
        spacing = timedelta(days=cls.MIN_SPACING_DAYS)
        windows: list[tuple[datetime, datetime, str]] = []
        for member in members:
            if member.output_language != language:
                continue
            for published_at in cls._publish_datetimes(member):
                windows.append((published_at - spacing, published_at + spacing, member.id))
        return windows

    @classmethod
    def validate_upload(
        cls,
        project: Project,
        account_id: str | None,
        candidate_datetimes: list[datetime],
    ) -> None:
        """Raise ValueError if the upload would violate duplication rules."""
        members = cls.family_members(project)
        if not members:
            return

        if account_id:
            blocked_accounts = cls.blocked_account_ids(project, members)
            if account_id in blocked_accounts:
                raise ValueError(
                    f"Account '{account_id}' already uploaded linked duplicated "
                    f"project '{blocked_accounts[account_id]}' — an account can "
                    "never upload two projects from the same duplication family"
                )

        windows = cls.blocked_windows(project, members)
        for candidate in candidate_datetimes:
            candidate_utc = cls._as_utc(candidate)
            for start, end, member_id in windows:
                if start <= candidate_utc <= end:
                    raise ValueError(
                        f"Publish time {candidate_utc.isoformat()} is within "
                        f"{cls.MIN_SPACING_DAYS} days of linked duplicated project "
                        f"'{member_id}' ({project.output_language}) — same-language "
                        "duplicates must be spaced by at least "
                        f"{cls.MIN_SPACING_DAYS} days"
                    )

    @classmethod
    def describe(cls, project: Project) -> dict:
        """JSON-friendly snapshot of restrictions for the frontend."""
        members = cls.family_members(project)
        blocked_accounts = cls.blocked_account_ids(project, members)
        windows = cls.blocked_windows(project, members)
        return {
            "mother_project_id": project.mother_project_id,
            "family_project_ids": [member.id for member in members],
            "blocked_accounts": [
                {"account_id": account_id, "linked_project_id": member_id}
                for account_id, member_id in sorted(blocked_accounts.items())
            ],
            "blocked_windows": [
                {
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "linked_project_id": member_id,
                }
                for start, end, member_id in windows
            ],
            "min_spacing_days": cls.MIN_SPACING_DAYS,
        }
