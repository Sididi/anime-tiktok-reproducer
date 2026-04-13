from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path, PurePosixPath

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
BACKEND_ROOT = REPO_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.config import settings
from app.library_types import LibraryType
from app.models.project import Project
from app.models.project_startup import ProjectStartupJob
from app.services.anime_library import AnimeLibraryService
from app.services.anime_matcher import AnimeMatcherService
from app.services.library_hydration_service import HYDRATION_STATUS_INDEX_READY
from app.services.library_hydration_service import LibraryHydrationService
from app.services.library_hydration_service import OPERATION_COMPLETE, OPERATION_RUNNING
from app.services.library_hydration_service import SeriesDeleteBlockedError
from app.services.library_hydration_service import SeriesRenameConflictError
from app.services.library_state_db import LibraryStateDb
from app.services.project_service import ProjectService
from app.services.project_startup_service import project_startup_queue
from app.services.storage_box_repository import LocalArtifact, StorageBoxRepository
from app.services.storage_box_sftp_client import StorageBoxSftpClient
from app.services.storage_box_transfer import StorageBoxTransferService


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


async def _async_result(value):
    return value


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
    progress_updates: list[tuple[int, int, str]] = []

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

    async def on_progress(completed: int, total: int, relative_path: str) -> None:
        progress_updates.append((completed, total, relative_path))

    await LibraryHydrationService._hydrate_index_artifacts(
        LibraryType.ANIME,
        manifest,
        progress_callback=on_progress,
    )

    assert download_max_active == 2
    assert set(materialized_files) == {
        "series-1/manifest.fragment.json",
        "series-1/state.fragment.json",
        "series-1/series/demo-key/faiss.index",
    }
    assert [update[:2] for update in progress_updates] == [(1, 3), (2, 3), (3, 3)]


