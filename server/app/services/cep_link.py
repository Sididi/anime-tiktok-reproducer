"""Premiere Link hub: the WebSocket broker between the main backend and the
Premiere Pro CEP panel.

Both peers connect *outbound* to this VPS: the backend POSTs launch requests
to /api/internal/cep/launches, the panel keeps a WebSocket open on
/api/cep/ws. The hub replays undelivered launches on (re)connect, pushes new
ones live, records the panel's ack, and edits the project's Discord message
with the outcome so nothing is ever silent.

Protocol (JSON text frames):
  panel -> hub : {"type":"auth","token","panel_build_id","port"}   (first frame)
                 {"type":"pong","ts"} / {"type":"ping","ts"}
                 {"type":"ack","launch_id","project_id","result",
                  "detail","status","queue_state","batch_phase"}
  hub -> panel : {"type":"auth_ok","server_time","pending_count","heartbeat_interval_s"}
                 {"type":"launch","launch_id","project_id","anime_title","requested_at","replay"}
                 {"type":"ping","ts"} / {"type":"pong","ts"}
                 {"type":"error","code","detail"}
"""
from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from app.config import Settings
from app.models.cep_launch import ACK_RESULTS, CepLaunch
from app.services.cep_launch_store import CepLaunchStore

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = 1
HEARTBEAT_INTERVAL_S = 25.0  # stays under nginx's default 60 s idle timeout
AUTH_TIMEOUT_S = 5.0
CLOSE_AUTH_FAILED = 4401
CLOSE_PROTOCOL = 4400
CLOSE_HEARTBEAT = 4408
CLOSE_SHUTDOWN = 1012

_OUTCOME_PREFIX = "Premiere: "


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _local_hhmm(value: datetime) -> str:
    try:
        from zoneinfo import ZoneInfo  # noqa: PLC0415

        return value.astimezone(ZoneInfo("Europe/Paris")).strftime("%H:%M")
    except Exception:  # tzdata missing on a slim image — fall back to UTC
        return value.astimezone(UTC).strftime("%H:%M UTC")


def outcome_line(launch: CepLaunch) -> str:
    if launch.status == "pending":
        return f"{_OUTCOME_PREFIX}⏳ waiting for the panel"
    if launch.status == "accepted":
        stamp = _local_hhmm(launch.acked_at or launch.updated_at)
        return f"{_OUTCOME_PREFIX}✅ accepted {stamp}"
    if launch.status == "duplicate":
        return f"{_OUTCOME_PREFIX}⚠️ duplicate (already run this session)"
    if launch.status == "error":
        return f"{_OUTCOME_PREFIX}❌ error — {launch.ack_detail or 'unknown error'}"
    if launch.status == "expired":
        return f"{_OUTCOME_PREFIX}⌛ expired (panel never connected within 7 days)"
    return f"{_OUTCOME_PREFIX}{launch.status}"


def with_outcome_line(content: str | None, line: str) -> str:
    """Append `line` to the Discord message, replacing any earlier outcome line."""
    kept = [
        row for row in (content or "").splitlines() if not row.startswith(_OUTCOME_PREFIX)
    ]
    while kept and not kept[-1].strip():
        kept.pop()
    return "\n".join([*kept, line]) if kept else line


def launch_frame(launch: CepLaunch, *, replay: bool) -> dict[str, Any]:
    return {
        "type": "launch",
        "launch_id": launch.launch_id,
        "project_id": launch.project_id,
        "anime_title": launch.anime_title,
        "requested_at": _iso(launch.requested_at),
        "replay": replay,
    }


@dataclass
class _Conn:
    ws: WebSocket
    connected_at: datetime
    last_pong_at: datetime
    panel_build_id: str | None = None
    port: int | None = None
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    closed: bool = False


