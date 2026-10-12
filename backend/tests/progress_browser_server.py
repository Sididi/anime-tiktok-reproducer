"""Isolated browser-test backend: real job/hub lifecycle, deterministic fake work.

PYTHONPATH=backend .pixi/envs/dev/bin/python -m uvicorn \
  progress_browser_server:app --app-dir backend/tests --port 8123
Then run Vite with ATR_BACKEND_ORIGIN=http://127.0.0.1:8123 on port 5174.
"""
import asyncio
import json
import tempfile
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import StreamingResponse

from app.api.routes import transcription, progress, events
from app.services import progress_jobs as jobs_module
from app.services.progress_jobs import ProgressJobs
from app.services.event_hub import event_hub
from app.services.indexation_queue import IndexationQueueService

directory = tempfile.TemporaryDirectory(prefix="atr-progress-browser-")
jobs = ProgressJobs(Path(directory.name) / "jobs.json")
jobs_module.progress_jobs = jobs
progress.progress_jobs = jobs
queue = IndexationQueueService()
gate = asyncio.Event()
stats = {"active": 0, "peak": 0, "finished": 0, "started": 0}


async def load(project_id):
    return object()


async def work(project_id, request):
    async def body():
        async with queue.heavy_slot("transcription_diarization"):
            stats["active"] += 1
            stats["started"] += 1
            stats["peak"] = max(stats["peak"], stats["active"])
            try:
                yield 'data: {"status":"transcribing","progress":0.2,"message":"Transcribing test audio…"}\n\n'
                await gate.wait()
                await asyncio.sleep(0.04)
                yield "data: " + json.dumps({"status": "complete", "project_id": project_id,
                    "transcription": {"language": "en", "scenes": [{"scene_index": 0,
                        "text": "Example transcript", "words": [], "is_raw": False}]}}) + "\n\n"
                stats["finished"] += 1
            finally:
                stats["active"] -= 1
    return StreamingResponse(body(), media_type="text/event-stream")


transcription.ProjectService.aload = load
transcription._start_transcription_stream = work
# No unrelated production registries or application startup side effects.
events._topics_registered = True
event_hub.register_topic("progress_jobs", lambda: [
    {"key": job.id, "project_id": job.project_id, "data": job.model_dump(mode="json")}
    for job in jobs.list_jobs()
])
app = FastAPI()
app.include_router(transcription.router, prefix="/api")
app.include_router(progress.router, prefix="/api")
app.include_router(events.router, prefix="/api")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/api/test/stats")
async def test_stats():
    return {**stats, "jobs": len(jobs.jobs), "subscribers": event_hub.stats()["subscribers"]}


@app.post("/api/test/release")
async def release():
    gate.set()
    return {"ok": True}


@app.post("/api/test/reset")
async def reset():
    await jobs.shutdown()
    jobs.jobs.clear()
    gate.clear()
    stats.update(active=0, peak=0, finished=0, started=0)
    return {"ok": True}