@pytest.mark.asyncio
async def test_activate_project_series_skips_index_download_when_local_cache_matches_release(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    display_name = "Demo"
    series_id = "series-1"
    release_id = "release-1"
    shard_key = "demo-key"

    library_root = tmp_path / "library"
    series_dir = library_root / display_name
    index_dir = library_root / AnimeLibraryService.INDEX_DIR_NAME
    shard_dir = index_dir / "series" / shard_key
    shard_dir.mkdir(parents=True, exist_ok=True)
    series_dir.mkdir(parents=True, exist_ok=True)

    (index_dir / AnimeLibraryService.MANIFEST_FILE).write_text(
        json.dumps(
            {
                "version": AnimeLibraryService.SEARCHER_INDEX_FORMAT_VERSION,
                "engine_profile": AnimeLibraryService.SEARCHER_ENGINE_PROFILE,
                "config": {},
                "series": {
                    display_name: {
                        "key": shard_key,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    (index_dir / AnimeLibraryService.STATE_FILE).write_text(
        json.dumps({"files": {}}),
        encoding="utf-8",
    )
    (shard_dir / "faiss.index").write_bytes(b"index-bytes")
    (shard_dir / "metadata.json").write_text("{}", encoding="utf-8")
    StorageBoxRepository.write_local_series_metadata(
        series_dir=series_dir,
        series_id=series_id,
        display_name=display_name,
        release_id=release_id,
    )

    manifest = {
        "series_id": series_id,
        "release_id": release_id,
        "display_name": display_name,
        "episode_count": 1,
        "episodes": [],
        "artifacts": [],
    }

    operation_updates: list[tuple[str, float]] = []
    state_updates: list[tuple[str, int, int]] = []
    hydrate_called = False

    monkeypatch.setattr(settings, "cache_dir", tmp_path / "cache")
    monkeypatch.setattr(
        AnimeLibraryService,
        "get_library_path",
        classmethod(lambda cls, library_type=None: library_root),
    )
    async def fake_get_current_release(
        cls,
        library_type: LibraryType,
        series_id: str,
    ) -> dict[str, str]:
        return {"release_id": release_id}

    async def fake_get_series_manifest(
        cls,
        library_type: LibraryType,
        series_id: str,
        release_id: str | None = None,
    ) -> dict[str, object]:
        return manifest

    monkeypatch.setattr(
        StorageBoxRepository,
        "get_current_release",
        classmethod(fake_get_current_release),
    )
    monkeypatch.setattr(
        StorageBoxRepository,
        "get_series_manifest",
        classmethod(fake_get_series_manifest),
    )
    monkeypatch.setattr(
        LibraryHydrationService,
        "_count_local_episodes_from_manifest",
        classmethod(lambda cls, library_type, manifest: 0),
    )

    async def fake_hydrate_index_artifacts(
        cls,
        library_type: LibraryType,
        manifest: dict,
        progress_callback=None,
    ) -> None:
        nonlocal hydrate_called
        hydrate_called = True

    async def fake_get_activation_state(cls, *, library_type: LibraryType | str, series_id: str) -> dict[str, object]:
        return {"series_id": series_id, "hydration_status": "index_ready"}

    monkeypatch.setattr(
        LibraryHydrationService,
        "_hydrate_index_artifacts",
        classmethod(fake_hydrate_index_artifacts),
    )
    monkeypatch.setattr(
        LibraryHydrationService,
        "get_activation_state",
        classmethod(fake_get_activation_state),
    )
    monkeypatch.setattr(LibraryStateDb, "add_project_pin", lambda project_id, series_id: None)
    monkeypatch.setattr(
        LibraryStateDb,
        "upsert_operation",
        lambda *, library_type, series_id, operation_type, status, progress, error: operation_updates.append(
            (status, float(progress))
        ),
    )
    monkeypatch.setattr(
        LibraryStateDb,
        "upsert_series_state",
        lambda *, library_type, series_id, release_id, hydration_status, local_episode_count, expected_episode_count, last_error, permanent_pin=None: state_updates.append(
            (hydration_status, int(local_episode_count), int(expected_episode_count))
        ),
    )

    result = await LibraryHydrationService.activate_project_series(
        project_id="proj-1",
        library_type=LibraryType.ANIME,
        series_id=series_id,
    )

    assert result["hydration_status"] == "index_ready"
    assert hydrate_called is False
    assert operation_updates == [
        (OPERATION_RUNNING, 0.0),
        (OPERATION_COMPLETE, 1.0),
    ]
    assert state_updates == [
        (HYDRATION_STATUS_INDEX_READY, 0, 1),
    ]


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


@pytest.mark.asyncio
async def test_delete_series_removes_remote_tree_and_rebuilds_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    removed_paths: list[PurePosixPath] = []
    rebuilt_types: list[LibraryType] = []

    async def fake_exists(cls, remote_path: PurePosixPath) -> bool:
        return True

    async def fake_remove_tree(cls, remote_path: PurePosixPath) -> None:
        removed_paths.append(PurePosixPath(remote_path))

    async def fake_rebuild_catalog(cls, library_type: LibraryType) -> dict[str, object]:
        rebuilt_types.append(library_type)
        return {}

    monkeypatch.setattr(StorageBoxSftpClient, "is_configured", classmethod(lambda cls: True))
    monkeypatch.setattr(StorageBoxSftpClient, "exists", classmethod(fake_exists))
    monkeypatch.setattr(StorageBoxSftpClient, "remove_tree", classmethod(fake_remove_tree))
    monkeypatch.setattr(StorageBoxRepository, "rebuild_catalog", classmethod(fake_rebuild_catalog))

    await StorageBoxRepository.delete_series(
        library_type=LibraryType.ANIME,
        series_id="series-1",
    )

    assert removed_paths == [StorageBoxRepository._series_root(LibraryType.ANIME, "series-1")]
    assert rebuilt_types == [LibraryType.ANIME]


@pytest.mark.asyncio
async def test_delete_series_blocks_when_saved_projects_reference_series(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = Project(
        id="project-1",
        anime_name="Demo",
        series_id="series-1",
        library_type=LibraryType.ANIME,
    )

    def fake_evict_local_series_sync(cls, *_args, **_kwargs) -> None:
        raise AssertionError("local eviction should not run when deletion is blocked")

    async def fake_delete_remote_series(cls, **_kwargs) -> None:
        raise AssertionError("remote deletion should not run when deletion is blocked")

    def fake_delete_series_records(**_kwargs) -> None:
        raise AssertionError("db cleanup should not run when deletion is blocked")

    monkeypatch.setattr(
        ProjectService,
        "list_referencing_series",
        classmethod(lambda cls, **kwargs: [project]),
    )
    monkeypatch.setattr(
        LibraryHydrationService,
        "_evict_local_series_sync",
        classmethod(fake_evict_local_series_sync),
    )
    monkeypatch.setattr(
        StorageBoxRepository,
        "delete_series",
        classmethod(fake_delete_remote_series),
    )
    monkeypatch.setattr(LibraryStateDb, "delete_series_records", fake_delete_series_records)

    with pytest.raises(SeriesDeleteBlockedError) as exc_info:
        await LibraryHydrationService.delete_series(
            library_type=LibraryType.ANIME,
            series_id="series-1",
        )

    payload = exc_info.value.to_payload()
    assert payload["code"] == "series_delete_blocked"
    assert payload["referencing_projects"][0]["project_id"] == "project-1"


@pytest.mark.asyncio
async def test_delete_series_cleans_local_state_and_disappears_from_source_listing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    library_root = tmp_path / "library" / "anime"
    series_dir = library_root / "Demo"
    series_dir.mkdir(parents=True, exist_ok=True)
    (series_dir / "ep-1.mp4").write_bytes(b"episode-1")
    StorageBoxRepository.write_local_series_metadata(
        series_dir=series_dir,
        series_id="series-1",
        display_name="Demo",
        release_id="release-1",
    )

    index_dir = library_root / AnimeLibraryService.INDEX_DIR_NAME
    shard_dir = index_dir / "series" / "demo-key"
    shard_dir.mkdir(parents=True, exist_ok=True)
    (shard_dir / "faiss.index").write_bytes(b"index")
    (index_dir / AnimeLibraryService.MANIFEST_FILE).write_text(
        json.dumps(
            {
                "version": 4,
                "engine_profile": "profile",
                "config": {},
                "series": {"Demo": {"key": "demo-key"}},
            }
        ),
        encoding="utf-8",
    )
    (index_dir / AnimeLibraryService.STATE_FILE).write_text(
        json.dumps({"files": {"Demo/ep-1.mp4": {"frames": 1}}}),
        encoding="utf-8",
    )

    cache_dir = tmp_path / "cache"
    manifest_cache_dir = cache_dir / "storage_box" / "manifests" / "anime" / "series-1"
    manifest_cache_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "series_id": "series-1",
        "release_id": "release-1",
        "display_name": "Demo",
        "episode_count": 1,
        "episodes": [
            {
                "episode_key": "ep-1",
                "media": {
                    "local_relative_path": "Demo/ep-1.mp4",
                    "size_bytes": 9,
                },
                "sidecars": [],
            }
        ],
    }
    (manifest_cache_dir / "release-1.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )

    monkeypatch.setattr(settings, "cache_dir", cache_dir)
    monkeypatch.setattr(settings, "library_state_db_path", tmp_path / "library_state.db")
    monkeypatch.setattr(
        AnimeLibraryService,
        "get_library_path",
        classmethod(lambda cls, library_type=None: library_root),
    )
    monkeypatch.setattr(
        ProjectService,
        "list_referencing_series",
        classmethod(lambda cls, **kwargs: []),
    )
    LibraryStateDb.initialize()
    LibraryStateDb.upsert_series_state(
        library_type=LibraryType.ANIME,
        series_id="series-1",
        release_id="release-1",
        permanent_pin=True,
        hydration_status="fully_local",
        local_episode_count=1,
        expected_episode_count=1,
    )
    LibraryStateDb.upsert_operation(
        library_type=LibraryType.ANIME,
        series_id="series-1",
        operation_type="hydrate",
        status="complete",
        progress=1.0,
        error=None,
    )
    LibraryStateDb.add_project_pin("stale-project", "series-1")

    remote_delete_calls: list[tuple[LibraryType, str]] = []

    async def fake_delete_remote_series(
        cls,
        *,
        library_type: LibraryType,
        series_id: str,
    ) -> None:
        remote_delete_calls.append((library_type, series_id))

    async def fake_list_catalog(cls, library_type: LibraryType) -> list[dict[str, object]]:
        return []

    monkeypatch.setattr(
        StorageBoxRepository,
        "delete_series",
        classmethod(fake_delete_remote_series),
    )
    monkeypatch.setattr(
        StorageBoxRepository,
        "list_catalog",
        classmethod(fake_list_catalog),
    )

    result = await LibraryHydrationService.delete_series(
        library_type=LibraryType.ANIME,
        series_id="series-1",
    )
    details = await LibraryHydrationService.list_source_details(
        library_type=LibraryType.ANIME,
    )

    assert result["status"] == "deleted"
    assert remote_delete_calls == [(LibraryType.ANIME, "series-1")]
    assert not series_dir.exists()
    assert not shard_dir.exists()
    assert not manifest_cache_dir.exists()
    assert json.loads((index_dir / AnimeLibraryService.MANIFEST_FILE).read_text())["series"] == {}
    assert json.loads((index_dir / AnimeLibraryService.STATE_FILE).read_text())["files"] == {}
    assert LibraryStateDb.get_series_state(LibraryType.ANIME, "series-1") is None
    assert LibraryStateDb.list_operations(
        library_type=LibraryType.ANIME,
        series_id="series-1",
    ) == []
    assert LibraryStateDb.count_project_pins("series-1") == 0
    assert details == []


@pytest.mark.asyncio
async def test_rename_series_rewrites_remote_json_artifacts_and_hardlinks_unchanged_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    library_root = tmp_path / "library"
    library_root.mkdir(parents=True, exist_ok=True)
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    old_name = "Old Name"
    new_name = "New Name"
    series_id = "series-1"
    old_release_id = "release-1"

    old_absolute_episode = str((library_root / old_name / "ep-1.mp4").resolve())
    new_absolute_episode = str((library_root / new_name / "ep-1.mp4").resolve())

    manifest = {
        "schema_version": 1,
        "library_type": LibraryType.ANIME.value,
        "series_id": series_id,
        "release_id": old_release_id,
        "display_name": old_name,
        "fps": 24.0,
        "created_at": "2026-04-01T10:00:00Z",
        "episode_count": 1,
        "total_size_bytes": 5,
        "torrent_count": 1,
        "torrent_metadata_relative_path": f"payload/library/{old_name}/.atr_torrents.json",
        "episodes": [
            {
                "episode_key": "ep-1",
                "media": {
                    "relative_path": f"payload/library/{old_name}/ep-1.mp4",
                    "local_relative_path": f"{old_name}/ep-1.mp4",
                    "size_bytes": 5,
                    "sha256": "media-sha",
                },
                "sidecars": [
                    {
                        "relative_path": f"payload/library/{old_name}/ep-1.mp4.atr_source.json",
                        "local_relative_path": f"{old_name}/ep-1.mp4.atr_source.json",
                        "size_bytes": 1,
                        "sha256": "source-old",
                        "artifact_type": "source_metadata",
                    }
                ],
            }
        ],
        "artifacts": [
            {
                "relative_path": f"payload/library/{old_name}/ep-1.mp4",
                "local_relative_path": f"{old_name}/ep-1.mp4",
                "size_bytes": 5,
                "sha256": "media-sha",
                "artifact_type": "library",
            },
            {
                "relative_path": f"payload/library/{old_name}/ep-1.mp4.atr_source.json",
                "local_relative_path": f"{old_name}/ep-1.mp4.atr_source.json",
                "size_bytes": 1,
                "sha256": "source-old",
                "artifact_type": "library",
            },
            {
                "relative_path": f"payload/library/{old_name}/.atr_torrents.json",
                "local_relative_path": f"{old_name}/.atr_torrents.json",
                "size_bytes": 1,
                "sha256": "torrent-old",
                "artifact_type": "library",
            },
            {
                "relative_path": f"payload/index/{series_id}/manifest.fragment.json",
                "local_relative_path": None,
                "size_bytes": 1,
                "sha256": "fragment-old",
                "artifact_type": "index",
            },
            {
                "relative_path": f"payload/index/{series_id}/state.fragment.json",
                "local_relative_path": None,
                "size_bytes": 1,
                "sha256": "state-old",
                "artifact_type": "index",
            },
        ],
    }

    remote_json_payloads = {
        f"payload/library/{old_name}/ep-1.mp4.atr_source.json": {
            "source_path": "/downloads/original-ep-1.mkv",
            "prepared_path": old_absolute_episode,
            "sidecar_source_path": old_absolute_episode,
            "original_source_path": "/downloads/original-ep-1.mkv",
        },
        f"payload/library/{old_name}/.atr_torrents.json": {
            "torrents": [
                {
                    "files": [
                        {
                            "library_path": old_absolute_episode,
                        }
                    ]
                }
            ],
            "purge_protection": False,
        },
        f"payload/index/{series_id}/manifest.fragment.json": {
            "schema_version": 1,
            "series": {
                old_name: {
                    "key": "rename-key",
                    "fps": 24.0,
                }
            },
        },
        f"payload/index/{series_id}/state.fragment.json": {
            "schema_version": 1,
            "files": {
                f"{old_name}/ep-1.mp4": {"frames": 1},
            },
        },
    }

    uploaded_payloads: dict[str, str] = {}
    written_texts: dict[str, str] = {}
    replace_calls: list[tuple[PurePosixPath, PurePosixPath]] = []
    hardlink_calls: list[tuple[PurePosixPath, PurePosixPath]] = []
    verify_calls: list[list[str]] = []

    monkeypatch.setattr(settings, "cache_dir", cache_dir)
    monkeypatch.setattr(StorageBoxSftpClient, "is_configured", classmethod(lambda cls: True))
    monkeypatch.setattr(
        AnimeLibraryService,
        "get_library_path",
        classmethod(lambda cls, library_type=None: library_root),
    )
    monkeypatch.setattr(
        StorageBoxRepository,
        "get_current_release",
        classmethod(lambda cls, library_type, series_id: _async_result({"release_id": old_release_id})),
    )
    monkeypatch.setattr(
        StorageBoxRepository,
        "get_series_manifest",
        classmethod(lambda cls, library_type, series_id, release_id=None: _async_result(manifest)),
    )

    async def fake_read_remote_json(
        cls,
        remote_path: PurePosixPath,
        *,
        context: str,
    ) -> dict[str, object]:
        relative = str(
            PurePosixPath(remote_path).relative_to(
                StorageBoxRepository._release_root(
                    LibraryType.ANIME,
                    series_id,
                    old_release_id,
                )
            )
        )
        payload = remote_json_payloads[relative]
        return json.loads(json.dumps(payload))

    async def fake_upload_file(
        cls,
        local_path: Path,
        remote_path: PurePosixPath,
    ) -> None:
        uploaded_payloads[str(remote_path)] = local_path.read_text(encoding="utf-8")

    async def fake_write_text(
        cls,
        remote_path: PurePosixPath,
        content: str,
    ) -> None:
        written_texts[str(remote_path)] = content

    async def fake_replace_file(
        cls,
        src: PurePosixPath,
        dst: PurePosixPath,
    ) -> None:
        replace_calls.append((PurePosixPath(src), PurePosixPath(dst)))

    async def fake_try_hardlink_first(
        cls,
        src: PurePosixPath,
        dst: PurePosixPath,
    ) -> bool:
        hardlink_calls.append((PurePosixPath(src), PurePosixPath(dst)))
        return True

    async def fake_verify_remote_artifacts(
        cls,
        *,
        staging_root: PurePosixPath,
        artifacts: list[LocalArtifact],
    ) -> None:
        verify_calls.append([str(staging_root / artifact.remote_relative_path) for artifact in artifacts])

    monkeypatch.setattr(
        StorageBoxRepository,
        "_read_remote_json",
        classmethod(fake_read_remote_json),
    )
    monkeypatch.setattr(
        StorageBoxTransferService,
        "upload_file",
        classmethod(fake_upload_file),
    )
    monkeypatch.setattr(StorageBoxSftpClient, "write_text", classmethod(fake_write_text))
    monkeypatch.setattr(StorageBoxSftpClient, "rename", classmethod(lambda cls, src, dst: _async_result(None)))
    monkeypatch.setattr(StorageBoxSftpClient, "replace_file", classmethod(fake_replace_file))
    monkeypatch.setattr(
        StorageBoxRepository,
        "_try_hardlink_first",
        classmethod(fake_try_hardlink_first),
    )
    monkeypatch.setattr(
        StorageBoxRepository,
        "_verify_remote_artifacts",
        classmethod(fake_verify_remote_artifacts),
    )
    monkeypatch.setattr(
        StorageBoxRepository,
        "rebuild_catalog",
        classmethod(lambda cls, library_type: _async_result({})),
    )

    result = await StorageBoxRepository.rename_series(
        library_type=LibraryType.ANIME,
        series_id=series_id,
        new_display_name=new_name,
    )

    assert result["series_id"] == series_id
    assert result["old_name"] == old_name
    assert result["new_name"] == new_name
    assert result["release_id"] != old_release_id

    source_upload = next(
        value
        for path, value in uploaded_payloads.items()
        if path.endswith(f"payload/library/{new_name}/ep-1.mp4.atr_source.json")
    )
    assert new_absolute_episode in source_upload
    assert old_absolute_episode not in source_upload

    torrents_upload = next(
        value
        for path, value in uploaded_payloads.items()
        if path.endswith(f"payload/library/{new_name}/.atr_torrents.json")
    )
    assert new_absolute_episode in torrents_upload
    assert old_absolute_episode not in torrents_upload

    manifest_fragment_upload = next(
        value
        for path, value in uploaded_payloads.items()
        if path.endswith(f"payload/index/{series_id}/manifest.fragment.json")
    )
    assert new_name in manifest_fragment_upload
    assert old_name not in manifest_fragment_upload

    state_fragment_upload = next(
        value
        for path, value in uploaded_payloads.items()
        if path.endswith(f"payload/index/{series_id}/state.fragment.json")
    )
    assert f"{new_name}/ep-1.mp4" in state_fragment_upload
    assert f"{old_name}/ep-1.mp4" not in state_fragment_upload

    series_manifest_text = next(
        value
        for path, value in written_texts.items()
        if path.endswith("series_manifest.json")
    )
    rewritten_manifest = json.loads(series_manifest_text)
    assert rewritten_manifest["display_name"] == new_name
    assert rewritten_manifest["release_id"] == result["release_id"]
    assert rewritten_manifest["episodes"][0]["media"]["local_relative_path"] == f"{new_name}/ep-1.mp4"
    assert rewritten_manifest["artifacts"][0]["relative_path"] == f"payload/library/{new_name}/ep-1.mp4"

    current_payload = json.loads(
        next(value for path, value in written_texts.items() if "/current." in path)
    )
    assert current_payload["display_name"] == new_name
    assert current_payload["release_id"] == result["release_id"]
    assert replace_calls and replace_calls[0][1] == StorageBoxRepository._current_path(
        LibraryType.ANIME,
        series_id,
    )
    assert hardlink_calls == [
        (
            StorageBoxRepository._release_root(
                LibraryType.ANIME,
                series_id,
                old_release_id,
            ) / f"payload/library/{old_name}/ep-1.mp4",
            PurePosixPath(
                next(path for path in verify_calls[0] if path.endswith(f"payload/library/{new_name}/ep-1.mp4"))
            ),
        )
    ]


@pytest.mark.asyncio
async def test_rename_series_updates_local_state_and_saved_references(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    old_name = "Old Name"
    new_name = "New Name"
    series_id = "series-1"
    old_release_id = "release-1"
    new_release_id = "release-2"

    library_root = tmp_path / "library"
    old_dir = library_root / old_name
    old_dir.mkdir(parents=True, exist_ok=True)
    (old_dir / "ep-1.mp4").write_bytes(b"episode-1")
    StorageBoxRepository.write_local_series_metadata(
        series_dir=old_dir,
        series_id=series_id,
        display_name=old_name,
        release_id=old_release_id,
    )
    (old_dir / "ep-1.mp4.atr_source.json").write_text(
        json.dumps(
            {
                "source_path": "/downloads/original-ep-1.mkv",
                "prepared_path": str((old_dir / "ep-1.mp4").resolve()),
                "sidecar_source_path": str((old_dir / "ep-1.mp4").resolve()),
            }
        ),
        encoding="utf-8",
    )
    subtitle_dir = old_dir / "ep-1.atr_subtitles"
    subtitle_dir.mkdir(parents=True, exist_ok=True)
    (subtitle_dir / "manifest.json").write_text(
        json.dumps(
            {
                "source_path": str((old_dir / "ep-1.mp4").resolve()),
                "generated_from": "/downloads/original-ep-1.mkv",
            }
        ),
        encoding="utf-8",
    )
    (old_dir / ".atr_torrents.json").write_text(
        json.dumps(
            {
                "torrents": [
                    {
                        "files": [
                            {
                                "library_path": str((old_dir / "ep-1.mp4").resolve()),
                            }
                        ]
                    }
                ],
                "purge_protection": False,
            }
        ),
        encoding="utf-8",
    )

    index_dir = library_root / AnimeLibraryService.INDEX_DIR_NAME
    shard_dir = index_dir / "series" / "rename-key"
    shard_dir.mkdir(parents=True, exist_ok=True)
    (shard_dir / "faiss.index").write_bytes(b"index")
    (shard_dir / "metadata.json").write_text("{}", encoding="utf-8")
    (index_dir / AnimeLibraryService.MANIFEST_FILE).write_text(
        json.dumps(
            {
                "version": AnimeLibraryService.SEARCHER_INDEX_FORMAT_VERSION,
                "engine_profile": AnimeLibraryService.SEARCHER_ENGINE_PROFILE,
                "config": {},
                "series": {
                    old_name: {
                        "key": "rename-key",
                        "fps": 24.0,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    (index_dir / AnimeLibraryService.STATE_FILE).write_text(
        json.dumps(
            {
                "files": {
                    f"{old_name}/ep-1.mp4": {"frames": 1},
                }
            }
        ),
        encoding="utf-8",
    )

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(settings, "cache_dir", cache_dir)
    monkeypatch.setattr(settings, "projects_dir", projects_dir)
    monkeypatch.setattr(settings, "library_state_db_path", tmp_path / "library_state.db")
    monkeypatch.setattr(
        AnimeLibraryService,
        "get_library_path",
        classmethod(lambda cls, library_type=None: library_root),
    )

    release_state = {"release_id": old_release_id, "display_name": old_name}
    old_manifest = {
        "series_id": series_id,
        "release_id": old_release_id,
        "display_name": old_name,
        "episode_count": 1,
        "episodes": [
            {
                "episode_key": "ep-1",
                "media": {
                    "local_relative_path": f"{old_name}/ep-1.mp4",
                    "size_bytes": 9,
                },
                "sidecars": [],
            }
        ],
    }
    new_manifest = {
        "series_id": series_id,
        "release_id": new_release_id,
        "display_name": new_name,
        "episode_count": 1,
        "episodes": [
            {
                "episode_key": "ep-1",
                "media": {
                    "local_relative_path": f"{new_name}/ep-1.mp4",
                    "size_bytes": 9,
                },
                "sidecars": [],
            }
        ],
    }

    AnimeMatcherService._stale_series[LibraryType.ANIME].clear()
    LibraryStateDb.initialize()
    LibraryStateDb.upsert_series_state(
        library_type=LibraryType.ANIME,
        series_id=series_id,
        release_id=old_release_id,
        hydration_status="fully_local",
        local_episode_count=1,
        expected_episode_count=1,
    )

    project = ProjectService.create(
        anime_name=old_name,
        series_id=series_id,
        library_type=LibraryType.ANIME,
    )
    startup_job = ProjectStartupJob(
        project_id=project.id,
        anime_name=old_name,
        series_id=series_id,
        library_type=LibraryType.ANIME,
    )
    monkeypatch.setattr(project_startup_queue, "_jobs_path", tmp_path / "project_startup_jobs.json", raising=False)
    monkeypatch.setattr(project_startup_queue, "_jobs", {project.id: startup_job}, raising=False)
    monkeypatch.setattr(project_startup_queue, "_subscribers", [], raising=False)

    async def fake_get_current_release(
        cls,
        library_type: LibraryType,
        request_series_id: str,
    ) -> dict[str, str]:
        assert request_series_id == series_id
        return {
            "release_id": release_state["release_id"],
            "display_name": release_state["display_name"],
        }

    async def fake_get_series_manifest(
        cls,
        library_type: LibraryType,
        request_series_id: str,
        release_id: str | None = None,
    ) -> dict[str, object]:
        assert request_series_id == series_id
        effective_release_id = release_id or release_state["release_id"]
        if effective_release_id == new_release_id:
            return new_manifest
        return old_manifest

    async def fake_remote_rename(
        cls,
        *,
        library_type: LibraryType,
        series_id: str,
        new_display_name: str,
    ) -> dict[str, object]:
        release_state["release_id"] = new_release_id
        release_state["display_name"] = new_display_name
        return {
            "series_id": series_id,
            "release_id": new_release_id,
            "old_name": old_name,
            "new_name": new_display_name,
            "manifest": new_manifest,
            "current": {
                "release_id": new_release_id,
                "display_name": new_display_name,
            },
        }

    async def fake_find_catalog_entry_by_name(
        cls,
        library_type: LibraryType,
        display_name: str,
    ) -> dict[str, object] | None:
        return None

    async def fake_find_remote_series_id_by_name(
        cls,
        library_type: LibraryType,
        display_name: str,
    ) -> str | None:
        return None

    async def fake_list_catalog(
        cls,
        library_type: LibraryType,
    ) -> list[dict[str, object]]:
        return [
            {
                "series_id": series_id,
                "name": release_state["display_name"],
                "storage_release_id": release_state["release_id"],
                "episode_count": 1,
                "total_size_bytes": 9,
                "fps": 24.0,
                "torrent_count": 1,
                "updated_at": "2026-04-01T12:00:00Z",
            }
        ]

    monkeypatch.setattr(StorageBoxRepository, "get_current_release", classmethod(fake_get_current_release))
    monkeypatch.setattr(StorageBoxRepository, "get_series_manifest", classmethod(fake_get_series_manifest))
    monkeypatch.setattr(StorageBoxRepository, "rename_series", classmethod(fake_remote_rename))
    monkeypatch.setattr(
        StorageBoxRepository,
        "find_catalog_entry_by_name",
        classmethod(fake_find_catalog_entry_by_name),
    )
    monkeypatch.setattr(
        StorageBoxRepository,
        "find_remote_series_id_by_name",
        classmethod(fake_find_remote_series_id_by_name),
    )
    monkeypatch.setattr(StorageBoxRepository, "list_catalog", classmethod(fake_list_catalog))

    result = await LibraryHydrationService.rename_series(
        library_type=LibraryType.ANIME,
        series_id=series_id,
        new_name=new_name,
    )
    details = await LibraryHydrationService.list_source_details(
        library_type=LibraryType.ANIME,
    )

    new_dir = library_root / new_name
    assert result["status"] == "renamed"
    assert not old_dir.exists()
    assert new_dir.exists()
    metadata = StorageBoxRepository.read_local_series_metadata(new_dir)
    assert metadata["series_id"] == series_id
    assert metadata["display_name"] == new_name
    assert metadata["release_id"] == new_release_id

    source_manifest = json.loads((new_dir / "ep-1.mp4.atr_source.json").read_text(encoding="utf-8"))
    assert source_manifest["prepared_path"] == str((new_dir / "ep-1.mp4").resolve())
    assert source_manifest["sidecar_source_path"] == str((new_dir / "ep-1.mp4").resolve())
    assert source_manifest["source_path"] == "/downloads/original-ep-1.mkv"

    subtitle_manifest = json.loads((new_dir / "ep-1.atr_subtitles" / "manifest.json").read_text(encoding="utf-8"))
    assert subtitle_manifest["source_path"] == str((new_dir / "ep-1.mp4").resolve())

    torrents_payload = json.loads((new_dir / ".atr_torrents.json").read_text(encoding="utf-8"))
    assert torrents_payload["torrents"][0]["files"][0]["library_path"] == str(
        (new_dir / "ep-1.mp4").resolve()
    )

    local_manifest = json.loads((index_dir / AnimeLibraryService.MANIFEST_FILE).read_text(encoding="utf-8"))
    assert old_name not in local_manifest["series"]
    assert new_name in local_manifest["series"]

    local_state = json.loads((index_dir / AnimeLibraryService.STATE_FILE).read_text(encoding="utf-8"))
    assert f"{new_name}/ep-1.mp4" in local_state["files"]
    assert f"{old_name}/ep-1.mp4" not in local_state["files"]

    reloaded_project = ProjectService.load(project.id)
    assert reloaded_project is not None
    assert reloaded_project.anime_name == new_name

    assert project_startup_queue._jobs[project.id].anime_name == new_name
    startup_jobs_payload = json.loads((tmp_path / "project_startup_jobs.json").read_text(encoding="utf-8"))
    assert startup_jobs_payload["jobs"][0]["anime_name"] == new_name

    assert details[0]["name"] == new_name
    assert details[0]["storage_release_id"] == new_release_id
    assert old_name in AnimeMatcherService._stale_series[LibraryType.ANIME]
    assert new_name in AnimeMatcherService._stale_series[LibraryType.ANIME]


@pytest.mark.asyncio
async def test_rename_series_rejects_conflicting_target_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_get_current_release(
        cls,
        library_type: LibraryType,
        series_id: str,
    ) -> dict[str, str]:
        return {"release_id": "release-1"}

    async def fake_get_series_manifest(
        cls,
        library_type: LibraryType,
        series_id: str,
        release_id: str | None = None,
    ) -> dict[str, object]:
        return {
            "series_id": series_id,
            "release_id": "release-1",
            "display_name": "Old Name",
            "episode_count": 0,
            "episodes": [],
        }

    async def fake_find_catalog_entry_by_name(
        cls,
        library_type: LibraryType,
        display_name: str,
    ) -> dict[str, object] | None:
        return {"series_id": "series-2"}

    async def fake_remote_rename(
        cls,
        *,
        library_type: LibraryType,
        series_id: str,
        new_display_name: str,
    ) -> dict[str, object]:
        raise AssertionError("remote rename should not run on name conflict")

    monkeypatch.setattr(StorageBoxRepository, "get_current_release", classmethod(fake_get_current_release))
    monkeypatch.setattr(StorageBoxRepository, "get_series_manifest", classmethod(fake_get_series_manifest))
    monkeypatch.setattr(
        StorageBoxRepository,
        "find_catalog_entry_by_name",
        classmethod(fake_find_catalog_entry_by_name),
    )
    monkeypatch.setattr(StorageBoxRepository, "rename_series", classmethod(fake_remote_rename))

    with pytest.raises(SeriesRenameConflictError):
        await LibraryHydrationService.rename_series(
            library_type=LibraryType.ANIME,
            series_id="series-1",
            new_name="Taken Name",
        )


@pytest.mark.asyncio
async def test_rename_series_rejects_active_library_operations(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(settings, "library_state_db_path", tmp_path / "library_state.db")
    LibraryStateDb.initialize()
    LibraryStateDb.upsert_operation(
        library_type=LibraryType.ANIME,
        series_id="series-1",
        operation_type="hydrate",
        status=OPERATION_RUNNING,
        progress=0.4,
        error=None,
    )

    async def fake_get_current_release(
        cls,
        library_type: LibraryType,
        series_id: str,
    ) -> dict[str, str]:
        raise AssertionError("current release should not be queried when rename is blocked")

    monkeypatch.setattr(StorageBoxRepository, "get_current_release", classmethod(fake_get_current_release))

    with pytest.raises(SeriesRenameConflictError):
        await LibraryHydrationService.rename_series(
            library_type=LibraryType.ANIME,
            series_id="series-1",
            new_name="Blocked Rename",
        )
