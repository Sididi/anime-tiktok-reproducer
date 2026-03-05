from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import logging
import re
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import settings
from ..models import Project, SceneMatch
from .gap_resolution import GapResolutionService
from .google_drive_service import GoogleDriveService
from .music_config_service import MusicConfigService
from .project_service import ProjectService

logger = logging.getLogger("uvicorn.error")


@dataclass
class ManifestEntry:
    relative_path: str
    source_path: Path | None = None
    inline_content: bytes | None = None
    mime_type: str = "application/octet-stream"


class ExportService:
    VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".webm"}
    BAKED_SUBTITLE_RE = re.compile(r"^subtitle_(\d+)\.mogrt$", re.IGNORECASE)
    REQUIRED_IMPORT_ASSETS = (
        "TikTok60fps.sqpreset",
        "White border 10px.mogrt",
        "SPM Anime Background.prfpset",
        "SPM Anime Foreground.prfpset",
        "SPM Anime Category Title.prfpset",
    )
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
        return f"SPM_{anime}_{pid}"

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
        source_items: list[str],
        subtitle_filename: str,
    ) -> str:
        source_list = "\n".join(f"  - {name}" for name in source_items) or "  - (none)"
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
sources/                - Source episodes + overlays + optional music
subtitles/              - Baked subtitle MOGRT files

