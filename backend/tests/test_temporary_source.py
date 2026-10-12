"""Private source lifecycle, routing and isolation (no network or GPU required)."""
from __future__ import annotations

import asyncio
import json
import shutil
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings
from app.library_types import LibraryType, LocalLibrary
from app.models import Project, ProjectPhase, Scene, SceneList, MatchList, SceneMatch
from app.models.temporary_source import TemporarySourceInput
from app.services.anime_library import AnimeLibraryService as Library, IndexProgress, SourceMediaProbe
from app.services.downloader import DownloaderService, DownloadProgress
from app.services.export_service import ExportService
from app.services.gap_resolution import GapResolutionService
from app.services.indexation_queue import IndexationQueueService
from app.services.match_playback_service import MatchPlaybackService
from app.services.project_duplication_service import ProjectDuplicationService, DuplicationVariant
from app.services.project_service import ProjectService as Projects
from app.services.project_startup_service import ProjectStartupService
from app.services.scene_detector import SceneDetectorService, SceneDetectionProgress
from app.services.temporary_source_service import TemporarySourceService as Sources
from app.utils.file_tasks import file_io


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    for key in ("data_dir", "projects_dir", "cache_dir", "anime_library_path"):
        path = tmp_path / key
        path.mkdir()
        monkeypatch.setattr(settings, key, path)
    monkeypatch.setattr(Projects, "sync_project_pin", lambda _project: None)
    monkeypatch.setattr("app.services.project_service.LibraryStateDb.remove_project_pins", lambda _id: None)
    monkeypatch.setattr(Library, "_episode_manifest_cache", {})
    monkeypatch.setattr(Library, "_episode_manifest_locks", {})
    monkeypatch.setattr(MatchPlaybackService, "_prepare_locks", {})
    monkeypatch.setattr(MatchPlaybackService, "_clip_tasks", {})
    monkeypatch.setattr("app.services.indexation_queue.indexation_queue", IndexationQueueService())
    monkeypatch.setattr(Sources, "_deleting", set())
    folder = tmp_path / "originals"
    folder.mkdir()
    (folder / "episode.mp4").write_bytes(b"original video bytes")
    nested = folder / "nested"
    nested.mkdir()
    (nested / "excluded.mp4").write_bytes(b"nested video")
    return folder


def create(folder, **kwargs):
    return Projects.create(
        tiktok_url="https://www.tiktok.com/@test/video/123",
        library_type=LibraryType.PURE,
        temporary_source=TemporarySourceInput(kind="folder", value=str(folder)),
        **kwargs,
    )


def write_index(library):
    index = library / ".index"
    shard = index / "series" / "source"
    shard.mkdir(parents=True, exist_ok=True)
    (index / "manifest.json").write_text(json.dumps({"series": {"source": {"fps": 2}}}))
    (shard / "index.faiss").write_bytes(b"stand-in for GPU embeddings")
    (shard / "metadata.json").write_text('{}')
    (index / "state.json").write_text('{}')


def ready(project):
    library = Library.get_library_path(project.media_library)
    video = library / "source" / "episode.mp4"
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"project copy")
    write_index(library)
    Library._scan_library_episodes_sync(project.media_library)
    project.source_paths = [str(video.parent)]
    project.temporary_source.status = "ready"
    Projects.save(project)
    (Sources.root(project.id) / "ready.json").write_text(json.dumps(Sources._ready_files(project)))
    return video


@pytest.fixture
def indexer(monkeypatch):
    """Use the real importer/copy/manifest code; substitute media probes and GPU."""
    calls = []
    def probe(path):
        return SourceMediaProbe(
            source_path=path, container_suffix=".mp4", format_name="mp4",
            video_codec="h264", audio_codec=None, pix_fmt="yuv420p",
            fps=30, duration=2, has_audio=False,
        )
    monkeypatch.setattr(Library, "_probe_media_sync", probe)
    monkeypatch.setattr(Library, "get_primary_video_codec_sync", lambda _path: "h264")
    monkeypatch.setattr(Library, "_verify_prepared_library_files", AsyncMock(return_value=None))
    async def stream(**kwargs):
        calls.append(kwargs["cmd"])
        command = kwargs["cmd"]
        library = Path(command[command.index("index") + 1]) / "anime"
        write_index(library)
        yield IndexProgress(status="indexing", progress=0.95, message="indexed")
    monkeypatch.setattr(Library, "_stream_searcher_index_command", stream)
    def forbidden(*args, **kwargs):
        raise AssertionError("Private sources must not enter catalog or Storage Box publishing")
    monkeypatch.setattr("app.services.library_hydration_service.LibraryHydrationService.finalize_series_release", forbidden)
    monkeypatch.setattr(IndexationQueueService, "enqueue", forbidden)
    return calls


