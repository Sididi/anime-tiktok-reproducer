from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings
from app.models import Project
from app.services.export_service import ExportService, ManifestEntry
from app.services.google_drive_rclone import GoogleDriveRclone
from app.services.google_drive_service import GoogleDriveService


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    monkeypatch.setattr(settings, "cache_dir", cache_dir)
    monkeypatch.setattr(
        GoogleDriveService, "is_configured", classmethod(lambda cls: True)
    )
    yield


def _entries(tmp_path: Path) -> list[ManifestEntry]:
    source = tmp_path / "sources" / "Episode 01.mkv"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"episode-bytes")
    jsx = tmp_path / "import_project.jsx"
    jsx.write_text("// jsx", encoding="utf-8")
    return [
        ManifestEntry(
            relative_path="SPM_folder/import_project.jsx", source_path=jsx
        ),
        ManifestEntry(
            relative_path="SPM_folder/sources/Episode 01.mkv", source_path=source
        ),
        ManifestEntry(
            relative_path="SPM_folder/subtitles/atr_subtitles.zip",
            inline_content=b"zip-bytes",
        ),
    ]


def test_stage_manifest_tree_symlinks_and_inline(tmp_path: Path) -> None:
    stage_dir = tmp_path / "stage"
    stage_dir.mkdir()
    entries = _entries(tmp_path)

    ExportService._stage_manifest_tree(entries, stage_dir)

    jsx = stage_dir / "import_project.jsx"
    episode = stage_dir / "sources" / "Episode 01.mkv"
    inline = stage_dir / "subtitles" / "atr_subtitles.zip"
    assert jsx.is_symlink() and jsx.read_text(encoding="utf-8") == "// jsx"
    assert episode.is_symlink() and episode.read_bytes() == b"episode-bytes"
    assert not inline.is_symlink() and inline.read_bytes() == b"zip-bytes"
    # Folder-name prefix stripped: nothing named SPM_folder inside the stage.
    assert not (stage_dir / "SPM_folder").exists()


@pytest.mark.asyncio
async def test_upload_manifest_syncs_staged_tree_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    entries = _entries(tmp_path)
    project = Project(anime_name="Test Anime")
    synced: list[dict[str, Any]] = []

    monkeypatch.setattr(
        ExportService,
        "build_manifest",
        classmethod(lambda cls, p, m: ("SPM_folder", entries)),
    )
    monkeypatch.setattr(
        GoogleDriveService,
        "ensure_project_folder",
        classmethod(
            lambda cls, name, existing_folder_id=None, drive=None: (
                "folder-1",
                "https://drive.example/folder-1",
            )
        ),
    )

    async def _fake_sync(cls, stage_dir: Path, *, folder_id, stats_callback=None):
        synced.append(
            {
                "stage_dir": stage_dir,
                "folder_id": folder_id,
                "files": sorted(
                    str(p.relative_to(stage_dir))
                    for p in stage_dir.rglob("*")
                    if p.is_file() or p.is_symlink()
                ),
            }
        )

    monkeypatch.setattr(GoogleDriveRclone, "sync_tree", classmethod(_fake_sync))

    payloads: list[dict[str, Any]] = []
    result = await ExportService.upload_manifest_to_drive(
        project, [], progress_callback=payloads.append
    )

    assert result["folder_id"] == "folder-1"
    assert result["folder_url"] == "https://drive.example/folder-1"
    assert result["file_count"] == 3

    assert len(synced) == 1
    assert synced[0]["folder_id"] == "folder-1"
    assert synced[0]["files"] == [
        "import_project.jsx",
        "sources/Episode 01.mkv",
        "subtitles/atr_subtitles.zip",
    ]
    # Stage dir removed after the sync.
    assert not synced[0]["stage_dir"].exists()

    # First frame is the manifest phase; last is persist.
    assert payloads[0]["phase"] == "manifest"
    assert payloads[-1]["phase"] == "persist"


