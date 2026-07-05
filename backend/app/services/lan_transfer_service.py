# backend/app/services/lan_transfer_service.py
"""LAN transfer: manifest building, output receiving, Drive relay.

The manifest reuses ExportService.build_manifest so the LAN tree is exactly
the tree uploaded to Drive; files are served by manifest lookup (never by
filesystem path join), which removes path-traversal risk by construction.
"""
from __future__ import annotations

import logging
from typing import Any

from .export_service import ExportService, ManifestEntry
from .project_service import ProjectService

logger = logging.getLogger(__name__)


class LanTransferService:
    API_VERSION = 1

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