def test_source_input_and_project_constraints(isolated):
    project = create(isolated)
    assert project.library_type == LibraryType.PURE
    assert project.publishing_type == "pure"
    assert not project.uses_identity_source
    assert isinstance(project.media_library, LocalLibrary)
    assert SceneDetectorService.default_threshold(project.media_library) == 16
    assert Project(library_type="pure").uses_identity_source
    with pytest.raises(ValidationError):
        TemporarySourceInput(kind="url", value="file:///private/video.mp4")
    with pytest.raises(ValidationError):
        TemporarySourceInput(kind="folder", value=" ")
    with pytest.raises(ValidationError):
        TemporarySourceInput(kind="url", value="https://example.test/video", fps=3)
    with pytest.raises(ValidationError):
        Project(library_type="anime", temporary_source=project.temporary_source)
    with pytest.raises(ValidationError):
        Project(library_type="pure", series_id="remote", temporary_source=project.temporary_source)


def test_source_uses_anime_speed_floor_and_keeps_pure_prompts(isolated, monkeypatch):
    from app.services.prompt_resolver import PromptResolver, SCRIPT
    project = create(isolated)
    monkeypatch.setattr(Project, "_resolved_template", lambda self: SimpleNamespace(min_playback_speed=0.75))
    assert project.resolved_min_playback_speed() == 0.75
    assert Project(library_type="pure").resolved_min_playback_speed() == 0.4
    prompt = PromptResolver.resolve(prompt_group=SCRIPT, language_variant="fr", library_type=project.library_type)
    assert "[OEUVRE]" not in prompt


@pytest.mark.asyncio
async def test_matching_uses_anime_engine_with_private_index_and_no_cleanup(isolated, monkeypatch):
    from app.api.routes.matching import find_matches, FindMatchesRequest
    from app.api.routes.cleanup import _require_pure
    from app.services.scene_aligner import SceneAlignerService
    from app.services.anime_matcher import MatchProgress
    project = create(isolated)
    ready(project)
    video = Projects.get_project_dir(project.id) / "tiktok.mp4"
    video.write_bytes(b"tiktok")
    project.video_path = str(video)
    Projects.save(project)
    scenes = SceneList(scenes=[Scene(index=0, start_time=0, end_time=1)])
    Projects.save_scenes(project.id, scenes)
    matches = MatchList(matches=[SceneMatch(scene_index=0, episode="episode", start_time=20, end_time=21, speed_ratio=1, confidence=.9)])
    seen = []
    async def align(video_path, found_scenes, library_path, library_type, **kwargs):
        seen.append((library_path, library_type, kwargs))
        yield MatchProgress("complete", 1, "matched", 1, 1, matches)
    monkeypatch.setattr(SceneAlignerService, "align_scenes_progress", align)
    monkeypatch.setattr(SceneAlignerService, "get_last_result", lambda: SimpleNamespace(scenes=scenes, matches=matches))
    response = await find_matches(project.id, FindMatchesRequest())
    _body = [chunk async for chunk in response.body_iterator]
    assert seen == [(Library.get_library_path(project.media_library), project.media_library, {"anime_name": "source", "episode_whitelist": None})]
    assert Projects.load_matches(project.id).matches[0].start_time == 20
    assert Projects.load(project.id).phase == ProjectPhase.MATCH_VALIDATION
    with pytest.raises(HTTPException):
        _require_pure(project.id)


