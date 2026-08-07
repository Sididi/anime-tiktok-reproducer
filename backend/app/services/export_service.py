from __future__ import annotations

import asyncio
from collections import defaultdict
import logging
import os
import re
import shutil
import tempfile
import time
import zipfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, Callable

from ..config import settings
from ..models import Project, SceneMatch
from .anime_library import AnimeLibraryService
from .google_drive_rclone import GoogleDriveRclone
from .google_drive_service import GoogleDriveService
from .music_config_service import MusicConfigService
from .project_service import ProjectService
from .rclone_runner import RcloneStats

logger = logging.getLogger("uvicorn.error")
DriveUploadProgressCallback = Callable[[dict[str, Any]], None]


@dataclass
class ManifestEntry:
    relative_path: str
    source_path: Path | None = None
    inline_content: bytes | None = None
    mime_type: str = "application/octet-stream"


def _format_bytes(value: int) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    size = float(max(0, value))
    unit = units[0]
    for unit in units:
        if size < 1024.0 or unit == units[-1]:
            break
        size /= 1024.0
    decimals = 0 if unit == "B" else 1
    return f"{size:.{decimals}f} {unit}"


class _RcloneDriveProgressAdapter:
    """Maps rclone sync stats onto the existing Drive-upload SSE payload.

    Keeps the exact field set the frontend consumes (phase + numeric fields);
    the delta sync never emits "clear" frames — stale files are deleted by
    rclone itself.
    """

    def __init__(
        self,
        *,
        callback: DriveUploadProgressCallback | None,
        file_count: int,
        total_bytes: int,
    ) -> None:
        self._callback = callback
        self._started_at = time.perf_counter()
        self.manifest_file_count = file_count
        self.manifest_total_bytes = total_bytes
        self.last_stats: RcloneStats | None = None

    def emit_manifest(self) -> None:
        self._emit(
            phase="manifest",
            message=(
                f"Preparing Drive manifest ({self.manifest_file_count} files, "
                f"{_format_bytes(self.manifest_total_bytes)})"
            ),
            file_count=self.manifest_file_count,
            files_completed=0,
            total_bytes=self.manifest_total_bytes,
            uploaded_bytes=0,
            current_file=None,
            throughput_mb_per_sec=0.0,
        )

    def on_stats(self, stats: RcloneStats) -> None:
        self.last_stats = stats
        current_file = stats.transferring_names[0] if stats.transferring_names else None
        if stats.bytes_total > 0:
            message = (
                f"Uploading {stats.transfers}/{stats.total_transfers} files "
                f"({_format_bytes(stats.bytes_transferred)} / "
                f"{_format_bytes(stats.bytes_total)})"
            )
        elif stats.total_checks > 0:
            message = f"Comparing files ({stats.checks}/{stats.total_checks})"
        else:
            message = "Uploading project to Google Drive..."
        self._emit(
            phase="upload",
            message=message,
            file_count=stats.total_transfers,
            files_completed=stats.transfers,
            # rclone's post-scan totals = bytes actually being transferred
            # under the delta sync; unchanged files never inflate the bar.
            total_bytes=stats.bytes_total,
            uploaded_bytes=stats.bytes_transferred,
            current_file=current_file,
            throughput_mb_per_sec=round(
                stats.speed_bytes_per_sec / (1024 * 1024), 3
            ),
        )

    def emit_persist(self) -> None:
        self._emit(
            phase="persist",
            message="Finishing upload metadata",
            file_count=self.manifest_file_count,
            files_completed=(
                self.last_stats.transfers if self.last_stats else 0
            ),
            total_bytes=(
                self.last_stats.bytes_total if self.last_stats else 0
            ),
            uploaded_bytes=(
                self.last_stats.bytes_transferred if self.last_stats else 0
            ),
            current_file=None,
            throughput_mb_per_sec=0.0,
        )

    def _emit(
        self,
        *,
        phase: str,
        message: str,
        file_count: int,
        files_completed: int,
        total_bytes: int,
        uploaded_bytes: int,
        current_file: str | None,
        throughput_mb_per_sec: float,
    ) -> None:
        if self._callback is None:
            return
        self._callback(
            {
                "phase": phase,
                "message": message,
                "file_count": file_count,
                "files_completed": files_completed,
                "total_bytes": total_bytes,
                "uploaded_bytes": uploaded_bytes,
                "current_file": current_file,
                "clear_item_count": None,
                "clear_items_completed": None,
                "elapsed_ms": int((time.perf_counter() - self._started_at) * 1000),
                "throughput_mb_per_sec": throughput_mb_per_sec,
            }
        )