=== SOURCES ===
{source_list}
"""

    @classmethod
    def _validate_expected_filename(cls, path: Path, expected_name: str) -> None:
        if path.name != expected_name:
            raise ValueError(
                f"Asset filename mismatch: expected '{expected_name}', got '{path.name}'"
            )
        expected_suffix = Path(expected_name).suffix.lower()
        if expected_suffix and path.suffix.lower() != expected_suffix:
            raise ValueError(
                f"Asset extension mismatch for '{expected_name}': got '{path.suffix}'"
            )

    @classmethod
    def _collect_episode_sources(cls, matches: list[SceneMatch]) -> list[Path]:
        seen: set[str] = set()
        sources: list[Path] = []
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
            sources.append(resolved)
        return sources

    @classmethod
    def _resolve_selected_music_path(cls, project: Project) -> Path | None:
        if not project.music_key:
            return None
        try:
            music = MusicConfigService.get_music(project.music_key)
        except ValueError:
            return None
        music_path = Path(music.file_path)
        if not music_path.exists():
            return None
        return music_path

    @classmethod
    def _collect_baked_subtitle_files(cls, output_dir: Path) -> list[Path]:
        subtitles_dir = output_dir / "subtitles"
        if not subtitles_dir.exists():
            return []

        sortable: list[tuple[int, Path]] = []
        for path in subtitles_dir.iterdir():
            if not path.is_file():
                continue
            m = cls.BAKED_SUBTITLE_RE.match(path.name)
            if not m:
                continue
            sortable.append((int(m.group(1)), path))
        sortable.sort(key=lambda item: (item[0], item[1].name.lower()))
        return [path for _, path in sortable]

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

        baked_subtitles = cls._collect_baked_subtitle_files(output_dir)
        if not baked_subtitles:
            raise FileNotFoundError("Missing baked subtitle MOGRT files in output/subtitles.")

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
        for asset_name in cls.REQUIRED_IMPORT_ASSETS:
            asset = assets_dir / asset_name
            if not asset.exists():
                raise FileNotFoundError(f"Missing required asset file: {asset_name}")
            cls._validate_expected_filename(asset, asset_name)
            entries.append(
                ManifestEntry(relative_path=f"{folder}/assets/{asset_name}", source_path=asset)
            )

        launcher = assets_dir / "run_in_premiere.bat"
        if launcher.exists():
            entries.append(ManifestEntry(relative_path=f"{folder}/run_in_premiere.bat", source_path=launcher))

        source_items: list[str] = []
        source_name_to_path: dict[str, Path] = {}

        def _add_source_file(path: Path) -> None:
            name = path.name
            existing = source_name_to_path.get(name)
            if existing is not None:
                if existing.resolve() != path.resolve():
                    raise ValueError(f"Conflicting source filename in bundle: {name}")
                return
            source_name_to_path[name] = path
            source_items.append(name)
            entries.append(ManifestEntry(relative_path=f"{folder}/sources/{name}", source_path=path))

        for source_path in cls._collect_episode_sources(matches):
            _add_source_file(source_path)

        for overlay_name in ("title_overlay.png", "category_overlay.png"):
            overlay_path = output_dir / overlay_name
            if overlay_path.exists():
                _add_source_file(overlay_path)

        music_path = cls._resolve_selected_music_path(project)
        if music_path is not None:
            _add_source_file(music_path)

        for subtitle_mogrt in baked_subtitles:
            entries.append(
                ManifestEntry(
                    relative_path=f"{folder}/subtitles/{subtitle_mogrt.name}",
                    source_path=subtitle_mogrt,
                )
            )

        entries.append(
            ManifestEntry(
                relative_path=f"{folder}/README.txt",
                inline_content=cls._build_readme(
                    project=project,
                    source_items=sorted(source_items),
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

        started_at = time.perf_counter()
        drive = GoogleDriveService.client()
        folder_name = cls.output_folder_name(project)
        _, entries = cls.build_manifest(project, matches)
        folder_id, folder_url = GoogleDriveService.ensure_project_folder(
            folder_name,
            existing_folder_id=project.drive_folder_id,
            drive=drive,
        )
        total_bytes = sum(
            (entry.source_path.stat().st_size if entry.source_path is not None else len(entry.inline_content or b""))
            for entry in entries
        )
        upload_workers = max(1, min(settings.drive_upload_max_parallel, len(entries))) if entries else 1
        logger.info(
            "Drive manifest upload starting: project_id=%s folder_id=%s files=%d total_bytes=%d upload_workers=%d delete_workers=%d",
            project.id,
            folder_id,
            len(entries),
            total_bytes,
            upload_workers,
            settings.drive_delete_max_parallel,
        )

        # Keep drive folder architecture exactly in sync with the export manifest.
        clear_started_at = time.perf_counter()
        GoogleDriveService.clear_folder(folder_id, drive=drive)
        clear_duration = time.perf_counter() - clear_started_at

        # Cache parent folder IDs by relative path to avoid repeated Drive queries.
        parent_cache: dict[tuple[str, ...], str] = {tuple(): folder_id}
        upload_jobs: list[tuple[str, str, ManifestEntry]] = []

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
            # Strip the leading folder-name prefix (first component) since the Drive
            # root folder already represents that level — no nested subfolder needed.
            parts = parts[1:]
            filename = parts[-1]
            parent = folder_id
            if len(parts) > 1:
                # Preserve sub-directory architecture inside the Drive root folder.
                parent = _resolve_parent(parts[:-1])
            upload_jobs.append((parent, filename, entry))

        chunk_bytes = settings.drive_upload_chunk_mb * 1024 * 1024

        def _upload_job(job: tuple[str, str, ManifestEntry]) -> None:
            parent, filename, entry = job
            if entry.source_path is not None:
                uploaded = GoogleDriveService.upload_local_file(
                    parent_id=parent,
                    filename=filename,
                    local_path=entry.source_path,
                    chunksize=chunk_bytes,
                )
            else:
                uploaded = GoogleDriveService.upload_bytes(
                    parent_id=parent,
                    filename=filename,
                    content=entry.inline_content or b"",
                    mime_type=entry.mime_type,
                )
            uploaded_name = str(uploaded.get("name") or "")
            if uploaded_name != filename:
                raise RuntimeError(
                    f"Drive upload renamed file unexpectedly: expected '{filename}', got '{uploaded_name}'"
                )

        upload_started_at = time.perf_counter()
        max_workers = max(1, min(settings.drive_upload_max_parallel, len(upload_jobs))) if upload_jobs else 1
        if upload_jobs:
            failure: RuntimeError | None = None
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_job = {
                    executor.submit(_upload_job, job): job
                    for job in upload_jobs
                }
                for future in as_completed(future_to_job):
                    job = future_to_job[future]
                    _, _, entry = job
                    try:
                        future.result()
                    except Exception as exc:
                        failure = RuntimeError(f"Drive upload failed for '{entry.relative_path}': {exc}")
                        for other in future_to_job:
                            if other is not future:
                                other.cancel()
                        break
            if failure is not None:
                raise failure
        upload_duration = time.perf_counter() - upload_started_at
        total_duration = time.perf_counter() - started_at
        uploaded_files = len(upload_jobs)
        files_per_second = uploaded_files / upload_duration if upload_duration > 0 else 0.0
        mb_per_second = (total_bytes / (1024 * 1024)) / upload_duration if upload_duration > 0 else 0.0
        logger.info(
            "Drive manifest upload completed: project_id=%s folder_id=%s files=%d total_bytes=%d clear_seconds=%.2f upload_seconds=%.2f total_seconds=%.2f files_per_second=%.2f mb_per_second=%.2f",
            project.id,
            folder_id,
            uploaded_files,
            total_bytes,
            clear_duration,
            upload_duration,
            total_duration,
            files_per_second,
            mb_per_second,
        )

        return {
            "folder_id": folder_id,
            "folder_url": folder_url,
            "file_count": len(entries),
        }

    @classmethod
    def detect_upload_video_in_drive_root(cls, folder_id: str) -> list[dict[str, Any]]:
        return GoogleDriveService.list_root_video_files(folder_id, cls.VIDEO_EXTENSIONS)
