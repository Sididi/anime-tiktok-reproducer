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
from app.services.anime_library import AnimeLibraryService
from app.services.library_hydration_service import HYDRATION_STATUS_INDEX_READY
from app.services.library_hydration_service import LibraryHydrationService
from app.services.library_hydration_service import OPERATION_COMPLETE, OPERATION_RUNNING
from app.services.library_hydration_service import SeriesDeleteBlockedError
from app.services.library_state_db import LibraryStateDb
from app.services.project_service import ProjectService
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

    async def fake_hydrate_index_artifacts(cls, library_type: LibraryType, manifest: dict) -> None:
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
