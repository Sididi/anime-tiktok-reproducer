from __future__ import annotations

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
import logging
import re
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any, Callable

from ..config import settings
from ..models import Project, SceneMatch
from .gap_resolution import GapResolutionService
from .google_drive_service import GoogleDriveService
from .music_config_service import MusicConfigService
from .project_service import ProjectService

logger = logging.getLogger("uvicorn.error")
DriveUploadProgressCallback = Callable[[dict[str, Any]], None]


@dataclass
class ManifestEntry:
    relative_path: str
    source_path: Path | None = None
    inline_content: bytes | None = None
    mime_type: str = "application/octet-stream"


@dataclass(frozen=True)
class UploadJob:
    parent_id: str
    filename: str
    entry: ManifestEntry
    size_bytes: int


class _DriveUploadProgressTracker:
    _UPLOAD_PROGRESS_EMIT_INTERVAL_SECONDS = 0.25
    _UPLOAD_PROGRESS_EMIT_MIN_DELTA_BYTES = 4 * 1024 * 1024

    def __init__(
        self,
        *,
        callback: DriveUploadProgressCallback | None,
        file_count: int,
        total_bytes: int,
    ) -> None:
        self._callback = callback
        self._lock = Lock()
        self._started_at = time.perf_counter()
        self._upload_started_at: float | None = None
        self._last_upload_emit_at = 0.0
        self._last_upload_emit_bytes = 0
        self.file_count = file_count
        self.files_completed = 0
        self.total_bytes = total_bytes
        self.uploaded_bytes = 0
        self.current_file: str | None = None
        self.clear_item_count = 0
        self.clear_items_completed = 0
        self._bytes_by_file: dict[str, int] = {}
        self._completed_files: set[str] = set()

    def emit_manifest(self) -> None:
        with self._lock:
            self._emit_locked(
                phase="manifest",
                message=(
                    f"Preparing Drive manifest ({self.file_count} files, "
                    f"{self._format_bytes(self.total_bytes)})"
                ),
                force=True,
            )

    def start_clear(self, item_count: int) -> None:
        with self._lock:
            self.clear_item_count = item_count
            self.clear_items_completed = 0
            message = (
                "Drive folder is already empty."
                if item_count == 0
                else f"Clearing existing Drive folder (0/{item_count})"
            )
            self._emit_locked(phase="clear", message=message, force=True)

    def update_clear(self, completed: int, *, current_item: str | None = None) -> None:
        with self._lock:
            self.clear_items_completed = completed
            self.current_file = current_item
            total = self.clear_item_count
            if total == 0:
                message = "Drive folder is already empty."
            else:
                message = f"Clearing existing Drive folder ({completed}/{total})"
            self._emit_locked(phase="clear", message=message, force=True)

    def start_upload(self) -> None:
        with self._lock:
            if self._upload_started_at is None:
                self._upload_started_at = time.perf_counter()
            self.current_file = None
            self._emit_locked(
                phase="upload",
                message=self._upload_message_locked(),
                force=True,
            )

    def update_upload(
        self,
        relative_path: str,
        uploaded_bytes_for_file: int,
        *,
        completed: bool,
    ) -> None:
        with self._lock:
            if self._upload_started_at is None:
                self._upload_started_at = time.perf_counter()
            previous = self._bytes_by_file.get(relative_path, 0)
            current = max(previous, uploaded_bytes_for_file)
            self._bytes_by_file[relative_path] = current
            self.uploaded_bytes += current - previous
            self.current_file = relative_path
            if completed and relative_path not in self._completed_files:
                self._completed_files.add(relative_path)
                self.files_completed += 1
            force = completed
            if not force and not self._should_emit_upload_locked():
                return
            self._emit_locked(
                phase="upload",
                message=self._upload_message_locked(),
                force=force,
            )

    def emit_persist(self) -> None:
        with self._lock:
            self.current_file = None
            self._emit_locked(
                phase="persist",
                message="Finishing upload metadata",
                force=True,
            )

    def _should_emit_upload_locked(self) -> bool:
        now = time.perf_counter()
        if now - self._last_upload_emit_at >= self._UPLOAD_PROGRESS_EMIT_INTERVAL_SECONDS:
            return True
        return self.uploaded_bytes - self._last_upload_emit_bytes >= self._UPLOAD_PROGRESS_EMIT_MIN_DELTA_BYTES

    def _emit_locked(self, *, phase: str, message: str, force: bool) -> None:
        if self._callback is None:
            return
        elapsed_ms = int((time.perf_counter() - self._started_at) * 1000)
        throughput_mb_per_sec = 0.0
        if self._upload_started_at is not None:
            upload_elapsed = max(time.perf_counter() - self._upload_started_at, 0.001)
            throughput_mb_per_sec = (self.uploaded_bytes / (1024 * 1024)) / upload_elapsed
        payload = {
            "phase": phase,
            "message": message,
            "file_count": self.file_count,
            "files_completed": self.files_completed,
            "total_bytes": self.total_bytes,
            "uploaded_bytes": self.uploaded_bytes,
            "current_file": self.current_file,
            "clear_item_count": self.clear_item_count,
            "clear_items_completed": self.clear_items_completed,
            "elapsed_ms": elapsed_ms,
            "throughput_mb_per_sec": round(throughput_mb_per_sec, 3),
        }
        self._callback(payload)
        if phase == "upload":
            self._last_upload_emit_at = time.perf_counter()
            self._last_upload_emit_bytes = self.uploaded_bytes

    def _upload_message_locked(self) -> str:
        return (
            f"Uploading {self.files_completed}/{self.file_count} files "
            f"({self._format_bytes(self.uploaded_bytes)} / {self._format_bytes(self.total_bytes)})"
        )

    @staticmethod
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