class ExportService:
    IGNORED_UPLOAD_VIDEO_FILENAMES = {"output_instagram.mp4"}

    @classmethod
    def filter_upload_video_candidates(cls, files: list[dict[str, Any]]) -> list[dict[str, Any]]:
        ignored = {name.casefold() for name in cls.IGNORED_UPLOAD_VIDEO_FILENAMES}
        return [
            file_data
            for file_data in files
            if str(file_data.get("name") or "").casefold() not in ignored
        ]

    VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".webm"}
    BAKED_SUBTITLE_RE = re.compile(r"^subtitle_(\d+)\.mogrt$", re.IGNORECASE)
    SUBTITLES_ARCHIVE_FILENAME = "atr_subtitles.zip"

    @classmethod
    def get_required_import_assets(cls, project: Project) -> tuple[str, ...]:
        """Return the tuple of asset filenames to bundle in the export ZIP.

        Asset names come from the project's resolved template:
        - foreground/background prfpset
        - white border mogrt (omitted when white_border.enabled is false)
        - overlay prfpsets (each side may be null)
        """
        from .template_service import TemplateService

        template = TemplateService.get(project.resolved_template_key())

        assets: list[str] = [
            "TikTok60fps.sqpreset",
            "ATR Proxy H264.epr",
            template.background.prfpset,
            template.foreground.prfpset,
        ]
        if template.white_border.enabled and template.white_border.mogrt:
            assets.append(template.white_border.mogrt)
        if template.overlay.enabled:
            if template.overlay.title.prfpset:
                assets.append(template.overlay.title.prfpset)
            if template.overlay.category.prfpset:
                assets.append(template.overlay.category.prfpset)
        return tuple(dict.fromkeys(assets))  # de-dupe while preserving order
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
subtitles/              - CEP subtitle archive (extracts baked MOGRT files locally)

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
    def _collect_episode_sources(
        cls,
        project: Project,
        matches: list[SceneMatch],
    ) -> list[Path]:
        def _resolve_export_source_path(episode_ref: str) -> Path | None:
            episode_ref = str(episode_ref or "").strip()
            if not episode_ref:
                return None

            candidate = Path(episode_ref)
            if candidate.is_absolute() and candidate.exists():
                return candidate

            resolved = AnimeLibraryService.resolve_episode_path(
                episode_ref,
                library_type=project.library_type,
            )
            if resolved is not None and resolved.exists():
                return resolved

            if candidate.exists():
                return candidate
            return None

        seen: set[str] = set()
        sources: list[Path] = []
        unresolved_refs: list[str] = []
        missing_refs: list[str] = []
        for match in matches:
            episode_ref = str(match.episode or "").strip()
            if not episode_ref:
                continue
            resolved = _resolve_export_source_path(episode_ref)
            if resolved is None:
                unresolved_refs.append(episode_ref)
                continue
            if not resolved.exists() or not resolved.is_file():
                missing_refs.append(str(resolved))
                continue
            key = str(resolved)
            if key in seen:
                continue
            seen.add(key)
            sources.append(resolved)

        if unresolved_refs or missing_refs:
            details: list[str] = []
            if unresolved_refs:
                unique_unresolved_refs = list(dict.fromkeys(unresolved_refs))
                invalid_preview = ", ".join(unique_unresolved_refs[:3])
                if len(unique_unresolved_refs) > 3:
                    invalid_preview += ", ..."
                details.append(
                    "Matched episode refs could not be resolved to library sources: "
                    f"{invalid_preview}"
                )
            if missing_refs:
                unique_missing_refs = list(dict.fromkeys(missing_refs))
                missing_preview = ", ".join(unique_missing_refs[:3])
                if len(unique_missing_refs) > 3:
                    missing_preview += ", ..."
                details.append(
                    f"Resolved source episode files are missing: {missing_preview}"
                )
            details.append(
                "Ensure the source episode exists in the hydrated library, then rerun /processing."
            )
            raise RuntimeError(" ".join(details))
        return sources

    @classmethod
    def _resolve_selected_music_path(cls, project: Project) -> Path | None:
        music_key = project.resolved_music_key()
        if not music_key:
            return None
        try:
            music = MusicConfigService.get_music(music_key)
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
    def _collect_subtitle_timing_files(cls, output_dir: Path) -> list[Path]:
        subtitles_dir = output_dir / "subtitles"
        if not subtitles_dir.exists():
            return []
        return sorted(
            [
                path
                for path in subtitles_dir.iterdir()
                if path.is_file() and path.suffix.lower() == ".srt"
            ],
            key=lambda path: path.name.lower(),
        )

    @classmethod
    def _build_subtitles_archive_entry(
        cls,
        output_dir: Path,
        *,
        relative_path: str,
    ) -> ManifestEntry | None:
        baked_subtitles = cls._collect_baked_subtitle_files(output_dir)
        subtitle_timing_files = cls._collect_subtitle_timing_files(output_dir)
        archive_sources = baked_subtitles + subtitle_timing_files
        if not archive_sources:
            return None

        buffer = BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for source_path in archive_sources:
                archive.write(source_path, source_path.name)

        return ManifestEntry(
            relative_path=relative_path,
            inline_content=buffer.getvalue(),
            mime_type="application/zip",
        )

    @classmethod
    def _collect_raw_scene_subtitle_files(cls, output_dir: Path) -> list[Path]:
        raw_dir = output_dir / "raw_scene_subtitles"
        if not raw_dir.exists():
            return []
        return sorted(
            [path for path in raw_dir.rglob("*") if path.is_file()],
            key=lambda path: str(path.relative_to(raw_dir)).lower(),
        )

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
        subtitles_archive_entry = cls._build_subtitles_archive_entry(
            output_dir,
            relative_path=f"{folder}/subtitles/{cls.SUBTITLES_ARCHIVE_FILENAME}",
        )
        raw_scene_subtitle_files = cls._collect_raw_scene_subtitle_files(output_dir)

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
        for asset_name in cls.get_required_import_assets(project):
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

        for source_path in cls._collect_episode_sources(project, matches):
            _add_source_file(source_path)

        for overlay_name in ("title_overlay.png", "category_overlay.png"):
            overlay_path = output_dir / overlay_name
            if overlay_path.exists():
                _add_source_file(overlay_path)

        music_path = cls._resolve_selected_music_path(project)
        if music_path is not None:
            _add_source_file(music_path)

        if subtitles_archive_entry is not None:
            entries.append(subtitles_archive_entry)

        raw_scene_subtitle_root = output_dir / "raw_scene_subtitles"
        for raw_scene_subtitle_file in raw_scene_subtitle_files:
            relative = raw_scene_subtitle_file.relative_to(raw_scene_subtitle_root).as_posix()
            entries.append(
                ManifestEntry(
                    relative_path=f"{folder}/raw_scene_subtitles/{relative}",
                    source_path=raw_scene_subtitle_file,
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
    def _entry_size_bytes(cls, entry: ManifestEntry) -> int:
        return (
            entry.source_path.stat().st_size
            if entry.source_path is not None
            else len(entry.inline_content or b"")
        )

    @classmethod
    def _build_manifest_diagnostics(cls, entries: list[ManifestEntry]) -> dict[str, Any]:
        bytes_by_root: dict[str, int] = defaultdict(int)
        largest_files: list[tuple[int, str]] = []
        total_bytes = 0
        for entry in entries:
            size_bytes = cls._entry_size_bytes(entry)
            total_bytes += size_bytes
            rel_parts = Path(entry.relative_path).parts
            payload_parts = rel_parts[1:] if len(rel_parts) > 1 else rel_parts
            top_level = payload_parts[0] if payload_parts else entry.relative_path
            bytes_by_root[top_level] += size_bytes
            largest_files.append((size_bytes, entry.relative_path))
        largest_files.sort(key=lambda item: (-item[0], item[1]))
        return {
            "total_bytes": total_bytes,
            "bytes_by_root": dict(sorted(bytes_by_root.items(), key=lambda item: (-item[1], item[0]))),
            "largest_files": [
                {"relative_path": relative_path, "bytes": size_bytes}
                for size_bytes, relative_path in largest_files[:5]
            ],
        }

    @classmethod
    def _stage_manifest_tree(
        cls, entries: list[ManifestEntry], stage_dir: Path
    ) -> None:
        """Materialize the manifest as a local tree (folder-name level stripped).

        File entries become symlinks (rclone reads through with --copy-links);
        inline entries become real files.
        """
        for entry in entries:
            # Strip the leading folder-name prefix (first component) since the
            # Drive root folder already represents that level.
            parts = list(Path(entry.relative_path).parts)[1:]
            if not parts:
                raise RuntimeError(
                    "Manifest entry has no path inside the export folder: "
                    f"{entry.relative_path}"
                )
            target = stage_dir.joinpath(*parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            if entry.source_path is not None:
                os.symlink(Path(entry.source_path).resolve(), target)
            else:
                target.write_bytes(entry.inline_content or b"")

    @classmethod
    async def upload_manifest_to_drive(
        cls,
        project: Project,
        matches: list[SceneMatch],
        *,
        progress_callback: DriveUploadProgressCallback | None = None,
    ) -> dict[str, Any]:
        """Delta-sync the export bundle into the project's Drive folder.

        One ``rclone sync --checksum`` against a staged tree: unchanged files
        (matched by Drive's server-side MD5s) are skipped, stale remote files
        are deleted. The folder itself stays on googleapiclient, which owns
        the folder id + webViewLink contract.
        """
        if not GoogleDriveService.is_configured():
            raise RuntimeError("Google Drive integration is not configured")

        started_at = time.perf_counter()
        folder_name = cls.output_folder_name(project)
        _, entries = await asyncio.to_thread(cls.build_manifest, project, matches)
        diagnostics = cls._build_manifest_diagnostics(entries)
        total_bytes = diagnostics["total_bytes"]
        adapter = _RcloneDriveProgressAdapter(
            callback=progress_callback,
            file_count=len(entries),
            total_bytes=total_bytes,
        )
        adapter.emit_manifest()
        folder_id, folder_url = await asyncio.to_thread(
            GoogleDriveService.ensure_project_folder,
            folder_name,
            project.drive_folder_id,
        )
        logger.info(
            "Drive manifest sync starting: project_id=%s folder_id=%s files=%d "
            "total_bytes=%d transfers=%d bytes_by_root=%s largest_files=%s",
            project.id,
            folder_id,
            len(entries),
            total_bytes,
            settings.drive_rclone_transfers,
            diagnostics["bytes_by_root"],
            diagnostics["largest_files"],
        )

        stage_dir = Path(
            tempfile.mkdtemp(prefix="atr-drive-export-", dir=str(settings.cache_dir))
        )
        sync_started_at = time.perf_counter()
        try:
            await asyncio.to_thread(cls._stage_manifest_tree, entries, stage_dir)
            await GoogleDriveRclone.sync_tree(
                stage_dir,
                folder_id=folder_id,
                stats_callback=adapter.on_stats,
            )
        finally:
            await asyncio.to_thread(shutil.rmtree, stage_dir, True)
        adapter.emit_persist()

        sync_duration = time.perf_counter() - sync_started_at
        total_duration = time.perf_counter() - started_at
        transferred_bytes = (
            adapter.last_stats.bytes_transferred if adapter.last_stats else 0
        )
        transferred_files = adapter.last_stats.transfers if adapter.last_stats else 0
        mb_per_second = (
            (transferred_bytes / (1024 * 1024)) / sync_duration
            if sync_duration > 0
            else 0.0
        )
        logger.info(
            "Drive manifest sync completed: project_id=%s folder_id=%s files=%d "
            "transferred_files=%d transferred_bytes=%d sync_seconds=%.2f "
            "total_seconds=%.2f mb_per_second=%.2f",
            project.id,
            folder_id,
            len(entries),
            transferred_files,
            transferred_bytes,
            sync_duration,
            total_duration,
            mb_per_second,
        )

        return {
            "folder_id": folder_id,
            "folder_url": folder_url,
            "file_count": len(entries),
            "total_bytes": total_bytes,
        }

    @classmethod
    def detect_upload_video_in_drive_root(cls, folder_id: str) -> list[dict[str, Any]]:
        return cls.filter_upload_video_candidates(
            GoogleDriveService.list_root_video_files(folder_id, cls.VIDEO_EXTENSIONS)
        )
