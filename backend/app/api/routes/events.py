"""The browser's single live-update stream (see ``services/event_hub.py``).

Replaces the four always-on per-topic SSE endpoints (startup jobs, upload
jobs, indexation jobs, zoom-search jobs): every tab of the UI shares one
connection to ``/events/stream`` through a SharedWorker, which keeps the
browser far below its 6-sockets-per-host limit.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from ...services.event_hub import HubItem, event_hub

router = APIRouter(prefix="/events", tags=["events"])

TOPIC_STARTUP_JOBS = "startup_jobs"
TOPIC_UPLOAD_JOBS = "upload_jobs"
TOPIC_INDEX_JOBS = "index_jobs"
TOPIC_ZOOM_JOBS = "zoom_jobs"

_topics_registered = False


def _startup_jobs_snapshot() -> list[HubItem]:
    from ...services.project_startup_service import project_startup_queue

    return [
        {
            "key": job.project_id,
            "project_id": job.project_id,
            "data": job.model_dump(mode="json"),
        }
        for job in project_startup_queue.list_jobs()
    ]


def _upload_jobs_snapshot() -> list[HubItem]:
    from ...services.project_upload_service import project_upload_queue

    return [
        {
            "key": job.project_id,
            "project_id": job.project_id,
            "data": job.model_dump(mode="json"),
        }
        for job in project_upload_queue.list_jobs()
    ]


def _index_jobs_snapshot() -> list[HubItem]:
    from ...services.indexation_queue import indexation_queue

    return [
        {"key": job.id, "project_id": None, "data": job.model_dump(mode="json")}
        for job in indexation_queue.list_jobs()
    ]


def _zoom_jobs_snapshot() -> list[HubItem]:
    from ...services.zoom_search_service import zoom_search_service

    return [
        {
            "key": job.id,
            "project_id": job.project_id,
            "data": job.model_dump(mode="json"),
        }
        for job in zoom_search_service.list_all_jobs()
    ]


def ensure_topics_registered() -> None:
    """Bind the four job registries to their hub topics (idempotent).

    Done lazily here rather than in the services' constructors so tests that
    build extra service instances never hijack the global snapshot provider,
    and so importing this router does not pull the heavy indexation stack.
    """
    global _topics_registered
    if _topics_registered:
        return
    event_hub.register_topic(TOPIC_STARTUP_JOBS, _startup_jobs_snapshot)
    event_hub.register_topic(TOPIC_UPLOAD_JOBS, _upload_jobs_snapshot)
    event_hub.register_topic(TOPIC_INDEX_JOBS, _index_jobs_snapshot)
    event_hub.register_topic(TOPIC_ZOOM_JOBS, _zoom_jobs_snapshot)
    _topics_registered = True


@router.get("/stream")
async def stream_events():
    """One SSE stream carrying every job registry (hello, snapshots, events)."""
    ensure_topics_registered()
    return StreamingResponse(
        event_hub.stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/stats")
async def event_stats():
    """Subscriber count and delivery counters (for ops checks and e2e tests)."""
    ensure_topics_registered()
    return event_hub.stats()