@pytest.mark.asyncio
async def test_import_is_project_owned_nonrecursive_and_never_published(isolated, indexer):
    project = create(isolated)
    messages = []
    async def progress(value, message):
        messages.append(message)
    await Sources.prepare(project.id, progress)
    saved = Projects.load(project.id)
    library = Library.get_library_path(saved.media_library)
    assert Sources.is_ready(saved)
    assert (library / "source" / "episode.mp4").read_bytes() == (isolated / "episode.mp4").read_bytes()
    assert not (library / "source" / "excluded.mp4").exists()
    assert not list(settings.anime_library_path.iterdir())
    assert not list(settings.cache_dir.iterdir())
    assert saved.series_id is None and saved.anime_name is None
    assert len(indexer) == 1
    assert str(library.parent) in indexer[0]
    assert indexer[0][indexer[0].index("--type") + 1] == "anime"
    assert indexer[0][indexer[0].index("--fps") + 1] == "2"
    shutil.rmtree(isolated)
    # A ready project can be reused even after the original folder disappears.
    await Sources.prepare(project.id, progress)
    assert len(indexer) == 1
    assert "Source locale déjà prête." in messages


@pytest.mark.asyncio
async def test_same_episode_names_are_isolated_in_matching_playback_and_export(isolated):
    first, second = create(isolated), create(isolated)
    a, b = ready(first), ready(second)
    for project, expected in [(first, a), (second, b), (first, a)]:
        assert Library.resolve_episode_path("episode", library_type=project.media_library) == expected
        assert GapResolutionService.resolve_episode_path("episode", library_type=project.media_library) == expected
        assert await MatchPlaybackService._resolve_episode_path(project, "episode") == expected
        matches = [SceneMatch(scene_index=0, episode="episode", start_time=0, end_time=1, confidence=1, speed_ratio=1)]
        assert ExportService.resolve_episode_sources(project, matches).sources == [expected]
    assert Library.resolve_episode_path("episode", library_type=LibraryType.PURE) is None
    assert Library.get_preview_proxy_path(a).is_relative_to(Sources.root(first.id))


@pytest.mark.asyncio
async def test_source_failure_does_not_fall_back_to_identity_matching(isolated):
    from app.api.routes.matching import find_matches, FindMatchesRequest, set_sources, SetSourcesRequest
    project = create(isolated)
    with pytest.raises(HTTPException) as exc:
        await find_matches(project.id, FindMatchesRequest())
    assert exc.value.status_code == 409
    with pytest.raises(HTTPException) as exc:
        await set_sources(project.id, SetSourcesRequest(paths=[str(isolated)]))
    assert exc.value.status_code == 400
    ready(project)
    with pytest.raises(HTTPException) as exc:
        await find_matches(project.id, FindMatchesRequest(source_path=str(settings.anime_library_path)))
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_private_source_deferred_download_is_local_only(isolated, monkeypatch):
    from app.api.routes.matching import deferred_download
    from app.services.deferred_download import DeferredDownloadService
    project = create(isolated)
    ready(project)
    Projects.save_matches(project.id, MatchList(matches=[SceneMatch(scene_index=0, episode="episode", start_time=0, end_time=1, confidence=1, speed_ratio=1)]))
    monkeypatch.setattr(DeferredDownloadService, "check_and_download", lambda *a, **kw: pytest.fail("No fallback downloading for a private source"), raising=False)
    response = await deferred_download(project.id)
    body = "".join([chunk async for chunk in response.body_iterator])
    assert '"status":"complete"' in body


