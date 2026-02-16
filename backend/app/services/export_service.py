from __future__ import annotations

import json
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..models import Project, SceneMatch
from .gap_resolution import GapResolutionService
from .google_drive_service import GoogleDriveService
from .project_service import ProjectService


@dataclass
class ManifestEntry:
    relative_path: str
    source_path: Path | None = None
    inline_content: bytes | None = None
    mime_type: str = "application/octet-stream"


class ExportService:
    VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".webm"}
    _LANG_TO_LOCALE = {
        "fr": "fr_FR",
        "en": "en_GB",
        "es": "es_ES",
    }

    @classmethod
    def get_output_dir(cls, project_id: str) -> Path:
        return ProjectService.get_project_dir(project_id) / "output"

    @classmethod
    def get_assets_dir(cls) -> Path:
        return Path(__file__).resolve().parents[3] / "assets"

    @classmethod
    def sanitize_slug(cls, value: str) -> str:
        cleaned = re.sub(r"[^a-zA-Z0-9]+", "_", value).strip("_")
        return cleaned.lower() or "anime"

    @classmethod
    def output_folder_name(cls, project: Project) -> str:
        anime = cls.sanitize_slug(project.anime_name or "project")
        pid = re.sub(r"[^a-zA-Z0-9]+", "_", project.id).strip("_") or "unknown"
        return f"SPMAnime_{anime}_{pid}"

    @classmethod
    def language_to_locale(cls, language: str | None) -> str:
        if not language:
            return "fr_FR"
        lang = language.split("_")[0].lower()
        return cls._LANG_TO_LOCALE.get(lang, f"{lang}_{lang.upper()}")

    @classmethod
    def subtitle_filename(cls, project: Project) -> str:
        anime = cls.sanitize_slug(project.anime_name or "anime")
        locale = cls.language_to_locale(project.output_language)
        return f"{anime}.{locale}.srt"

    @classmethod
    def subtitle_path(cls, project: Project) -> Path:
        output_dir = cls.get_output_dir(project.id)
        named = output_dir / cls.subtitle_filename(project)
        if named.exists():
            return named
        legacy = output_dir / "subtitles.srt"
        if legacy.exists():
            return legacy
        return named

    @classmethod
    def _build_readme(
        cls,
        *,
        project: Project,
        source_mapping: dict[str, str],
        subtitle_filename: str,
    ) -> str:
        episode_list = "\n".join(f"  - {Path(bundle_item).name}" for bundle_item in source_mapping.values()) or "  - (none)"
        return f"""Anime TikTok Reproducer - Project Bundle
=========================================

Project ID: {project.id}
Anime: {project.anime_name or "Unknown"}

=== CONTENTS ===

import_project.jsx      - Premiere Pro automation script
tts_edited.wav          - Processed TTS audio
{subtitle_filename}     - Captions file
metadata/               - Generated metadata files (optional)
assets/                 - Required import assets
sources/                - Source episode files

=== SOURCE EPISODES ===
{episode_list}
"""

    @classmethod
    def build_manifest(cls, project: Project, matches: list[SceneMatch]) -> tuple[str, list[ManifestEntry]]:
        output_dir = cls.get_output_dir(project.id)
        if not output_dir.exists():
            raise FileNotFoundError("Processing output directory not found")

        jsx_path = output_dir / "import_project.jsx"
        tts_path = output_dir / "tts_edited.wav"
        subtitle_path = cls.subtitle_path(project)
        if not jsx_path.exists():
            raise FileNotFoundError("Missing output file: import_project.jsx")
        if not tts_path.exists():
            raise FileNotFoundError("Missing output file: tts_edited.wav")
        if not subtitle_path.exists():
            raise FileNotFoundError("Missing subtitle file. Run processing first.")

        folder = cls.output_folder_name(project)
        subtitle_name = subtitle_path.name
        entries: list[ManifestEntry] = [
            ManifestEntry(relative_path=f"{folder}/import_project.jsx", source_path=jsx_path),
            ManifestEntry(relative_path=f"{folder}/tts_edited.wav", source_path=tts_path),
            ManifestEntry(relative_path=f"{folder}/{subtitle_name}", source_path=subtitle_path),
        ]

        # Optional metadata files
        metadata_json = ProjectService.get_metadata_file(project.id)
        metadata_html = ProjectService.get_metadata_html_file(project.id)
        if metadata_json.exists():
            entries.append(
                ManifestEntry(
                    relative_path=f"{folder}/metadata/metadata.json",
                    source_path=metadata_json,
                    mime_type="application/json",
                )
            )
        if metadata_html.exists():
            entries.append(
                ManifestEntry(
                    relative_path=f"{folder}/metadata/metadata.html",
                    source_path=metadata_html,
                    mime_type="text/html",
                )
            )

        assets_dir = cls.get_assets_dir()
        for asset_name in ("TikTok60fps.sqpreset", "White border 5px.mogrt"):
            asset = assets_dir / asset_name
            if asset.exists():
                entries.append(
                    ManifestEntry(relative_path=f"{folder}/assets/{asset_name}", source_path=asset)
                )

        launcher = assets_dir / "run_in_premiere.bat"
        if launcher.exists():
            entries.append(ManifestEntry(relative_path=f"{folder}/run_in_premiere.bat", source_path=launcher))

        source_mapping: dict[str, str] = {}
        seen: set[str] = set()
        for match in matches:
            if not match.episode:
                continue
            resolved = GapResolutionService.resolve_episode_path(match.episode)
            if not resolved or not resolved.exists():
                continue
            key = str(resolved.resolve())
            if key in seen:
                continue
            seen.add(key)
            destination = f"{folder}/sources/{resolved.name}"
            source_mapping[key] = destination
            entries.append(ManifestEntry(relative_path=destination, source_path=resolved))

        entries.append(
            ManifestEntry(
                relative_path=f"{folder}/source_mapping.json",
                inline_content=json.dumps(source_mapping, indent=2).encode("utf-8"),
                mime_type="application/json",
            )
        )
        entries.append(
            ManifestEntry(
                relative_path=f"{folder}/README.txt",
                inline_content=cls._build_readme(
                    project=project,
                    source_mapping=source_mapping,
                    subtitle_filename=subtitle_name,
                ).encode("utf-8"),
                mime_type="text/plain",
            )
        )
        return folder, entries

    @classmethod
    def build_bundle(cls, project: Project, matches: list[SceneMatch]) -> Path:
        _, entries = cls.build_manifest(project, matches)
        bundle_path = ProjectService.get_project_dir(project.id) / "project_bundle.zip"
        with zipfile.ZipFile(bundle_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for entry in entries:
                if entry.source_path is not None:
                    zf.write(entry.source_path, entry.relative_path)
                else:
                    zf.writestr(entry.relative_path, entry.inline_content or b"")
        return bundle_path

    @classmethod
    def upload_manifest_to_drive(cls, project: Project, matches: list[SceneMatch]) -> dict[str, Any]:
        if not GoogleDriveService.is_configured():
            raise RuntimeError("Google Drive integration is not configured")

        drive = GoogleDriveService.client()
        _, entries = cls.build_manifest(project, matches)
        folder_id, folder_url = GoogleDriveService.ensure_project_folder(
            project.id,
            existing_folder_id=project.drive_folder_id,
            drive=drive,
        )

        # Keep drive folder architecture exactly in sync with the export manifest.
        GoogleDriveService.clear_folder(folder_id, drive=drive)

        # Cache parent folder IDs by relative path to avoid repeated Drive queries.
        parent_cache: dict[tuple[str, ...], str] = {tuple(): folder_id}

        def _resolve_parent(parts: list[str]) -> str:
            parent = folder_id
            prefix: list[str] = []
            for part in parts:
                prefix.append(part)
                key = tuple(prefix)
                if key in parent_cache:
                    parent = parent_cache[key]
                    continue
                parent = GoogleDriveService.ensure_subfolder(parent, part, drive=drive)
                parent_cache[key] = parent
            return parent

        for entry in entries:
            rel = Path(entry.relative_path)
            parts = list(rel.parts)
            filename = parts[-1]
            parent = folder_id
            if len(parts) > 1:
                # Preserve ZIP architecture inside Drive folder.
                parent = _resolve_parent(parts[:-1])
            if entry.source_path is not None:
                GoogleDriveService.upload_local_file(
                    parent_id=parent,
                    filename=filename,
                    local_path=entry.source_path,
                    drive=drive,
                )
            else:
                GoogleDriveService.upload_bytes(
                    parent_id=parent,
                    filename=filename,
                    content=entry.inline_content or b"",
                    mime_type=entry.mime_type,
                    drive=drive,
                )

        return {
            "folder_id": folder_id,
            "folder_url": folder_url,
            "file_count": len(entries),
        }

    @classmethod
    def detect_upload_video_in_drive_root(cls, folder_id: str) -> list[dict[str, Any]]:
        return GoogleDriveService.list_root_video_files(folder_id, cls.VIDEO_EXTENSIONS)
