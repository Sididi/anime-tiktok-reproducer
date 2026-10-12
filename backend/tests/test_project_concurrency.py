import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.library_types import LibraryType
from app.models import Scene
from app.services.downloader import DownloaderService, DownloadProgress
from app.services.project_service import ProjectService
from app.services.project_startup_service import ProjectStartupService
from app.services.scene_detector import SceneDetectorService
from app.services.indexation_queue import IndexationQueueService
from app.services.match_playback_service import MatchPlaybackService


@pytest.mark.asyncio
async def test_twenty_startups_overlap_stages_with_bounded_download_and_cpu_work(tmp_path, monkeypatch):
    rows = {f"p{i}": SimpleNamespace(id=f"p{i}", tiktok_url=f"https://example.test/{i}",
            anime_name="Test", series_id=None, library_type=LibraryType.ANIME,
            temporary_source=None, uses_identity_source=False) for i in range(20)}
    async def load(pid):
        return rows.get(pid)
    monkeypatch.setattr(ProjectService, "aload", load)
    monkeypatch.setattr(DownloaderService, "_project_download_slots", asyncio.Semaphore(3))
    monkeypatch.setattr(SceneDetectorService, "_cpu_slots", asyncio.Semaphore(2))
    download_gate, three_downloads, two_cpu = asyncio.Event(), asyncio.Event(), asyncio.Event()
    cpu_gate = threading.Event()
    loop = asyncio.get_running_loop()
    counts = {"download": 0, "download_peak": 0, "cpu": 0, "cpu_peak": 0}
    cpu_lock = threading.Lock()

    async def download(cls, url, pid):
        counts["download"] += 1
        counts["download_peak"] = max(counts["download_peak"], counts["download"])
        if counts["download"] == 3:
            three_downloads.set()
        try:
            await download_gate.wait()
            yield DownloadProgress("complete", 1, "Downloaded")
        finally:
            counts["download"] -= 1

    def detect(*args):
        with cpu_lock:
            counts["cpu"] += 1
            counts["cpu_peak"] = max(counts["cpu_peak"], counts["cpu"])
            if counts["cpu"] == 2:
                loop.call_soon_threadsafe(two_cpu.set)
        try:
            assert cpu_gate.wait(10)
            return [Scene(index=0, start_time=0, end_time=1)]
        finally:
            with cpu_lock:
                counts["cpu"] -= 1

    async def scenes(cls, pid):
        async for progress in cls.detect_scenes(Path(pid)):
            yield progress

    monkeypatch.setattr(DownloaderService, "_download_project_video", classmethod(download))
    monkeypatch.setattr(SceneDetectorService, "_detect_sync", staticmethod(detect))
    monkeypatch.setattr(SceneDetectorService, "detect_project_scenes", classmethod(scenes))
    service = ProjectStartupService(tmp_path / "startups.json")
    try:
        await asyncio.gather(*(service.enqueue_project(pid) for pid in rows))
        await asyncio.wait_for(three_downloads.wait(), 2)
        assert sum(job.status == "running" for job in service.list_jobs()) == 4
        assert counts["download"] == 3
        download_gate.set()
        await asyncio.wait_for(two_cpu.wait(), 2)
        assert counts["cpu"] == 2
        cpu_gate.set()
        await asyncio.wait_for(asyncio.gather(*list(service._tasks)), 5)
        assert all(job.status == "complete" for job in service.list_jobs())
        assert counts["download_peak"] == 3 and counts["cpu_peak"] == 2
        assert len(ProjectStartupService(service._jobs_path).list_jobs()) == 20
    finally:
        cpu_gate.set()
        download_gate.set()
        await service.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["scene_detection", "playback"])
async def test_gpu_assisted_preparation_cannot_bypass_transcription_budget(stage, monkeypatch):
    from app.services import indexation_queue as queue_module
    queue = IndexationQueueService()
    monkeypatch.setattr(queue_module, "indexation_queue", queue)
    monkeypatch.setattr(SceneDetectorService, "_cpu_slots", asyncio.Semaphore(2))
    entered = []
    if stage == "scene_detection":
        def detect(*args):
            entered.append(queue.available_heavy_slots())
            return [Scene(index=0, start_time=0, end_time=1)]
        monkeypatch.setattr(SceneDetectorService, "_detect_sync", staticmethod(detect))
        stream = SceneDetectorService.detect_scenes(Path("video"), library_path=Path("library"))
    else:
        async def clips(cls, *args, **kwargs):
            entered.append(queue.available_heavy_slots())
            yield object()
        monkeypatch.setattr(MatchPlaybackService, "_run_clip_jobs_impl", classmethod(clips))
        stream = MatchPlaybackService._run_clip_jobs("p", [object()], max_workers=8)
    async def consume():
        async for _ in stream:
            pass
    async with queue.heavy_slot("transcription_diarization"):
        task = asyncio.create_task(consume())
        await asyncio.sleep(0.01)
        assert entered == []
    await asyncio.wait_for(task, 2)
    assert entered == [0]
    assert queue.available_heavy_slots() == 2