def test_duplicate_is_self_contained_after_mother_and_originals_are_deleted(isolated, monkeypatch):
    project = create(isolated)
    source_video = ready(project)
    project.phase = ProjectPhase.SCRIPT_RESTRUCTURE
    Projects.save(project)
    Projects.save_matches(project.id, MatchList(matches=[SceneMatch(scene_index=0, episode="episode", start_time=0, end_time=1, confidence=1, speed_ratio=1)]))
    sidecar = source_video.with_suffix(".atr_source.json")
    sidecar.write_text(json.dumps({"prepared_path": str(source_video)}))
    monkeypatch.setattr("app.services.template_service.TemplateService.get", lambda key: object())
    duplicate = ProjectDuplicationService.duplicate(project.id, [DuplicationVariant(language="en", template="classic")])[0]
    duplicate_video = Library.get_library_path(duplicate.media_library) / "source" / "episode.mp4"
    assert duplicate_video.exists()
    assert duplicate_video.stat().st_ino != source_video.stat().st_ino
    assert duplicate.source_paths == [str(duplicate_video.parent)]
    assert str(source_video) not in duplicate_video.with_suffix(".atr_source.json").read_text()
    Projects.delete(project.id)
    shutil.rmtree(isolated)
    assert Sources.is_ready(duplicate)
    assert Library.resolve_episode_path("episode", library_type=duplicate.media_library) == duplicate_video
    assert ExportService.resolve_episode_sources(duplicate, Projects.load_matches(duplicate.id).matches).sources == [duplicate_video]
    assert ProjectDuplicationService.duplicate(duplicate.id, [DuplicationVariant(language="fr", template="classic")])


@pytest.mark.asyncio
async def test_startup_retry_reuses_download_and_prepared_files(isolated, indexer, monkeypatch):
    service = ProjectStartupService(settings.data_dir / "startup-test.json")
    downloads = []
    original_stream = Library._stream_searcher_index_command
    attempts = 0
    async def fail_once(**kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            yield IndexProgress(status="error", error="index interrupted")
        else:
            async for progress in original_stream(**kwargs):
                yield progress
    monkeypatch.setattr(Library, "_stream_searcher_index_command", fail_once)
    async def download(url, project_id):
        downloads.append(url)
        project = Projects.load(project_id)
        path = Projects.get_project_dir(project_id) / "tiktok.mp4"
        path.write_bytes(b"tiktok")
        project.video_path = str(path)
        Projects.save(project)
        yield DownloadProgress("complete", 1, "downloaded")
    monkeypatch.setattr(DownloaderService, "download_project_video", download)
    monkeypatch.setattr(DownloaderService, "get_video_info", AsyncMock(return_value={"width": 100, "duration": 2}))
    async def detect(project_id):
        project = Projects.load(project_id)
        assert Sources.is_ready(project)
        assert project.cleanup is None
        assert service._jobs[project_id].ready_url is None
        project.phase = ProjectPhase.SCENE_VALIDATION
        Projects.save(project)
        yield SceneDetectionProgress("complete", 1, "scenes ready", [Scene(index=0, start_time=0, end_time=1)])
    monkeypatch.setattr(SceneDetectorService, "detect_project_scenes", detect)
    job = await service.start_project(tiktok_url="https://example.test/tiktok", anime_name=None, series_id=None, library_type="pure", temporary_source=TemporarySourceInput(kind="folder", value=str(isolated)))
    await asyncio.gather(*service._tasks)
    assert job.status == "error" and job.ready_url is None
    copy = Library.get_library_path(Projects.load(job.project_id).media_library) / "source" / "episode.mp4"
    signature = copy.stat().st_ino, copy.stat().st_mtime_ns
    await service.retry_project(job.project_id)
    await asyncio.gather(*service._tasks)
    assert job.status == "complete"
    assert job.ready_url.endswith(("/matches", "/scenes"))
    assert len(downloads) == 1
    assert signature == (copy.stat().st_ino, copy.stat().st_mtime_ns)


@pytest.mark.asyncio
@pytest.mark.parametrize("checkpoint", ["valid", "missing", "invalid_json", "corrupt_shard"])
async def test_retry_discards_uncommitted_files_and_preserves_completed_imports(isolated, indexer, checkpoint):
    project = create(isolated)
    await Sources.prepare(project.id, AsyncMock())
    project = Projects.load(project.id)
    library = Library.get_library_path(project.media_library)
    video = library / "source" / "episode.mp4"
    signature = video.stat().st_ino, video.stat().st_mtime_ns
    (video.parent / "partial.mp4").write_bytes(b"incomplete copy")
    (video.parent / "episode.import.tmp.mp4").write_bytes(b"incomplete transcode")
    marker = library / ".index" / "completed-work"
    marker.write_bytes(b"saved checkpoint")
    if checkpoint == "missing":
        (library / ".index" / "manifest.json").unlink()
    elif checkpoint == "invalid_json":
        (library / ".index" / "manifest.json").write_text('{"interrupted":')
    elif checkpoint == "corrupt_shard":
        project.temporary_source.error = "Shard unreadable. Remove the index and rebuild the index with 'anime-search index'."
    project.temporary_source.status = "error"
    Projects.save(project)
    await Sources.prepare(project.id, AsyncMock())
    assert Sources.is_ready(Projects.load(project.id))
    assert signature == (video.stat().st_ino, video.stat().st_mtime_ns)
    assert sorted(p.name for p in video.parent.glob("*.mp4")) == ["episode.mp4"]
    assert marker.exists() == (checkpoint == "valid")
    assert (isolated / "episode.mp4").exists()


@pytest.mark.asyncio
async def test_delete_waits_for_cancelled_playback_encoder(isolated, monkeypatch):
    from app.api.routes.projects import delete_project
    from app.services.match_playback_service import _ClipPlan
    project = create(isolated)
    video = ready(project)
    plan = _ClipPlan(scene_index=0, track="source", input_path=video,
                     start_time=0, end_time=1, profile="source_fast", clip_id="test", source_key="episode")
    started, finish = threading.Event(), threading.Event()
    monkeypatch.setattr(MatchPlaybackService, "_is_nvenc_available_sync", lambda: False)
    monkeypatch.setattr(MatchPlaybackService, "_is_full_gpu_available_sync", lambda: False)
    def encode(**kwargs):
        started.set()
        finish.wait(timeout=3)
        output = MatchPlaybackService._clip_file(project.id, "test")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"finished clip")
        return 1.0
    monkeypatch.setattr(MatchPlaybackService, "_encode_clip_sync", encode)
    async def prepare(project_id, **kwargs):
        async for result in MatchPlaybackService._run_clip_jobs(project_id, [plan], max_workers=1):
            yield result
    monkeypatch.setattr(MatchPlaybackService, "_prepare_playback_locked", prepare)
    async def consume():
        return [item async for item in MatchPlaybackService.prepare_playback(project.id)]
    playback = asyncio.create_task(consume())
    assert await asyncio.to_thread(started.wait, 3)
    playback.cancel()
    deletion = asyncio.create_task(delete_project(project.id))
    await asyncio.sleep(0.02)
    assert not deletion.done()
    assert Projects.get_project_dir(project.id).exists()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await playback
    await deletion
    assert [p.name for p in Projects.get_project_dir(project.id).iterdir()] == ["project.json"]
    assert Projects.load(project.id).archived_at is not None
    assert not MatchPlaybackService._clip_tasks.get(project.id)