class CepLinkHub:
    def __init__(
        self,
        *,
        store: CepLaunchStore,
        settings: Settings,
        discord_provider: Callable[[], Any],
        heartbeat_interval_s: float = HEARTBEAT_INTERVAL_S,
        auth_timeout_s: float = AUTH_TIMEOUT_S,
    ) -> None:
        self._store = store
        self._settings = settings
        self._discord_provider = discord_provider
        self._heartbeat_interval_s = float(heartbeat_interval_s)
        self._auth_timeout_s = float(auth_timeout_s)
        self._conns: dict[int, _Conn] = {}
        self._last_seen_at: datetime | None = None
        self._last_panel_build_id: str | None = None

    # ---- socket lifecycle ----------------------------------------------------
    async def handle_socket(self, websocket: WebSocket) -> None:
        # Accept first: a close before accept surfaces as HTTP 403 to the
        # client, which would hide the 4401 auth code from the panel.
        await websocket.accept()
        conn = _Conn(ws=websocket, connected_at=_now(), last_pong_at=_now())

        try:
            raw = await asyncio.wait_for(websocket.receive_text(), self._auth_timeout_s)
        except TimeoutError:
            await self._close(conn, CLOSE_AUTH_FAILED, "auth timeout")
            return
        except (WebSocketDisconnect, RuntimeError):
            return

        frame = _parse_frame(raw)
        if frame is None or frame.get("type") != "auth":
            await self._close(conn, CLOSE_PROTOCOL, "expected auth frame")
            return
        expected = self._settings.cep_link_token or ""
        token = str(frame.get("token") or "")
        if not expected or not hmac.compare_digest(token, expected):
            logger.warning("CEP link auth rejected from %s", _client_label(websocket))
            await self._close(conn, CLOSE_AUTH_FAILED, "invalid token")
            return

        conn.panel_build_id = _opt_str(frame.get("panel_build_id"))
        conn.port = _opt_int(frame.get("port"))
        self._conns[id(conn)] = conn
        self._touch(conn)
        logger.info(
            "CEP link connected (build=%s, client=%s)",
            conn.panel_build_id, _client_label(websocket),
        )

        heartbeat = asyncio.create_task(self._heartbeat(conn))
        try:
            pending = await self._store.list_pending(_now())
            await self._send(
                conn,
                {
                    "type": "auth_ok",
                    "protocol_version": PROTOCOL_VERSION,
                    "server_time": _iso(_now()),
                    "pending_count": len(pending),
                    "heartbeat_interval_s": self._heartbeat_interval_s,
                },
            )
            for launch in pending:
                if await self._send(conn, launch_frame(launch, replay=True)):
                    await self._store.mark_delivered(launch.project_id, launch.launch_id)
            while not conn.closed:
                raw = await websocket.receive_text()
                await self._handle_frame(conn, raw)
        except (WebSocketDisconnect, RuntimeError):
            pass
        except Exception:
            logger.exception("CEP link session failed")
        finally:
            conn.closed = True
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await heartbeat
            self._conns.pop(id(conn), None)
            logger.info("CEP link disconnected (build=%s)", conn.panel_build_id)

    async def _heartbeat(self, conn: _Conn) -> None:
        interval = self._heartbeat_interval_s
        while not conn.closed:
            await asyncio.sleep(interval)
            if conn.closed:
                return
            now = _now()
            if (now - conn.last_pong_at).total_seconds() > 2 * interval:
                logger.warning("CEP link heartbeat timeout (build=%s)", conn.panel_build_id)
                await self._close(conn, CLOSE_HEARTBEAT, "heartbeat timeout")
                return
            await self._send(conn, {"type": "ping", "ts": _iso(now)})

    async def _handle_frame(self, conn: _Conn, raw: str) -> None:
        self._touch(conn)
        frame = _parse_frame(raw)
        if frame is None:
            await self._send_error(conn, "bad_frame", "frame must be a JSON object")
            return
        kind = frame.get("type")
        if kind == "pong":
            conn.last_pong_at = _now()
        elif kind == "ping":
            conn.last_pong_at = _now()
            await self._send(conn, {"type": "pong", "ts": frame.get("ts")})
        elif kind == "ack":
            await self._handle_ack(conn, frame)
        else:
            await self._send_error(conn, "unknown_type", f"unknown frame type {kind!r}")

    async def _handle_ack(self, conn: _Conn, frame: dict[str, Any]) -> None:
        project_id = str(frame.get("project_id") or "")
        launch_id = str(frame.get("launch_id") or "")
        result = str(frame.get("result") or "")
        if not project_id or not launch_id or result not in ACK_RESULTS:
            await self._send_error(conn, "bad_frame", "ack needs project_id, launch_id, result")
            return
        detail = _opt_str(frame.get("detail"))
        updated = await self._store.record_ack(
            project_id,
            launch_id,
            result=result,
            detail=detail,
            panel_build_id=conn.panel_build_id,
        )
        if updated is None:
            await self._send_error(
                conn, "unknown_launch", f"no current launch {launch_id} for {project_id}"
            )
            return
        logger.info(
            "CEP launch %s for %s acked: %s (%s)",
            launch_id, project_id, result, detail or "-",
        )
        await self.notify_outcome(updated)

    # ---- outbound ------------------------------------------------------------
    async def push(self, launch: CepLaunch) -> bool:
        """Send a new launch to every connected panel; True if at least one got it."""
        delivered = False
        for conn in list(self._conns.values()):
            if await self._send(conn, launch_frame(launch, replay=False)):
                delivered = True
        if delivered:
            await self._store.mark_delivered(launch.project_id, launch.launch_id)
        return delivered

    async def notify_outcome(self, launch: CepLaunch) -> None:
        """Best-effort edit of the project's Discord message with the outcome line."""
        if not launch.discord_message_id:
            return
        discord = self._discord_provider()
        if discord is None:
            return
        content = with_outcome_line(launch.discord_content, outcome_line(launch))
        try:
            await discord.edit_message(
                self._settings.discord.upload_channel_id,
                launch.discord_message_id,
                content=content,
            )
        except Exception as exc:
            logger.warning(
                "Discord outcome edit failed for %s: %s", launch.project_id, exc
            )

    def status(self) -> dict[str, Any]:
        live = [c for c in self._conns.values() if not c.closed]
        return {
            "connected": bool(live),
            "connections": len(live),
            "connected_since": _iso(min(c.connected_at for c in live)) if live else None,
            "last_seen_at": _iso(self._last_seen_at),
            "panel_build_id": (
                live[0].panel_build_id if live else self._last_panel_build_id
            ),
            "heartbeat_interval_s": self._heartbeat_interval_s,
        }

    # ---- maintenance ---------------------------------------------------------
    async def _maintenance_once(self) -> list[CepLaunch]:
        expired = await self._store.expire_stale(_now())
        for launch in expired:
            logger.info("CEP launch %s for %s expired", launch.launch_id, launch.project_id)
            await self.notify_outcome(launch)
        return expired

    async def maintenance_loop(
        self, stop_event: asyncio.Event, interval_s: float = 600.0
    ) -> None:
        while not stop_event.is_set():
            try:
                await self._maintenance_once()
            except Exception:
                logger.exception("CEP link maintenance failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=interval_s)

    async def shutdown(self) -> None:
        for conn in list(self._conns.values()):
            await self._close(conn, CLOSE_SHUTDOWN, "server restart")

    # ---- helpers -------------------------------------------------------------
    def _touch(self, conn: _Conn) -> None:
        self._last_seen_at = _now()
        if conn.panel_build_id:
            self._last_panel_build_id = conn.panel_build_id

    async def _send(self, conn: _Conn, payload: dict[str, Any]) -> bool:
        if conn.closed:
            return False
        try:
            async with conn.send_lock:
                await conn.ws.send_text(json.dumps(payload))
            return True
        except Exception as exc:
            logger.debug("CEP link send failed: %s", exc)
            conn.closed = True
            return False

    async def _send_error(self, conn: _Conn, code: str, detail: str) -> None:
        await self._send(conn, {"type": "error", "code": code, "detail": detail})

    async def _close(self, conn: _Conn, code: int, reason: str) -> None:
        conn.closed = True
        with contextlib.suppress(Exception):
            await conn.ws.close(code=code, reason=reason)


def _parse_frame(raw: str) -> dict[str, Any] | None:
    try:
        frame = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return frame if isinstance(frame, dict) else None


def _opt_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _opt_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _client_label(websocket: WebSocket) -> str:
    client = websocket.client
    return f"{client.host}:{client.port}" if client else "unknown"
