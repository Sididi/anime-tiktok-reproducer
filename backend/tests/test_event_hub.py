"""Wire contract + delivery semantics of the shared event hub."""

from __future__ import annotations

import asyncio
import json
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.event_hub import EventHub, MAX_PENDING_PER_SUBSCRIBER

EVENT_FRAME_KEYS = {"kind", "topic", "key", "project_id", "data"}


def _parse(frame: str) -> dict:
    assert frame.startswith("data: ") and frame.endswith("\n\n"), frame
    return json.loads(frame[len("data: ") : -2])


async def _next(gen) -> str:
    return await asyncio.wait_for(gen.__anext__(), 1.0)


def _item(key: str, **data) -> dict:
    return {"key": key, "project_id": "proj", "data": data}


@pytest.mark.asyncio
async def test_stream_opens_with_hello_then_one_snapshot_per_topic_in_order():
    hub = EventHub()
    hub.register_topic("a", lambda: [_item("1", status="error", id="1")])
    hub.register_topic("b", lambda: [])

    gen = hub.stream()
    try:
        assert _parse(await _next(gen)) == {
            "kind": "hello",
            "server_id": hub.server_id,
            "topics": ["a", "b"],
        }
        snapshot_a = _parse(await _next(gen))
        assert snapshot_a == {
            "kind": "snapshot",
            "topic": "a",
            "items": [{"key": "1", "project_id": "proj", "data": {"status": "error", "id": "1"}}],
        }
        # A job in the error state never surfaces as a top-level status.
        assert "status" not in snapshot_a
        assert _parse(await _next(gen)) == {"kind": "snapshot", "topic": "b", "items": []}
        assert hub.stats()["subscribers"] == 1
    finally:
        await gen.aclose()
    assert hub.stats()["subscribers"] == 0


@pytest.mark.asyncio
async def test_event_frame_contract():
    hub = EventHub()
    hub.register_topic("t", lambda: [])
    gen = hub.stream()
    try:
        await _next(gen)
        await _next(gen)
        hub.publish("t", key="k", data={"status": "error", "error": "boom"}, project_id="p")
        frame = _parse(await _next(gen))
        assert set(frame) == EVENT_FRAME_KEYS
        assert frame == {
            "kind": "event",
            "topic": "t",
            "key": "k",
            "project_id": "p",
            "data": {"status": "error", "error": "boom"},
        }
        hub.publish("t", key="k2", data={})
        assert _parse(await _next(gen))["project_id"] is None
    finally:
        await gen.aclose()


@pytest.mark.asyncio
async def test_unread_updates_coalesce_per_key_and_keep_other_keys():
    hub = EventHub()
    hub.register_topic("t", lambda: [])
    sub = hub.subscribe()
    try:
        hub.publish("t", key="a", data={"n": 1})
        hub.publish("t", key="b", data={"n": 1})
        hub.publish("t", key="a", data={"n": 2})
        hub.publish("t", key="a", data={"n": 3})
        frames = sub.drain()
        assert [(f["key"], f["data"]["n"]) for f in frames] == [("a", 3), ("b", 1)]
        assert sub.drain() == []
        stats = hub.stats()
        assert stats["published"] == 4
        assert stats["coalesced"] == 2
    finally:
        hub.unsubscribe(sub)
        hub.unsubscribe(sub)  # a second removal is a no-op, never an error
    assert hub.stats()["subscribers"] == 0


@pytest.mark.asyncio
async def test_idle_stream_emits_keepalive_comment():
    hub = EventHub(keepalive_seconds=0.05)
    hub.register_topic("t", lambda: [])
    gen = hub.stream()
    try:
        await _next(gen)
        await _next(gen)
        assert await _next(gen) == ": ping\n\n"
        # Still delivering after a ping.
        hub.publish("t", key="k", data={})
        assert _parse(await _next(gen))["kind"] == "event"
    finally:
        await gen.aclose()


@pytest.mark.asyncio
async def test_pending_cap_triggers_fresh_snapshots_instead_of_growth():
    hub = EventHub()
    hub.register_topic("t", lambda: [_item("x")])
    gen = hub.stream()
    try:
        await _next(gen)
        await _next(gen)
        for i in range(MAX_PENDING_PER_SUBSCRIBER + 1):
            hub.publish("t", key=str(i), data={})
        frame = _parse(await _next(gen))
        assert frame["kind"] == "snapshot" and frame["topic"] == "t"
        assert hub.stats()["resyncs"] == 1
        # The overflowed backlog was dropped, not replayed.
        hub.publish("t", key="after", data={})
        assert _parse(await _next(gen))["key"] == "after"
    finally:
        await gen.aclose()


@pytest.mark.asyncio
async def test_failing_snapshot_provider_yields_empty_snapshot():
    hub = EventHub()

    def boom():
        raise RuntimeError("registry mid-mutation")

    hub.register_topic("t", boom)
    gen = hub.stream()
    try:
        await _next(gen)
        assert _parse(await _next(gen)) == {"kind": "snapshot", "topic": "t", "items": []}
    finally:
        await gen.aclose()


@pytest.mark.asyncio
async def test_publish_from_worker_thread_is_marshalled_onto_the_loop():
    hub = EventHub()
    hub.register_topic("t", lambda: [])
    sub = hub.subscribe()
    try:
        worker = threading.Thread(
            target=lambda: hub.publish("t", key="k", data={"n": 1}, project_id="p")
        )
        worker.start()
        worker.join()
        await asyncio.wait_for(sub.wake.wait(), 1.0)
        assert [f["key"] for f in sub.drain()] == ["k"]
        assert hub.stats()["dropped_off_loop"] == 0
    finally:
        hub.unsubscribe(sub)


def test_publish_off_loop_before_any_loop_is_dropped_not_raised():
    hub = EventHub()
    hub.publish("t", key="k", data={})
    assert hub.stats()["dropped_off_loop"] == 1
