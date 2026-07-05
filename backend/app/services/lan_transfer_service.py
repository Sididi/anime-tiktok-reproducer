# backend/app/services/lan_transfer_service.py
"""LAN transfer: manifest building, output receiving, Drive relay.

The manifest reuses ExportService.build_manifest so the LAN tree is exactly
the tree uploaded to Drive; files are served by manifest lookup (never by
filesystem path join), which removes path-traversal risk by construction.
"""
from __future__ import annotations

import asyncio
import logging
import re
import uuid
from pathlib import Path
from typing import Any

from ..config import settings
from .export_service import ExportService, ManifestEntry
from .project_service import ProjectService

logger = logging.getLogger(__name__)


class LanTransferService:
    API_VERSION = 1
    TMP_SUFFIX = ".lan_tmp"
    _ALLOWED_OUTPUT_EXACT = {"output.mp4", "output_no_music.wav"}
    _ATR_OUTPUT_RE = re.compile(r"^atr_.*\.mp4$", re.IGNORECASE)
    _PROXY_SUFFIX = "__atr_proxy.mp4"

    @classmethod
    def _build_entries(cls, project) -> tuple[str, list[ManifestEntry]]:
        match_list = ProjectService.load_matches(project.id)
        matches = list(match_list.matches) if match_list else []
        if not matches:
            raise FileNotFoundError("No matches found for project; run processing first")
        return ExportService.build_manifest(project, matches)

    @staticmethod
    def _strip_folder_prefix(relative_path: str) -> str:
        return relative_path.split("/", 1)[1] if "/" in relative_path else relative_path

    @staticmethod
    def _entry_size(entry: ManifestEntry) -> int:
        if entry.source_path is not None:
            return entry.source_path.stat().st_size
        return len(entry.inline_content or b"")

    @classmethod
    def build_manifest_payload(cls, project) -> dict[str, Any]:
        folder_name, entries = cls._build_entries(project)
        return {
            "api_version": cls.API_VERSION,
            "project_id": project.id,
            "folder_name": folder_name,
            "drive_folder_id": project.drive_folder_id,
            "files": [
                {
                    "relative_path": cls._strip_folder_prefix(entry.relative_path),
                    "size": cls._entry_size(entry),
                }
                for entry in entries
            ],
        }

    @classmethod
    def resolve_entry(cls, project, relative_path: str) -> ManifestEntry | None:
        try:
            _, entries = cls._build_entries(project)
        except FileNotFoundError:
            return None
        for entry in entries:
            if cls._strip_folder_prefix(entry.relative_path) == relative_path:
                return entry
        return None

    @classmethod
    def is_allowed_output_filename(cls, name: str) -> bool:
        if not name or "/" in name or "\\" in name or name.startswith("."):
            return False
        lowered = name.casefold()
        if lowered in cls._ALLOWED_OUTPUT_EXACT:
            return True
        if lowered.endswith(cls._PROXY_SUFFIX):
            return False
        return bool(cls._ATR_OUTPUT_RE.match(lowered))

    @classmethod
    async def receive_output_stream(cls, project_id: str, filename: str, stream) -> Path:
        output_dir = ExportService.get_output_dir(project_id)
        output_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = output_dir / f"{filename}.{uuid.uuid4().hex}{cls.TMP_SUFFIX}"
        final_path = output_dir / filename
        try:
            with tmp_path.open("wb") as fh:
                async for chunk in stream:
                    if chunk:
                        await asyncio.to_thread(fh.write, chunk)
            tmp_path.replace(final_path)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise
        return final_path

    @classmethod
    def sweep_stale_tmp_files(cls) -> int:
        removed = 0
        projects_dir = settings.projects_dir
        if not projects_dir.exists():
            return 0
        for tmp_file in projects_dir.glob(f"*/output/*{cls.TMP_SUFFIX}"):
            try:
                tmp_file.unlink()
                removed += 1
            except OSError:
                logger.warning("Could not remove stale LAN temp file: %s", tmp_file)
        if removed:
            logger.info("Swept %d stale LAN temp file(s)", removed)
        return removed
