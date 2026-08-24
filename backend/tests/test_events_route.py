"""/api/events/* through the ASGI app: stats shape, topic registration, stream opening."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api.routes import events as events_route
from app.main import app
from app.services.event_hub import event_hub

EXPECTED_TOPICS = {"startup_jobs", "upload_jobs", "index_jobs", "zoom_jobs"}


@pytest.fixture
def client():
    # No lifespan: the events routes need nothing from startup.
    return TestClient(app)


def test_stats_shape_and_topic_registration(client):
    response = client.get("/api/events/stats")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "server_id",
        "subscribers",
        "topics",
        "published",
        "coalesced",
        "resyncs",
        "dropped_off_loop",
    }
    assert set(body["topics"]) == EXPECTED_TOPICS
    assert set(event_hub.topics()) == EXPECTED_TOPICS
    assert body["server_id"] == event_hub.server_id


@pytest.mark.asyncio
async def test_stream_opens_with_hello_and_all_snapshots(monkeypatch):
    # Starlette's TestClient runs the ASGI app to completion before it hands
    # back a response, so an endless SSE body can never be read through it;
    # drive the endpoint's StreamingResponse directly instead.
    monkeypatch.setattr(event_hub, "keepalive_seconds", 0.05)
    response = await events_route.stream_events()
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"

    frames: list[dict] = []
    body = response.body_iterator
    try:
        async for chunk in body:
            text = chunk if isinstance(chunk, str) else bytes(chunk).decode()
            if not text.startswith("data: "):
                continue
            frames.append(json.loads(text[len("data: ") :].strip()))
            if len(frames) == 1 + len(EXPECTED_TOPICS):
                break
    finally:
        await body.aclose()
    assert event_hub.stats()["subscribers"] == 0
    assert frames[0]["kind"] == "hello"
    assert set(frames[0]["topics"]) == EXPECTED_TOPICS
    assert [f["kind"] for f in frames[1:]] == ["snapshot"] * len(EXPECTED_TOPICS)
    assert {f["topic"] for f in frames[1:]} == EXPECTED_TOPICS
    for frame in frames[1:]:
        for item in frame["items"]:
            assert set(item) == {"key", "project_id", "data"}


def test_topic_snapshots_use_registry_keys(monkeypatch):
    events_route.ensure_topics_registered()
    startup = event_hub.snapshot_frame("startup_jobs")
    upload = event_hub.snapshot_frame("upload_jobs")
    index = event_hub.snapshot_frame("index_jobs")
    zoom = event_hub.snapshot_frame("zoom_jobs")
    for item in startup["items"] + upload["items"]:
        assert item["key"] == item["project_id"] == item["data"]["project_id"]
    for item in index["items"]:
        assert item["key"] == item["data"]["id"] and item["project_id"] is None
    for item in zoom["items"]:
        assert item["key"] == item["data"]["id"]
        assert item["project_id"] == item["data"]["project_id"]
