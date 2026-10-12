import asyncio
import json
from pathlib import Path
import sys

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.api.routes import transcription
from app.services import progress_jobs as module
from app.services.event_hub import EventHub
from app.services.indexation_queue import IndexationQueueService
from app.services.progress_jobs import ProgressJob, ProgressJobs


@pytest.fixture
def service(tmp_path, monkeypatch):
    service = ProgressJobs(tmp_path / "jobs.json")
    monkeypatch.setattr(module, "progress_jobs", service)
    monkeypatch.setattr(module, "event_hub", EventHub())
    return service


def frame(status, **kwargs):
    return "data: " + json.dumps({"status": status, **kwargs}) + "\n\n"


@pytest.mark.asyncio
async def test_twenty_posts_acknowledge_before_work_finishes_and_share_gpu_budget(service, monkeypatch):
    queue = IndexationQueueService()
    gate = asyncio.Event()
    started = asyncio.Event()
    active = peak = finished = 0

    async def load(_):
        return object()

    async def work(project_id, request):
        async def body():
            nonlocal active, peak, finished
            async with queue.heavy_slot("transcription_diarization"):
                active += 1
                peak = max(peak, active)
                if active == 2:
                    started.set()
                try:
                    yield frame("transcribing", project_id=project_id)
                    await gate.wait()
                    yield frame("complete", project_id=project_id)
                    # Consumers must see completion only after this finalizer.
                    finished += 1
                finally:
                    active -= 1
        return StreamingResponse(body())

    monkeypatch.setattr(transcription.ProjectService, "aload", load)
    monkeypatch.setattr(transcription, "_start_transcription_stream", work)
    app = FastAPI()
    app.include_router(transcription.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        responses = await asyncio.wait_for(asyncio.gather(*[
            client.post(f"/projects/p{i}/transcription/start", json={"language": "auto"},
                        headers={"Prefer": "respond-async"}) for i in range(20)
        ]), 5)
        await asyncio.wait_for(started.wait(), 2)
        assert all(r.status_code == 202 and r.headers["content-type"] == "application/json" for r in responses)
        assert len(service.tasks) == 20 and finished == 0 and peak == 2
        # A second tab observing the same job does not launch another worker.
        again = await client.post("/projects/p0/transcription/start", json={"language": "auto"},
                                  headers={"Prefer": "respond-async"})
        assert again.json()["id"] == responses[0].json()["id"]
    # Closing the HTTP client has no effect on server-owned jobs.
    assert len(service.tasks) == 20
    gate.set()
    await asyncio.gather(*list(service.tasks.values()))
    assert finished == 20 and peak == 2 and active == 0
    assert all(job.status == "complete" for job in service.jobs.values())
    assert queue.available_heavy_slots() == 2
    assert len(ProgressJobs(service.path).jobs) == 20


@pytest.mark.asyncio
async def test_completion_waits_for_post_yield_work_and_disconnect_does_not_cancel(service):
    gate = asyncio.Event()
    final_yielded = asyncio.Event()
    async def work():
        async def body():
            yield frame("complete", result="saved")
            final_yielded.set()
            await gate.wait()
        return StreamingResponse(body())
    job = await service.start("p", "matching", {}, work)
    await final_yielded.wait()
    assert job.status == "running"
    assert job.event.get("status") != "complete"
    gate.set()
    await service.tasks[job.id]
    assert job.status == "complete" and "result" not in job.event
    assert (await service.result(job))["event"]["result"] == "saved"
    reloaded = ProgressJobs(service.path)
    assert (await reloaded.result(reloaded.jobs[job.id]))["event"]["result"] == "saved"


@pytest.mark.asyncio
async def test_errors_parameter_conflict_restart_and_history_eviction(service, monkeypatch):
    gate = asyncio.Event()
    async def failure():
        await gate.wait()
        raise HTTPException(409, "Invalid source")
    job = await service.start("p", "matching", {"episodes": [1]}, failure)
    with pytest.raises(HTTPException, match="different parameters"):
        await service.start("p", "matching", {"episodes": [2]}, failure)
    gate.set()
    await service.tasks[job.id]
    assert job.status == "error" and job.event["error"] == "Invalid source"
    interrupted = ProgressJob(project_id="p", kind="transcription", status="running")
    service.jobs[interrupted.id] = interrupted
    await service._persist()
    restarted = ProgressJobs(service.path)
    async def load(_):
        return object()
    monkeypatch.setattr("app.services.project_service.ProjectService.aload", load)
    restarted.MAX_TERMINAL_JOBS = 1
    await restarted.startup_cleanup()
    assert list(restarted.jobs) == [interrupted.id]
    assert "redémarrage" in restarted.jobs[interrupted.id].event["error"]


@pytest.mark.asyncio
async def test_delete_waits_for_native_finalizer_before_removing_job(service):
    started, native_done = asyncio.Event(), asyncio.Event()
    async def work():
        async def body():
            try:
                started.set()
                await asyncio.Event().wait()
                yield frame("complete")
            finally:
                await native_done.wait()
        return StreamingResponse(body())
    job = await service.start("p", "transcription", {}, work)
    await started.wait()
    deletion = asyncio.create_task(service.remove_project("p"))
    await asyncio.sleep(0)
    assert not deletion.done()
    second_cancel = asyncio.create_task(service.cancel_project("p"))
    await asyncio.sleep(0)
    assert not second_cancel.done()
    native_done.set()
    await asyncio.wait_for(deletion, 2)
    await asyncio.wait_for(second_cancel, 2)
    assert job.id not in service.jobs and not service.tasks