class ExportService:
    VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".webm"}
    BAKED_SUBTITLE_RE = re.compile(r"^subtitle_(\d+)\.mogrt$", re.IGNORECASE)

    @classmethod
    def get_required_import_assets(cls) -> tuple[str, ...]:
        """Return the tuple of asset filenames to bundle in the export ZIP.

        The border mogrt varies depending on ``grand_mode_enabled``:
        - grand_mode=True  → White border 10px.mogrt
        - grand_mode=False → White border 5px.mogrt
        """
        border_mogrt = (
            "White border 10px.mogrt" if settings.grand_mode_enabled else "White border 5px.mogrt"
        )
        return (
            "TikTok60fps.sqpreset",
            border_mogrt,
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

        baked_subtitles = cls._collect_baked_subtitle_files(output_dir)
        subtitle_timing_files = cls._collect_subtitle_timing_files(output_dir)
        raw_scene_subtitle_files = cls._collect_raw_scene_subtitle_files(output_dir)

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
        for asset_name in cls.get_required_import_assets():
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
        for subtitle_timing_file in subtitle_timing_files:
            entries.append(
                ManifestEntry(
                    relative_path=f"{folder}/subtitles/{subtitle_timing_file.name}",
                    source_path=subtitle_timing_file,
                )
            )

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
    def upload_manifest_to_drive(
        cls,
        project: Project,
        matches: list[SceneMatch],
        *,
        progress_callback: DriveUploadProgressCallback | None = None,
    ) -> dict[str, Any]:
        if not GoogleDriveService.is_configured():
            raise RuntimeError("Google Drive integration is not configured")

        started_at = time.perf_counter()
        drive = GoogleDriveService.client()
        folder_name = cls.output_folder_name(project)
        _, entries = cls.build_manifest(project, matches)
        diagnostics = cls._build_manifest_diagnostics(entries)
        progress = _DriveUploadProgressTracker(
            callback=progress_callback,
            file_count=len(entries),
            total_bytes=diagnostics["total_bytes"],
        )
        progress.emit_manifest()
        folder_id, folder_url = GoogleDriveService.ensure_project_folder(
            folder_name,
            existing_folder_id=project.drive_folder_id,
            drive=drive,
        )
        total_bytes = diagnostics["total_bytes"]
        upload_workers = max(1, min(settings.drive_upload_max_parallel, len(entries))) if entries else 1
        logger.info(
            "Drive manifest upload starting: project_id=%s folder_id=%s files=%d total_bytes=%d upload_workers=%d delete_workers=%d bytes_by_root=%s largest_files=%s",
            project.id,
            folder_id,
            len(entries),
            total_bytes,
            upload_workers,
            settings.drive_delete_max_parallel,
            diagnostics["bytes_by_root"],
            diagnostics["largest_files"],
        )

        # Keep drive folder architecture exactly in sync with the export manifest.
        clear_started_at = time.perf_counter()
        clear_progress_started = False

        def _handle_clear_progress(payload: dict[str, Any]) -> None:
            nonlocal clear_progress_started
            item_count = int(payload.get("item_count") or 0)
            if not clear_progress_started:
                progress.start_clear(item_count)
                clear_progress_started = True
            completed = int(payload.get("items_completed") or 0)
            current_item = str(payload.get("current_item") or "") or None
            if completed == 0 and current_item is None:
                return
            progress.update_clear(
                completed,
                current_item=current_item,
            )

        cleared_items = GoogleDriveService.clear_folder(
            folder_id,
            drive=drive,
            progress_callback=_handle_clear_progress,
        )
        if not clear_progress_started:
            progress.start_clear(cleared_items)
        clear_duration = time.perf_counter() - clear_started_at

        # Cache parent folder IDs by relative path to avoid repeated Drive queries.
        parent_cache: dict[tuple[str, ...], str] = {tuple(): folder_id}
        upload_jobs: list[UploadJob] = []

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
            upload_jobs.append(
                UploadJob(
                    parent_id=parent,
                    filename=filename,
                    entry=entry,
                    size_bytes=cls._entry_size_bytes(entry),
                )
            )

        upload_jobs.sort(key=lambda job: (-job.size_bytes, job.entry.relative_path))

        chunk_bytes = settings.drive_upload_chunk_mb * 1024 * 1024

        progress.start_upload()

        def _upload_job(job: UploadJob) -> None:
            entry = job.entry

            def _handle_upload_progress(file_progress: dict[str, Any]) -> None:
                progress.update_upload(
                    entry.relative_path,
                    int(file_progress.get("uploaded_bytes") or 0),
                    completed=bool(file_progress.get("completed")),
                )

            if entry.source_path is not None:
                uploaded = GoogleDriveService.upload_local_file(
                    parent_id=job.parent_id,
                    filename=job.filename,
                    local_path=entry.source_path,
                    chunksize=chunk_bytes,
                    progress_callback=_handle_upload_progress,
                )
            else:
                uploaded = GoogleDriveService.upload_bytes(
                    parent_id=job.parent_id,
                    filename=job.filename,
                    content=entry.inline_content or b"",
                    mime_type=entry.mime_type,
                    progress_callback=_handle_upload_progress,
                )
            uploaded_name = str(uploaded.get("name") or "")
            if uploaded_name != job.filename:
                raise RuntimeError(
                    f"Drive upload renamed file unexpectedly: expected '{job.filename}', got '{uploaded_name}'"
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
                    try:
                        future.result()
                    except Exception as exc:
                        failure = RuntimeError(
                            f"Drive upload failed for '{job.entry.relative_path}': {exc}"
                        )
                        for other in future_to_job:
                            if other is not future:
                                other.cancel()
                        break
            if failure is not None:
                raise failure
        progress.emit_persist()
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
            "total_bytes": total_bytes,
        }

    @classmethod
    def detect_upload_video_in_drive_root(cls, folder_id: str) -> list[dict[str, Any]]:
        return GoogleDriveService.list_root_video_files(folder_id, cls.VIDEO_EXTENSIONS)