@pytest.mark.asyncio
async def test_upload_manifest_externalizes_shared_sources(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from app.services import drive_shared_sources as dss_module
    from app.services.drive_shared_sources import DriveSharedSources

    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
    monkeypatch.setattr(settings, "projects_dir", projects_dir)
    monkeypatch.setattr(settings, "drive_shared_sources_enabled", True)
    monkeypatch.setattr(settings, "drive_shared_sources_min_bytes", 4)
    monkeypatch.setattr(dss_module, "_shared_folder_id_cache", None)
    monkeypatch.setattr(
        GoogleDriveService,
        "ensure_child_folder_id",
        classmethod(lambda cls, name, parent_id=None, drive=None: "shared-1"),
    )

    entries = _entries(tmp_path)
    project = Project(anime_name="Test Anime")

    monkeypatch.setattr(
        ExportService,
        "build_manifest",
        classmethod(lambda cls, p, m: ("SPM_folder", entries)),
    )
    monkeypatch.setattr(
        GoogleDriveService,
        "ensure_project_folder",
        classmethod(
            lambda cls, name, existing_folder_id=None, drive=None: ("f-1", "url")
        ),
    )

    drive_children: list[dict[str, Any]] = []
    monkeypatch.setattr(
        GoogleDriveService,
        "list_children_with_hashes",
        classmethod(lambda cls, folder_id, drive=None: list(drive_children)),
    )

    copied: list[dict[str, Any]] = []

    async def _fake_copy(cls, stage_dir: Path, *, folder_id, stats_callback=None):
        # GC race guard: the pending manifest must be on disk before any
        # shared upload runs.
        pending = DriveSharedSources.load_local_manifest(project.id)
        assert pending is not None and pending["status"] == "pending"
        for staged in stage_dir.iterdir():
            copied.append({"name": staged.name, "folder_id": folder_id})
            drive_children.append(
                {
                    "id": f"d-{staged.name[:6]}",
                    "name": staged.name,
                    "size": str(staged.stat().st_size),
                    "md5Checksum": "md5-x",
                }
            )

    monkeypatch.setattr(GoogleDriveRclone, "copy_tree", classmethod(_fake_copy))

    synced_files: list[list[str]] = []

    async def _fake_sync(cls, stage_dir: Path, *, folder_id, stats_callback=None):
        synced_files.append(
            sorted(
                str(p.relative_to(stage_dir))
                for p in stage_dir.rglob("*")
                if p.is_file() or p.is_symlink()
            )
        )

    monkeypatch.setattr(GoogleDriveRclone, "sync_tree", classmethod(_fake_sync))

    result = await ExportService.upload_manifest_to_drive(project, [])

    # The episode left the project sync and went through the shared copy.
    assert len(copied) == 1
    assert copied[0]["folder_id"] == "shared-1"
    assert copied[0]["name"].endswith("__Episode 01.mkv")
    assert synced_files == [
        [
            "atr_remote_sources.json",
            "import_project.jsx",
            "subtitles/atr_subtitles.zip",
        ]
    ]

    manifest = DriveSharedSources.load_local_manifest(project.id)
    assert manifest["status"] == "uploaded"
    assert manifest["drive_folder_id"] == "f-1"
    assert manifest["shared_files"][0]["path"] == "sources/Episode 01.mkv"
    assert manifest["shared_files"][0]["md5"] == "md5-x"
    assert result["shared"] == {"externalized_count": 1, "externalized_bytes": 13}


@pytest.mark.asyncio
async def test_stage_dir_cleaned_up_on_sync_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    entries = _entries(tmp_path)
    project = Project(anime_name="Test Anime")
    monkeypatch.setattr(
        ExportService,
        "build_manifest",
        classmethod(lambda cls, p, m: ("SPM_folder", entries)),
    )
    monkeypatch.setattr(
        GoogleDriveService,
        "ensure_project_folder",
        classmethod(
            lambda cls, name, existing_folder_id=None, drive=None: ("f", "url")
        ),
    )

    async def _boom(cls, stage_dir: Path, *, folder_id, stats_callback=None):
        raise RuntimeError("sync failed")

    monkeypatch.setattr(GoogleDriveRclone, "sync_tree", classmethod(_boom))

    with pytest.raises(RuntimeError, match="sync failed"):
        await ExportService.upload_manifest_to_drive(project, [])

    leftovers = list(settings.cache_dir.glob("atr-drive-export-*"))
    assert leftovers == []
