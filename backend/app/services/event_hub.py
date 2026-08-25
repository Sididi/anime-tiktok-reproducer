"""Process-wide fan-out hub behind the browser's single live-update stream.

Every tab of the web UI shares ONE ``GET /api/events/stream`` connection (a
SharedWorker owns it and relays frames to the tabs).  Job registries publish
their full job state here instead of keeping their own subscriber lists.

Frames (each is one ``data:`` SSE line, never carrying a top-level ``status``
so the frontend's generic reader cannot mistake a job error for a stream
error):

* ``{"kind": "hello", "server_id": ..., "topics": [...]}`` — first frame; the
  ``server_id`` changes on every backend restart so clients can drop caches.
* ``{"kind": "snapshot", "topic": T, "items": [{key, project_id, data}, ...]}``
  — one per registered topic right after ``hello`` (and again after a
  resync); replaces the client's view of that topic.
* ``{"kind": "event", "topic": T, "key": K, "project_id": P|null, "data": {...}}``
  — a live update; ``data`` is always the job's complete current state.

Delivery is *coalescing*: a subscriber that has not yet read an update for
``(topic, key)`` only keeps the latest one.  Because every event is a full
state, dropping stale intermediates is lossless for anything that keys off
the current/terminal state — a slow client may see ``queued`` → ``complete``
without ``running``, and every consumer is written for that.  Memory per
subscriber is therefore bounded by the number of distinct jobs, with a hard
cap that triggers a fresh snapshot instead of unbounded growth.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any, AsyncIterator, Callable, Iterable, TypedDict

logger = logging.getLogger("uvicorn.error")


class HubItem(TypedDict):
    key: str
    project_id: str | None
    data: dict[str, Any]


SnapshotFn = Callable[[], Iterable[HubItem]]

KEEPALIVE_SECONDS = 15.0
MAX_PENDING_PER_SUBSCRIBER = 4096


class HubSubscription:
    """One connected client: a coalescing buffer plus a wake-up signal."""

    __slots__ = ("pending", "wake", "needs_resync")

    def __init__(self) -> None:
        self.pending: dict[tuple[str, str], dict[str, Any]] = {}
        self.wake = asyncio.Event()
        self.needs_resync = False

    def drain(self) -> list[dict[str, Any]]:
        """Take every pending frame (insertion order) and clear the buffer."""
        frames = list(self.pending.values())
        self.pending.clear()
        self.wake.clear()
        return frames


class EventHub:
    def __init__(self, *, keepalive_seconds: float = KEEPALIVE_SECONDS) -> None:
        self.server_id = uuid.uuid4().hex
        self.keepalive_seconds = keepalive_seconds
        self._topics: dict[str, SnapshotFn] = {}
        self._subscribers: set[HubSubscription] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._published = 0
        self._coalesced = 0
        self._resyncs = 0
        self._dropped_off_loop = 0

    # ------------------------------------------------------------------
    # registry

    def register_topic(self, topic: str, snapshot: SnapshotFn) -> None:
        """Register (or replace) how a topic enumerates its current items."""
        self._topics[topic] = snapshot

    def topics(self) -> list[str]:
        return list(self._topics)

    # ------------------------------------------------------------------
    # publishing

    def publish(
        self,
        topic: str,
        *,
        key: str,
        data: dict[str, Any],
        project_id: str | None = None,
    ) -> None:
        """Fan a full job state out to every subscriber.

        Meant to be called on the event-loop thread (every registry already
        marshals worker-thread progress there).  A call from another thread
        is re-scheduled onto the loop the hub last served on, or dropped with
        a warning if the hub never ran — it never raises into a job.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is None:
            known = self._loop
            if known is None or known.is_closed():
                self._dropped_off_loop += 1
                logger.warning(
                    "event_hub.publish(%s/%s) called off-loop before any loop was known; dropped",
                    topic,
                    key,
                )
                return
            known.call_soon_threadsafe(
                self._publish_on_loop, topic, key, data, project_id
            )
            return
        self._loop = loop
        self._publish_on_loop(topic, key, data, project_id)

    def _publish_on_loop(
        self, topic: str, key: str, data: dict[str, Any], project_id: str | None
    ) -> None:
        frame = {
            "kind": "event",
            "topic": topic,
            "key": key,
            "project_id": project_id,
            "data": data,
        }
        self._published += 1
        slot = (topic, key)
        for sub in tuple(self._subscribers):
            if slot in sub.pending:
                self._coalesced += 1
            sub.pending[slot] = frame
            if len(sub.pending) > MAX_PENDING_PER_SUBSCRIBER:
                sub.pending.clear()
                sub.needs_resync = True
                self._resyncs += 1
            sub.wake.set()

    # ------------------------------------------------------------------
    # subscribing

    def subscribe(self) -> HubSubscription:
        self._loop = asyncio.get_running_loop()
        sub = HubSubscription()
        self._subscribers.add(sub)
        return sub

    def unsubscribe(self, sub: HubSubscription) -> None:
        self._subscribers.discard(sub)

    def hello_frame(self) -> dict[str, Any]:
        return {"kind": "hello", "server_id": self.server_id, "topics": self.topics()}

    def snapshot_frame(self, topic: str) -> dict[str, Any]:
        try:
            items = [dict(item) for item in self._topics[topic]()]
        except Exception:
            # A registry mid-mutation must not stall the whole stream; the
            # next event for that topic repairs the client's view.
            logger.exception("event_hub snapshot for topic %s failed", topic)
            items = []
        return {"kind": "snapshot", "topic": topic, "items": items}

    @staticmethod
    def format_frame(frame: dict[str, Any]) -> str:
        return "data: " + json.dumps(frame) + "\n\n"

    async def stream(self) -> AsyncIterator[str]:
        """SSE body: hello, one snapshot per topic, then coalesced live events.

        Emits a ``: ping`` comment after ``keepalive_seconds`` of silence so
        the server notices dead clients (the write fails) and intermediaries
        never see an idle connection.
        """
        sub = self.subscribe()
        try:
            yield self.format_frame(self.hello_frame())
            for topic in self.topics():
                yield self.format_frame(self.snapshot_frame(topic))
            while True:
                try:
                    await asyncio.wait_for(sub.wake.wait(), timeout=self.keepalive_seconds)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                if sub.needs_resync:
                    sub.needs_resync = False
                    sub.drain()
                    for topic in self.topics():
                        yield self.format_frame(self.snapshot_frame(topic))
                    continue
                for frame in sub.drain():
                    yield self.format_frame(frame)
        finally:
            self.unsubscribe(sub)

    # ------------------------------------------------------------------
    # introspection

    def stats(self) -> dict[str, Any]:
        pending_by_topic: dict[str, int] = {topic: 0 for topic in self._topics}
        for sub in self._subscribers:
            for topic, _key in sub.pending:
                pending_by_topic[topic] = pending_by_topic.get(topic, 0) + 1
        return {
            "server_id": self.server_id,
            "subscribers": len(self._subscribers),
            "topics": pending_by_topic,
            "published": self._published,
            "coalesced": self._coalesced,
            "resyncs": self._resyncs,
            "dropped_off_loop": self._dropped_off_loop,
        }


event_hub = EventHub()