@pytest.mark.asyncio
@pytest.mark.parametrize("managed", [False, True])
async def test_delete_stops_index_writer_before_removing_source(isolated, monkeypatch, managed):
    from app.api.routes.projects import delete_project
    from app.api.routes.project_manager import delete_managed_project
    project = create(isolated)
    ready(project)
    service = ProjectStartupService(settings.data_dir / "startup-test.json")
    monkeypatch.setattr("app.services.project_startup_service.project_startup_queue", service)
    monkeypatch.setattr("app.services.project_upload_service.project_upload_queue.remove_project_jobs", AsyncMock())
    monkeypatch.setattr("app.api.routes.project_manager.UploadPhaseService.managed_delete", lambda project_id, **kw: {"status": "deleted", "local_deleted": Projects.delete(project_id)})
    started = asyncio.Event()
    stopped = []
    async def writer():
        try:
            started.set()
            await asyncio.Event().wait()
        finally:
            # A late writer still owns the directory until cancellation ends.
            (Sources.root(project.id) / "last-write").write_text("done")
            stopped.append(True)
    task = asyncio.create_task(writer())
    service._project_tasks[project.id] = task
    await started.wait()
    await (delete_managed_project(project.id, confirmed=True) if managed else delete_project(project.id))
    assert stopped and task.done()
    assert not Projects.get_project_dir(project.id).exists()
    assert (isolated / "episode.mp4").exists()
    assert Library._manifest_cache_key(project.media_library) not in Library._episode_manifest_cache
    with pytest.raises(RuntimeError):
        Projects.save(project)
    with pytest.raises(FileNotFoundError):
        Library._scan_library_episodes_sync(project.media_library)
    await Library.trigger_preview_proxy_generation(
        Library.get_library_path(project.media_library) / "source" / "episode.mp4"
    )
    assert not Projects.get_project_dir(project.id).exists()


