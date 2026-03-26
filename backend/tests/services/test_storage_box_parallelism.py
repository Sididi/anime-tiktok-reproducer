from __future__ import annotations

import asyncio
import hashlib
import sys
from pathlib import Path, PurePosixPath

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
BACKEND_ROOT = REPO_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.config import settings
from app.library_types import LibraryType
from app.services.anime_library import AnimeLibraryService
from app.services.library_hydration_service import LibraryHydrationService
from app.services.library_hydration_service import OPERATION_COMPLETE, OPERATION_RUNNING
from app.services.library_state_db import LibraryStateDb
from app.services.storage_box_repository import LocalArtifact, StorageBoxRepository
from app.services.storage_box_sftp_client import StorageBoxSftpClient
from app.services.storage_box_transfer import StorageBoxTransferService


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


@pytest.mark.asyncio
async def test_publish_series_uploads_artifacts_in_parallel_and_waits_for_verification(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    display_name = "Demo Series"
    library_root = tmp_path / "library"
    series_dir = library_root / display_name
    series_dir.mkdir(parents=True, exist_ok=True)
    temp_root = tmp_path / "publish-temp"
    temp_root.mkdir()
    local_files: list[Path] = []
    for index in range(3):
        path = tmp_path / f"artifact-{index}.bin"
        path.write_bytes(bytes([index]) * (1024 + index))
        local_files.append(path)

    artifacts = [
        LocalArtifact(
            local_path=path,
            remote_relative_path=PurePosixPath(f"payload/library/{display_name}/file-{index}.bin"),
            size_bytes=path.stat().st_size,
            sha256=_sha256_bytes(path.read_bytes()),
            artifact_type="library",
            local_relative_path=f"{display_name}/file-{index}.bin",
        )
        for index, path in enumerate(local_files)
    ]
    episodes = [
        {
            "episode_key": "ep-1",
            "media": {
                "size_bytes": artifacts[0].size_bytes,
            },
            "sidecars": [],
        }
    ]

    upload_active = 0
    upload_max_active = 0
    upload_completed = 0
    stat_completed = 0

    monkeypatch.setattr(settings, "storage_box_upload_max_parallel", 2)
    monkeypatch.setattr(StorageBoxSftpClient, "is_configured", classmethod(lambda cls: True))
    monkeypatch.setattr(AnimeLibraryService, "get_library_path", classmethod(lambda cls, library_type=None: library_root))

    async def fake_resolve_or_create_series_id(cls, **kwargs) -> str:
        return "series-1"

    async def fake_rebuild_catalog(cls, *_args, **_kwargs) -> dict[str, object]:
        return {}

    monkeypatch.setattr(
        StorageBoxRepository,
        "_resolve_or_create_series_id",
        classmethod(fake_resolve_or_create_series_id),
    )
    monkeypatch.setattr(
        StorageBoxRepository,
        "_collect_series_artifacts",
        classmethod(lambda cls, **kwargs: (artifacts, episodes, temp_root)),
    )
    monkeypatch.setattr(
        StorageBoxRepository,
        "_read_local_index_series_payload",
        classmethod(lambda cls, **kwargs: ({"series": {display_name: {"fps": 24.0}}}, {}, "key")),
    )
    monkeypatch.setattr(StorageBoxRepository, "rebuild_catalog", classmethod(fake_rebuild_catalog))
    monkeypatch.setattr(StorageBoxRepository, "write_local_series_metadata", classmethod(lambda cls, **kwargs: None))

    async def fake_upload(cls, local_path: Path, remote_path: PurePosixPath) -> None:
        nonlocal upload_active, upload_max_active, upload_completed
        upload_active += 1
        upload_max_active = max(upload_max_active, upload_active)
        try:
            await asyncio.sleep(0.03 if local_path.name.endswith("0.bin") else 0.01)
            upload_completed += 1
        finally:
            upload_active -= 1

    async def fake_stat(cls, remote_path: PurePosixPath):
        nonlocal stat_completed
        await asyncio.sleep(0.01)
        stat_completed += 1

        class _Stat:
            size = next(
                artifact.size_bytes
                for artifact in artifacts
                if artifact.remote_relative_path.name == PurePosixPath(remote_path).name
            )

        return _Stat()

    async def fake_write_text(cls, remote_path: PurePosixPath, content: str) -> None:
        if PurePosixPath(remote_path).name == "series_manifest.json":
            assert upload_completed == len(artifacts)
            assert stat_completed == len(artifacts)

    async def fake_read_text(cls, remote_path: PurePosixPath) -> str:
        raise FileNotFoundError("no existing current.json")

    async def fake_rename(cls, src: PurePosixPath, dst: PurePosixPath) -> None:
        return None

    async def fake_replace_file(cls, src: PurePosixPath, dst: PurePosixPath) -> None:
        return None

    monkeypatch.setattr(StorageBoxTransferService, "upload_file", classmethod(fake_upload))
    monkeypatch.setattr(StorageBoxSftpClient, "stat", classmethod(fake_stat))
    monkeypatch.setattr(StorageBoxSftpClient, "write_text", classmethod(fake_write_text))
    monkeypatch.setattr(StorageBoxSftpClient, "read_text", classmethod(fake_read_text))
    monkeypatch.setattr(StorageBoxSftpClient, "rename", classmethod(fake_rename))
    monkeypatch.setattr(StorageBoxSftpClient, "replace_file", classmethod(fake_replace_file))

    result = await StorageBoxRepository.publish_series(
        library_type=LibraryType.ANIME,
        display_name=display_name,
    )

    assert result["series_id"] == "series-1"
    assert upload_max_active == 2
    assert upload_completed == len(artifacts)
    assert stat_completed == len(artifacts)


@pytest.mark.asyncio
async def test_hydrate_index_artifacts_downloads_in_parallel(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(settings, "storage_box_download_max_parallel", 2)
    payloads = {
        "payload/index/series-1/manifest.fragment.json": b'{"series": {"Demo": {"key": "demo-key"}}}',
        "payload/index/series-1/state.fragment.json": b'{"files": {}}',
        "payload/index/series-1/series/demo-key/faiss.index": b"index-bytes",
    }
    manifest = {
        "series_id": "series-1",
        "release_id": "release-1",
        "display_name": "Demo",
        "artifacts": [
            {
                "relative_path": relative_path,
                "artifact_type": "index",
                "sha256": _sha256_bytes(content),
            }
            for relative_path, content in payloads.items()
        ],
    }

    download_active = 0
    download_max_active = 0
    materialized_files: list[str] = []

    async def fake_download(cls, remote_path: PurePosixPath, local_path: Path) -> None:
        nonlocal download_active, download_max_active
        download_active += 1
        download_max_active = max(download_max_active, download_active)
        try:
            await asyncio.sleep(0.02 if local_path.name.endswith(".index") else 0.01)
            local_path.parent.mkdir(parents=True, exist_ok=True)
            relative = str(PurePosixPath(remote_path).relative_to(StorageBoxRepository._release_root(LibraryType.ANIME, "series-1", "release-1")))
            local_path.write_bytes(payloads[relative])
        finally:
            download_active -= 1

    async def fake_materialize(cls, library_type: LibraryType, manifest: dict, temp_root: Path) -> None:
        materialized_files.extend(
            sorted(
                str(path.relative_to(temp_root))
                for path in temp_root.rglob("*")
                if path.is_file()
            )
        )

    monkeypatch.setattr(StorageBoxTransferService, "download_file", classmethod(fake_download))
    monkeypatch.setattr(
        LibraryHydrationService,
        "_materialize_local_matcher_cache",
        classmethod(fake_materialize),
    )

    await LibraryHydrationService._hydrate_index_artifacts(LibraryType.ANIME, manifest)

    assert download_max_active == 2
    assert set(materialized_files) == {
        "series-1/manifest.fragment.json",
        "series-1/state.fragment.json",
        "series-1/series/demo-key/faiss.index",
    }


@pytest.mark.asyncio
async def test_hydrate_series_runs_episode_downloads_with_bounded_parallelism(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "storage_box_download_max_parallel", 2)
    manifest = {
        "series_id": "series-1",
        "release_id": "release-1",
        "display_name": "Demo",
        "episode_count": 3,
        "episodes": [
            {"episode_key": "ep-1", "media": {"local_relative_path": "Demo/ep-1.mp4"}},
            {"episode_key": "ep-2", "media": {"local_relative_path": "Demo/ep-2.mp4"}},
            {"episode_key": "ep-3", "media": {"local_relative_path": "Demo/ep-3.mp4"}},
        ],
    }
    completed: list[str] = []
    upsert_calls: list[tuple[str, float]] = []
    active = 0
    max_active = 0

    async def fake_load_or_fetch_manifest(cls, library_type: LibraryType, series_id: str) -> dict:
        return manifest

    async def fake_hydrate_episode(cls, library_type: LibraryType, manifest: dict, episode: dict) -> None:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        try:
            delays = {"ep-1": 0.05, "ep-2": 0.01, "ep-3": 0.01}
            await asyncio.sleep(delays[str(episode["episode_key"])])
            completed.append(str(episode["episode_key"]))
        finally:
            active -= 1

    def fake_upsert_operation(
        *,
        library_type,
        series_id,
        operation_type,
        status,
        progress,
        error,
    ) -> None:
        upsert_calls.append((status, round(float(progress), 3)))

    def fake_upsert_series_state(**kwargs) -> None:
        return None

    def fake_count_local_episodes(cls, library_type: LibraryType, manifest: dict) -> int:
        return len(completed)

    async def fake_describe_series(cls, library_type: LibraryType, series_id: str) -> dict[str, object]:
        return {"series_id": series_id, "completed": list(completed)}

    monkeypatch.setattr(
        LibraryHydrationService,
        "_load_or_fetch_manifest",
        classmethod(fake_load_or_fetch_manifest),
    )
    monkeypatch.setattr(
        LibraryHydrationService,
        "_hydrate_episode",
        classmethod(fake_hydrate_episode),
    )
    monkeypatch.setattr(
        LibraryHydrationService,
        "_count_local_episodes_from_manifest",
        classmethod(fake_count_local_episodes),
    )
    monkeypatch.setattr(
        LibraryHydrationService,
        "describe_series",
        classmethod(fake_describe_series),
    )
    monkeypatch.setattr(LibraryStateDb, "upsert_operation", fake_upsert_operation)
    monkeypatch.setattr(LibraryStateDb, "upsert_series_state", fake_upsert_series_state)

    result = await LibraryHydrationService.hydrate_series(
        library_type=LibraryType.ANIME,
        series_id="series-1",
        full_series=True,
    )

    running_progresses = [progress for status, progress in upsert_calls if status == OPERATION_RUNNING]
    assert completed == ["ep-2", "ep-3", "ep-1"]
    assert max_active == 2
    assert running_progresses == sorted(running_progresses)
    assert running_progresses[-1] == 1.0
    assert upsert_calls[-1] == (OPERATION_COMPLETE, 1.0)
    assert result["completed"] == completed