@pytest.mark.asyncio
async def test_cancelled_filesystem_copy_settles_before_cleanup(tmp_path):
    started, finish = threading.Event(), threading.Event()
    def writer():
        started.set()
        finish.wait(timeout=3)
        (tmp_path / "copy.mp4").write_bytes(b"finished")
    task = asyncio.create_task(file_io(writer))
    await asyncio.to_thread(started.wait, 3)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (tmp_path / "copy.mp4").read_bytes() == b"finished"


@pytest.mark.asyncio
async def test_playlist_is_rejected_before_any_download(isolated, monkeypatch):
    result = SimpleNamespace(returncode=0, stdout=b'{"_type":"playlist","entries":[]}', stderr=b'')
    run = AsyncMock(return_value=result)
    monkeypatch.setattr("app.services.temporary_source_service.run_command", run)
    with pytest.raises(ValueError, match="seule vidéo"):
        await Sources._validate_single_video_url("https://example.test/playlist", isolated / "out.mp4")
    command = run.call_args.args[0]
    assert "--no-playlist" in command and "--skip-download" in command


@pytest.mark.asyncio
async def test_source_url_download_is_distinct_from_tiktok_and_resumable(isolated, indexer, monkeypatch):
    project = Projects.create(tiktok_url="https://example.test/tiktok", library_type=LibraryType.PURE,
                             temporary_source=TemporarySourceInput(kind="url", value="https://example.test/source"))
    calls = []
    monkeypatch.setattr(Sources, "_validate_single_video_url", AsyncMock(return_value=False))
    monkeypatch.setattr(DownloaderService, "get_video_info", AsyncMock(return_value={"width": 1280, "duration": 2}))
    async def download(url, project_id, **kwargs):
        calls.append((url, kwargs))
        path = kwargs["output_path"]
        path.parent.mkdir(parents=True)
        path.write_bytes(b"original URL media")
        yield DownloadProgress("complete", 1, "downloaded")
    monkeypatch.setattr(DownloaderService, "download", download)
    await Sources.prepare(project.id, AsyncMock())
    saved = Projects.load(project.id)
    assert saved.video_path is None and saved.tiktok_url == project.tiktok_url
    assert calls[0][0] == "https://example.test/source"
    assert calls[0][1]["format_selector"] == DownloaderService.SOURCE_FORMAT_SELECTOR
    assert calls[0][1]["use_browser_cookies"] is False
    assert calls[0][1]["output_path"].is_relative_to(Sources.root(project.id))
    saved.temporary_source.status = "error"
    Projects.save(saved)
    await Sources.prepare(project.id, AsyncMock())
    assert len(calls) == 1
    assert Sources.is_ready(Projects.load(project.id))


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [b'{"_type":"video"}', b'{"_type":"playlist","entries":[]}'])
async def test_source_url_probe_is_anonymous_from_first_request(isolated, monkeypatch, response):
    monkeypatch.setattr(settings, "ytdlp_cookies_from_browser", "firefox")
    run = AsyncMock(return_value=SimpleNamespace(returncode=0, stdout=response, stderr=b""))
    monkeypatch.setattr("app.services.temporary_source_service.run_command", run)
    probe = Sources._validate_single_video_url("https://www.youtube.com/watch?v=video", isolated / "output.mp4")
    if b"playlist" in response:
        with pytest.raises(ValueError, match="seule vidéo"):
            await probe
    else:
        assert await probe is False
    run.assert_awaited_once()
    command = run.call_args.args[0]
    assert "--cookies-from-browser" not in command
    assert "--no-cookies" in command and "--no-cookies-from-browser" in command
    assert "--ignore-config" in command
    assert command[command.index("-f") + 1] == DownloaderService.SOURCE_FORMAT_SELECTOR
    assert settings.ytdlp_cookies_from_browser == "firefox"
